"""
validate.py — Validação local de estrutura e coerência dos artefatos Minute.

Contrato alinhado ao APK Android 1.29.0 (Galaxy S22, smali `l2.1`) e ao
histórico 1.28.0. Cobre timestamps por quadro, IMU CSV, timebase
android_elapsedRealtimeNanos / trinet_camera_monotonic. Não comprova origem
física dos dados nem substitui `POST /uploads/{id}/evaluate`.

Severidade:
  - fail: estrutura inválida ou incoerência demonstrável entre os artefatos.
  - warn: informação ausente, formato legado ou relação não comprovada.

Uso:
    from moneymin.validate import validate_sidecar_zip, summarize
    result = validate_sidecar_zip(zip_bytes, log_id=..., duration_ms=...)
    print(summarize(result))
"""
from __future__ import annotations

import io
import json
import math
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from . import config
from .atomic_io import JsonStateError, decode_json_state

_ISO_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_NS = re.compile(r"^[0-9]+$")
_MAX_NS = (1 << 63) - 1
_NATIVE_CLOCK = "android_elapsedRealtimeNanos"
_EXTERNAL_CLOCK = "trinet_camera_monotonic"


@dataclass
class Check:
    name: str
    status: str          # "pass" | "fail" | "warn"
    detail: str = ""
    issues: list[str] = field(default_factory=list)


def _ns(value: Any) -> int | None:
    """Nanosegundos são strings decimais no metadata e inteiros nos CSVs."""
    if not isinstance(value, str) or not _NS.fullmatch(value):
        return None
    try:
        number = int(value)
        return number if number <= _MAX_NS else None
    except ValueError:  # inclui strings além do limite de conversão do Python
        return None


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _jvm_int(value: Any, bits: int = 32) -> bool:
    """Reject JSON bool/float values that cannot be the APK's Int/Long fields."""
    return type(value) is int and -(1 << (bits - 1)) <= value < (1 << (bits - 1))


def _check_metadata_fields(metadata: dict[str, Any]) -> list[Check]:
    """Types written by Ll2/o0.c, without inferring sensor/wall-clock equality.

    Native video paths may be absolute or relative. Rotation is an Int, not a
    proven closed enum here. Chunk wall times are Long values; the constructor
    does not prove their span equals the sensor interval or durationMs.
    """
    video = metadata.get("video")
    video_ok = (isinstance(video, dict) and _nonempty_string(video.get("path"))
                and all(_jvm_int(video.get(key)) and video[key] > 0
                        for key in ("width", "height"))
                and _jvm_int(video.get("rotationDeg")))
    session = metadata.get("session")
    session_ok = isinstance(session, dict) and _nonempty_string(session.get("id"))
    chunk = metadata.get("chunk")
    chunk_ok = (isinstance(chunk, dict) and _jvm_int(chunk.get("index"))
                and chunk["index"] >= 0
                and all(_jvm_int(chunk.get(key), 64)
                        for key in ("startTimeMs", "endTimeMs")))
    source_ok = metadata.get("source") == "ego"
    version_ok = _nonempty_string(metadata.get("appVersion"))
    return [
        Check("metadata_json.video", "pass" if video_ok else "fail",
              "video deve ter path string não vazia, dimensões Int positivas e rotationDeg Int"),
        Check("metadata_json.session", "pass" if session_ok else "fail",
              "session deve ter id string não vazia"),
        Check("metadata_json.chunk", "pass" if chunk_ok else "fail",
              "chunk deve ter index Int não negativo e startTimeMs/endTimeMs Long"),
        Check("metadata_json.source", "pass" if source_ok else "fail",
              "source=ego no writer l2.1/n0 do APK 1.29.0"),
        Check("metadata_json.appVersion", "pass" if version_ok else "fail",
              "appVersion deve ser string não vazia; versão remota não é validada localmente"),
    ]


def _finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _axis(row: list[str]) -> list[int | float] | None:
    if len(row) != 7:
        return None
    try:
        timestamp = _ns(row[0])
        if timestamp is None:
            return None
        values = [float(value) for value in row[1:]]
        if not all(math.isfinite(value) for value in values):
            return None
        # Não converter timestamp para float: ele pode exceder 2**53.
        return [timestamp, *values]
    except (TypeError, ValueError):
        return None


def _check_platform(platform: Any, name: str, *, require_version: bool = False) -> Check:
    """APK 1.29 (n0.1.smali) escreve platform.os; type é legado pré-1.29."""
    if not isinstance(platform, dict):
        return Check(name, "fail", "platform não é objeto")
    values = {key: platform[key] for key in ("os", "type") if key in platform}
    if (not values or any(not isinstance(value, str) or value.casefold() != "android"
                          for value in values.values())):
        return Check(name, "fail", "os/type ausente, inválido ou conflitante")
    if require_version and not _positive_int(platform.get("version")):
        return Check(name, "fail", "version deve ser SDK inteiro positivo")
    if "os" not in values:
        return Check(name, "warn", "platform.type legado; APK 1.29 usa platform.os")
    return Check(name, "pass", "platform.os=android")


def _check_external_timebase(timebase: dict[str, Any]) -> Check:
    """K0.m mantém o relógio do host em extras, separado do relógio Trinet."""
    name = "timebase.external_host"
    fields = ("hostStartElapsedNs", "hostEndElapsedNs")
    if not any(key in timebase for key in fields):
        return Check(name, "warn", "sem âncoras do host; não é possível correlacionar os relógios")
    start, end = (_ns(timebase.get(key)) for key in fields)
    if start is None or end is None or end <= start:
        return Check(name, "fail", "âncoras do host incompletas, inválidas ou invertidas")
    if timebase.get("frameTimestampSemantics") != "mid_exposure":
        return Check(name, "warn", "semântica de timestamp externo não confirmada")
    return Check(name, "pass", "host elapsed e câmera externa preservados em domínios separados")


def _check_cameras(cameras: Any) -> Check:
    name = "artifact.cameras_schema"
    if cameras is None:
        return Check(name, "warn", "APK permite ausência de calibração; óptica não verificada")
    if not isinstance(cameras, list) or not cameras:
        return Check(name, "fail", "cameras deve ser lista não vazia")
    warnings = []
    for camera in cameras:
        if not isinstance(camera, dict) or not isinstance(camera.get("name"), str) or not camera["name"]:
            return Check(name, "fail", "câmera sem nome válido")
        intrinsics = camera.get("intrinsics")
        if intrinsics is None:
            warnings.append("intrinsics ausentes: " + str(camera.get("intrinsics_omitted_reason") or "motivo não informado"))
            continue
        if not isinstance(intrinsics, dict):
            return Check(name, "fail", "intrinsics não é objeto")
        optics = ("fx", "fy", "cx", "cy", "coordinate_frame",
                  "intrinsics_reference_dimensions", "distortion_model", "distortion_coefficients")
        if any(key not in intrinsics for key in optics):
            return Check(name, "fail", "intrinsics incompletas")
        numbers = [intrinsics[key] for key in ("fx", "fy", "cx", "cy")]
        if (any(not _finite_number(value) for value in numbers)
                or numbers[0] <= 0 or numbers[1] <= 0):
            return Check(name, "fail", "intrinsics numéricas inválidas")
        dimensions = intrinsics["intrinsics_reference_dimensions"]
        if (not isinstance(dimensions, dict)
                or not all(_positive_int(dimensions.get(key)) for key in ("width", "height"))):
            return Check(name, "fail", "dimensões de referência inválidas")
        coefficients = intrinsics["distortion_coefficients"]
        if (not isinstance(coefficients, list)
                or any(not _finite_number(value) for value in coefficients)):
            return Check(name, "fail", "coeficientes de distorção inválidos")
        model = intrinsics["distortion_model"]
        if model == "brown_conrady":
            if (len(coefficients) != 5
                    or intrinsics.get("distortion_coefficients_layout") != "brown_conrady_k1_k2_k3_p1_p2"):
                return Check(name, "fail", "layout Brown-Conrady inválido")
        elif model == "pinhole":
            if coefficients:
                return Check(name, "fail", "pinhole com coeficientes de distorção")
        else:
            warnings.append("modelo óptico não interpretado")
        if intrinsics["coordinate_frame"] != "video_frame":
            warnings.append("referência de coordenadas não interpretada")
    return Check(name, "warn" if warnings else "pass", "; ".join(warnings) or "calibração estruturalmente coerente")


def validate_sidecar_zip(
    payload: bytes,
    *,
    log_id: str,
    duration_ms: int,
) -> list[Check]:
    """Verifica artefatos localmente; um pass não é aprovação do backend."""
    checks: list[Check] = []
    if not _positive_int(duration_ms):
        return [Check("duration_ms", "fail", "duração deve ser inteiro positivo")]
    if not payload:
        return [Check("zip", "fail", "sidecar vazio")]
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = {info.filename for info in archive.infolist()}
            if len(names) != len(archive.infolist()):
                return [Check("zip", "fail", "membros duplicados no zip")]
            if any("/" in name or "\\" in name or name.startswith(".") for name in names):
                return [Check("zip", "fail", "membros com caminhos inesperados")]
            if sum(info.file_size for info in archive.infolist()) > 512 * 1024 * 1024:
                return [Check("zip", "fail", "conteúdo descompactado excede 512 MiB")]
            required = {
                f"{log_id}.metadata.json",
                f"{log_id}.imu.csv",
                f"{log_id}.frames.csv",
            }
            missing = sorted(required - names)
            if missing:
                return [Check("zip", "fail",
                              "membros ausentes: " + ", ".join(missing))]
            # Writer APK order: imu → frames → metadata (C2500n0).
            ordered = [info.filename for info in archive.infolist()
                       if info.filename in required]
            expected_order = [
                f"{log_id}.imu.csv",
                f"{log_id}.frames.csv",
                f"{log_id}.metadata.json",
            ]
            order_ok = ordered == expected_order
            metadata = decode_json_state(
                archive.read(f"{log_id}.metadata.json").decode("utf-8"))
            imu_text = archive.read(f"{log_id}.imu.csv").decode("utf-8")
            frames_text = archive.read(
                f"{log_id}.frames.csv").decode("utf-8")
    except JsonStateError:
        return [Check("metadata_json.valid", "fail",
                      "metadata.json inválido ou ambíguo; arquivo preservado")]
    except Exception as exc:  # noqa: BLE001
        return [Check("zip", "fail", f"zip inválido: {exc}")]

    checks.append(Check(
        "zip.member_order",
        "pass" if order_ok else "fail",
        "imu→frames→metadata" if order_ok else f"ordem {ordered}"))

    # --- metadata_json.valid -------------------------------------------------
    if not isinstance(metadata, dict):
        checks.append(Check("metadata_json.valid", "fail",
                            "metadata.json não é objeto"))
        return checks
    missing_keys = [
        key for key in ("id", "logId", "createdAt", "durationMs", "appVersion",
                        "platform", "device", "video", "session", "chunk",
                        "source", "timebase", "imuDiagnostics", "artifacts",
                        "codecActuals")
        if key not in metadata
    ]
    checks.append(Check(
        "metadata_json.valid", "fail" if missing_keys else "pass",
        "chaves ausentes: " + ", ".join(missing_keys) if missing_keys
        else f"{len(metadata)} chaves top-level"))
    checks.extend(_check_metadata_fields(metadata))

    # --- logId ↔ simulês do upload ------------------------------------------
    meta_log = metadata.get("logId")
    identities_match = meta_log == log_id and metadata.get("id") == log_id
    checks.append(Check(
        "xcheck.metadata_json_matches_upload",
        "pass" if identities_match else "fail",
        f"metadata.id={metadata.get('id')!r}, logId={meta_log!r} vs {log_id!r}"))
    declared_duration = metadata.get("durationMs")
    duration_ok = (_positive_int(declared_duration)
                   and abs(declared_duration - duration_ms) <= max(500, int(duration_ms * 0.01)))
    checks.append(Check("xcheck.duration_consistency.metadata", "pass" if duration_ok else "fail",
                        f"metadata={declared_duration} vs duração {duration_ms} ms"
                        if _positive_int(declared_duration)
                        else "durationMs deve ser inteiro positivo"))

    # --- platform/device (contrato Android do jadx) --------------------------
    platform = metadata.get("platform") or {}
    checks.append(_check_platform(platform, "platform.android", require_version=True))
    device = metadata.get("device") or {}
    checks.append(Check(
        "device.android",
        "fail" if not (
            isinstance(device, dict)
            and device.get("systemName") == "Android"
            and _nonempty_string(device.get("model"))
            and _nonempty_string(device.get("systemVersion")))
        else "pass",
        json.dumps(device, ensure_ascii=False) if isinstance(device, dict)
        else str(device)))

    # --- timebase ------------------------------------------------------------
    raw_timebase = metadata.get("timebase")
    timebase = raw_timebase if isinstance(raw_timebase, dict) else {}
    raw_domain = timebase.get("clockDomain")
    clock_domain = raw_domain if isinstance(raw_domain, str) else ""
    accepted = {_NATIVE_CLOCK, _EXTERNAL_CLOCK}
    # 1.29 writer only emits native or Trinet domains (q.1 / K0.smali).
    checks.append(Check(
        "timebase.clockDomain",
        "pass" if clock_domain in accepted else "fail",
        clock_domain or "(ausente)"))
    for key in ("startNs", "endNs", "startSensorTimestampNs",
                "endSensorTimestampNs", "firstFrameSensorTimestampNs"):
        value = timebase.get(key)
        ok = _ns(value) is not None
        checks.append(Check(
            f"timebase.{key}", "pass" if ok else "fail", repr(value)))
    start_ns = _ns(timebase.get("startNs"))
    end_ns = _ns(timebase.get("endNs"))
    span_ok = start_ns is not None and end_ns is not None and end_ns > start_ns
    checks.append(Check(
        "timebase.span",
        "pass" if span_ok else "fail",
        f"{start_ns}..{end_ns}"))
    if clock_domain == _EXTERNAL_CLOCK:
        checks.append(_check_external_timebase(timebase))

    # --- cameras -------------------------------------------------------------
    checks.append(_check_cameras(metadata.get("cameras")))

    # --- codecActuals ---------------------------------------------------------
    codec = metadata.get("codecActuals")
    codec_status = "fail"
    if isinstance(codec, dict):
        if not codec:
            codec_status = "warn"  # EgoCodecActuals retorna mapa vazio se leitura falhar
        elif isinstance(codec.get("mime"), str) and codec["mime"].startswith("video/"):
            dimensions = [codec.get(key) for key in ("width", "height")]
            if all(value is None or _positive_int(value) for value in dimensions):
                codec_status = "warn" if any(value is None for value in dimensions) else "pass"
    checks.append(Check(
        "codecActuals.schema",
        codec_status,
        json.dumps(codec, ensure_ascii=False) if isinstance(codec, dict)
        else str(codec)))

    # --- artifacts -----------------------------------------------------------
    artifacts = metadata.get("artifacts") or []
    ok_artifacts = isinstance(artifacts, list)
    for name in ("imu", "frames"):
        matches = [item for item in artifacts if isinstance(item, dict) and item.get("name") == name] if isinstance(artifacts, list) else []
        if (len(matches) != 1 or matches[0].get("remoteFilename") != f"{log_id}.{name}.csv"
                or matches[0].get("contentType") != "text/csv"):
            ok_artifacts = False
    checks.append(Check(
        "artifact.metadata_json_fields",
        "pass" if ok_artifacts and artifacts else "fail",
        json.dumps(artifacts, ensure_ascii=False)[:200] if artifacts
        else "(sem artifacts)"))

    # --- IMU csv -------------------------------------------------------------
    imu_checks = _check_imu_csv(imu_text, duration_ms)
    checks.extend(imu_checks)
    checks.append(_check_imu_timebase(imu_text, timebase))

    # --- frames csv ----------------------------------------------------------
    frame_checks = _check_frames_csv(frames_text, duration_ms)
    checks.extend(frame_checks)
    checks.append(_check_frames_timebase(frames_text, timebase))

    # --- imuDiagnostics (1.29: strategy + sampleCount; sem clockOffsetNs) ----
    diag = metadata.get("imuDiagnostics") or {}
    if isinstance(diag, dict) and "clockOffsetNs" in diag:
        checks.append(Check(
            "imuDiagnostics.no_clockOffsetNs",
            "fail",
            "clockOffsetNs não pertence ao metadata.json do sidecar 1.29 "
            "(só ao meta de sessão JS / debug overlay)",
        ))
    elif isinstance(diag, dict) and diag.get("strategy") not in (None, "gyro_anchored_v1"):
        checks.append(Check(
            "imuDiagnostics.strategy",
            "warn",
            f"strategy={diag.get('strategy')!r}; 1.29 usa gyro_anchored_v1",
        ))
    if isinstance(diag, dict):
        span = diag.get("maxInterpolationSpanNs")
        # EgoImu constructor always serializes config ceiling 25000000.
        if span is None:
            checks.append(Check(
                "imuDiagnostics.maxInterpolationSpanNs",
                "warn",
                "ausente (writer 1.29 emite 25000000)"))
        else:
            span_ok = span == "25000000" or span == 25000000
            checks.append(Check(
                "imuDiagnostics.maxInterpolationSpanNs",
                "pass" if span_ok else "fail",
                repr(span)))
    if isinstance(diag, dict) and _positive_int(diag.get("sampleCount")):
        imu_rows = len([ln for ln in imu_text.splitlines() if ln.strip()]) - 1
        reported = int(diag.get("sampleCount"))
        # O que importa de verdade: sampleCount do metadata == linhas do CSV.
        # (antes comparava com o *esperado*, dando falso "pass" p/ valores
        # arbitrários tipo 123 com 30.001 amostras.)
        mismatch = abs(reported - imu_rows)
        checks.append(Check(
            "imuDiagnostics.sampleCount",
            "fail" if mismatch else "pass",
            f"metadata={reported} vs linhas CSV={imu_rows}"))
        # taxa (warn separado): o CSV segue ~500 Hz p/ a duração declarada?
        rate = config.ANDROID_IMU_SAMPLE_RATE_HZ
        expected = max(1, int(duration_ms / 1000 * rate) + 1)
        if abs(imu_rows - expected) > max(5, int(expected * 0.01)):
            checks.append(Check(
                "imu.rate_matches_duration", "warn",
                f"linhas={imu_rows} vs ~{expected} para 500 Hz"))
    else:
        checks.append(Check("imuDiagnostics.sampleCount", "fail",
                            "sampleCount ausente/nao-int"))

    # --- gravidade (warn: orientação de montagem varia) ----------------------
    gravity = _gravity_check(imu_text)
    checks.append(Check("imu.signals_gravity", "warn", gravity))

    return checks


def _check_imu_csv(text: str, duration_ms: int) -> list[Check]:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return [Check("imu.csv", "fail", "vazio")]
    if lines[0].split(",") != ["t", "ax", "ay", "az", "wx", "wy", "wz"]:
        return [Check("imu.csv", "fail", "header inesperado: " + lines[0])]
    rows = lines[1:]
    if len(rows) < 2:
        return [Check("imu.csv", "fail",
                      f"poucas amostras ({len(rows)}) — CSV só com cabeçalho?")]
    parsed = [_axis(row.split(",")) for row in rows]
    if any(row is None for row in parsed):
        return [Check("imu.csv", "fail",
                      "timestamp ou sinais inválidos/não-finitos")]
    ts = [int(row[0]) for row in parsed if row is not None]
    if any(b <= a for a, b in zip(ts, ts[1:])):
        return [Check("imu.csv", "fail", "timestamps t nao monotonicos")]
    gap = (ts[-1] - ts[0]) / max(1, len(ts) - 1)
    rate_hz = 1e9 / gap if gap > 0 else 0.0
    ok_rate = abs(rate_hz - config.ANDROID_IMU_SAMPLE_RATE_HZ) \
        <= config.ANDROID_IMU_SAMPLE_RATE_HZ * 0.05
    checks = [
        Check("imu.csv", "pass", f"{len(rows)} amostras"),
        Check("imu.rate", "pass" if ok_rate else "warn",
              f"{rate_hz:.0f} Hz (esperado 500)"),
    ]
    span = ts[-1] - ts[0]
    expected = duration_ms * 1_000_000
    ok_span = abs(span - expected) <= max(500_000_000, expected * 0.02)
    checks.append(Check(
        "xcheck.duration_consistency.imu",
        "pass" if ok_span else "fail",
        f"span {span / 1e9:.3f}s vs duração {duration_ms / 1000:.3f}s"))
    return checks


def _check_imu_timebase(text: str, timebase: dict[str, Any]) -> Check:
    """IMU ``t`` and timebase must share ``android_elapsedRealtimeNanos``.

    Phone EgoImu stamps samples with the same elapsedRealtime domain as
    ``firstFrameSensorTimestampNs`` (Y.smali / q.1). A relative zero grid
    against a non-zero uptime anchor fails the wire envelope.
    """
    name = "xcheck.imu_timebase"
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        return Check(name, "fail", "imu.csv sem amostras suficientes")
    if not isinstance(timebase, dict):
        return Check(name, "fail", "timebase não é objeto")
    domain = timebase.get("clockDomain")
    if domain and domain != _NATIVE_CLOCK:
        return Check(name, "warn",
                     f"domínio {domain!r}; cruzamento IMU/elapsedRealtime não exigido")
    try:
        if lines[0].split(",") != ["t", "ax", "ay", "az", "wx", "wy", "wz"]:
            raise ValueError("header IMU inesperado")
        first_imu = _ns(lines[1].split(",")[0])
        if first_imu is None:
            raise ValueError("primeiro t inválido")
        anchor = _ns(timebase.get("firstFrameSensorTimestampNs"))
        start = _ns(timebase.get("startNs"))
        end = _ns(timebase.get("endNs"))
        if anchor is None or start is None or end is None or end <= start:
            raise ValueError("âncora/intervalo inválidos")
    except (TypeError, ValueError, IndexError) as exc:
        return Check(name, "fail", f"não foi possível cruzar IMU e timebase: {exc}")
    # One sample period (2 ms at 500 Hz) around the first-frame sensor anchor.
    step_ns = int(1_000_000_000 // config.ANDROID_IMU_SAMPLE_RATE_HZ)
    if anchor > step_ns and first_imu < step_ns:
        return Check(
            name, "fail",
            f"IMU relativa t0={first_imu} com âncora elapsedRealtime={anchor}")
    if abs(first_imu - anchor) > step_ns:
        return Check(
            name, "fail",
            f"IMU t0={first_imu} desalinhada da âncora do 1º frame ({anchor})")
    if first_imu < start - step_ns or first_imu > end + step_ns:
        return Check(
            name, "fail",
            f"IMU t0={first_imu} fora do intervalo timebase [{start}, {end}]")
    return Check(name, "pass", "IMU e timebase no mesmo domínio elapsedRealtime")


def _check_frames_csv(text: str, duration_ms: int) -> list[Check]:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return [Check("frames.csv", "fail", "vazio")]
    if lines[0].split(",") != ["i", "ptsNs", "dtNs", "tNs", "key"]:
        return [Check("frames.csv", "fail", "header inesperado: " + lines[0])]
    rows: list[list[str]] = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) != 5:
            return [Check("frames.csv", "fail", f"linha malformada: {ln[:80]}")]
        rows.append(parts)
    if len(rows) < 2:
        return [Check("frames.csv", "fail",
                      f"poucas linhas ({len(rows)}) — CSV só com cabeçalho?")]
    try:
        indexes = [int(r[0]) for r in rows]
        pts = [_ns(r[1]) for r in rows]
        dt = [int(r[2]) for r in rows]
        tns = [_ns(r[3]) for r in rows]
        keys = [int(r[4]) for r in rows]
        if any(value is None for value in pts + tns):
            raise ValueError("timestamps fora do formato/range de Long não negativo")
    except (TypeError, ValueError) as exc:
        return [Check("frames.csv", "fail", f"valores ilegiveis: {exc}")]
    issues = []
    if indexes != list(range(len(rows))):
        issues.append("i nao sequencial")
    if any(b <= a for a, b in zip(pts, pts[1:])):
        issues.append("ptsNs nao monotonico")
    if any(value < 0 for value in pts + tns):
        issues.append("timestamp negativo")
    expected_dt = [0, *[b - a for a, b in zip(pts, pts[1:])]]
    if dt != expected_dt:
        issues.append("dtNs nao corresponde aos deltas de ptsNs")
    if any(b <= a for a, b in zip(tns, tns[1:])):
        issues.append("tNs nao monotonico")
    if not any(value == 1 for value in keys):
        issues.append("nenhum keyframe")
    if any(value not in (0, 1) for value in keys):
        issues.append("key fora de 0/1")
    status = "pass" if not issues else "fail"
    checks = [
        Check("frames.csv", status,
              "; ".join(issues) if issues
              else f"{len(rows)} frames, "
                   f"{sum(1 for value in keys if value == 1)} keyframes")]

    span = pts[-1] - pts[0]
    expected = duration_ms * 1_000_000
    ok_span = abs(span - expected) <= max(500_000_000, expected * 0.02)
    checks.append(Check(
        "xcheck.duration_consistency.frames",
        "pass" if ok_span else "fail",
        f"span {span / 1e9:.3f}s vs duração {duration_ms / 1000:.3f}s"))
    sensor_span = tns[-1] - tns[0]
    sensor_ok = abs(sensor_span - expected) <= max(500_000_000, expected * 0.02)
    checks.append(Check(
        "xcheck.duration_consistency.frame_timestamps", "pass" if sensor_ok else "fail",
        f"span tNs {sensor_span / 1e9:.3f}s vs duração {duration_ms / 1000:.3f}s"))
    return checks


def _check_frames_timebase(text: str, timebase: dict[str, Any]) -> Check:
    """Cruza a âncora sem impor offset constante aos timestamps reais.

    EgoSidecar.j usa timestamps de captura por quadro. Quando faltam, calcula
    anchor + (ptsNs - firstPtsNs). Portanto PTS não zero e offsets variáveis
    não são incoerência por si só; isso também vale no relógio externo.
    """
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        return Check("xcheck.frames_timebase", "fail",
                     "frames.csv sem amostras suficientes")
    try:
        if not isinstance(timebase, dict):
            raise ValueError("timebase não é objeto")
        anchor = _ns(timebase.get("firstFrameSensorTimestampNs"))
        if anchor is None:
            raise ValueError("âncora inválida")
        if lines[0] != "i,ptsNs,dtNs,tNs,key":
            raise ValueError("header de frames inesperado")
        timestamps = []
        for line in lines[1:]:
            row = line.split(",")
            if len(row) != 5:
                raise ValueError("linha com número incorreto de colunas")
            timestamp = _ns(row[3])
            if timestamp is None:
                raise ValueError("timestamp de quadro inválido")
            timestamps.append(timestamp)
    except (TypeError, ValueError) as exc:
        return Check("xcheck.frames_timebase", "fail",
                     f"não foi possível cruzar os relógios: {exc}")
    name = "xcheck.frames_timebase"
    if any(b <= a for a, b in zip(timestamps, timestamps[1:])):
        return Check(name, "fail", "timestamps de captura não crescentes")
    if not anchor:
        return Check(name, "warn", "âncora zero: correlação com metadata não comprovada")
    anchor_mismatch = timestamps[0] != anchor
    domain = timebase.get("clockDomain")
    start, end = _ns(timebase.get("startNs")), _ns(timebase.get("endNs"))
    if start is None or end is None or end <= start:
        return Check(name, "fail", "intervalo do relógio de captura inválido")
    if domain == _EXTERNAL_CLOCK:
        if start != anchor:
            return Check(name, "fail", "início do relógio externo não corresponde à primeira captura")
        if timestamps[-1] != end:
            return Check(name, "warn", "fim externo e último quadro diferem; relação do fallback não comprovada")
    elif domain == _NATIVE_CLOCK:
        if timestamps[-1] < start or timestamps[0] > end:
            return Check(name, "fail", "quadros e intervalo elapsedRealtime não se sobrepõem")
        if timestamps[0] < start or timestamps[-1] > end:
            return Check(name, "warn", "quadros ultrapassam intervalo do host; offsets de captura não comprovados")
    else:
        return Check(name, "warn", "domínio de captura desconhecido; correlação parcial pela âncora")
    if anchor_mismatch:
        return Check(name, "warn",
                     "primeiro tNs e âncora diferem; correspondência após corrida/drop não comprovada")
    return Check(name, "pass", "âncora e timestamps por quadro coerentes; offset constante não exigido")


def _gravity_check(imu_text: str) -> str:
    lines = [ln for ln in imu_text.strip().splitlines()[1:] if ln.strip()]
    sample = lines[len(lines) // 2: len(lines) // 2 + 200]
    magnitudes: list[float] = []
    for ln in sample:
        row = _axis(ln.split(","))
        if row is None:
            continue
        ax, ay, az = row[1], row[2], row[3]
        magnitudes.append(math.sqrt(ax * ax + ay * ay + az * az))
    if not magnitudes:
        return "sem amostras para |g|"
    mean_g = sum(magnitudes) / len(magnitudes)
    if 7.0 <= mean_g <= 14.0:
        return f"|g| medio {mean_g:.2f} m/s2 (plausivel)"
    return (f"|g| medio {mean_g:.2f} m/s2 fora do intervalo 7-14; "
            "confira escalas/zeros")


def validate_upload_meta(
    meta: dict[str, Any],
    *,
    log_id: str,
    duration_ms: int,
) -> list[Check]:
    """Consistência do `meta` do POST /uploads (o backend o compara ao sidecar)."""
    checks: list[Check] = []
    if not isinstance(meta, dict):
        return [Check("upload.meta.valid", "fail", "meta não é objeto")]
    checks.append(Check(
        "upload.meta.logId",
        "fail" if str(meta.get("logId") or "") != log_id else "pass",
        f"meta.logId={meta.get('logId')!r} vs {log_id!r}"))
    declared = meta.get("durationMs")
    duration_ok = (_positive_int(declared) and _positive_int(duration_ms)
                   and abs(declared - duration_ms) <= max(500, int(duration_ms * 0.01)))
    checks.append(Check(
        "upload.meta.duration",
        "pass" if duration_ok else "fail",
        f"meta.durationMs={declared} vs {duration_ms}"
        if _positive_int(declared) and _positive_int(duration_ms)
        else "durationMs e duração esperada devem ser inteiros positivos"))
    platform = meta.get("platform") or {}
    checks.append(_check_platform(platform, "upload.meta.platform"))
    device = meta.get("device") or {}
    checks.append(Check(
        "upload.meta.device",
        "pass" if isinstance(device, dict) and _nonempty_string(device.get("model")) else "fail",
        json.dumps(device, ensure_ascii=False) if isinstance(device, dict)
        else str(device)))
    return checks


def summarize(result: list[Check]) -> dict[str, Any]:
    counts = {"pass": 0, "fail": 0, "warn": 0}
    failures: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for check in result:
        counts[check.status] = counts.get(check.status, 0) + 1
        if check.status == "fail":
            failures.append({"id": check.name, "detail": check.detail})
        elif check.status == "warn":
            warnings.append({"id": check.name, "detail": check.detail})
    return {"counts": counts, "failures": failures, "warnings": warnings}
