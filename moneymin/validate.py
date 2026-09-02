"""
validate.py — Dry-run do checklist do backend (`POST /uploads/{id}/evaluate`).

Espelha o que sabemos do evaluate real (23 checks, extraído do retorno de
avaliações do próprio projeto e dos contratos do jadx): valida o sidecar
`.data.zip` e o `meta` do `POST /uploads` ANTES de tocar a rede — o aviso de
hoje evita o retrabalho de amanhã.

Severidade:
  - fail: condição explicitamente documentada como rejeitada (metadata.valid,
    xcheck.metadata_json_matches_upload, duration_consistency, CSVs
    estruturais, vídeo); quebrar isso = rejeição certa.
  - warn: plausibilidade (gravidade do IMU, keyframes/s, clockDomain no
    conjunto aceito, intrinsics presentes) — informativo, decide o backend.

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

_ISO_MS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@dataclass
class Check:
    name: str
    status: str          # "pass" | "fail" | "warn"
    detail: str = ""
    issues: list[str] = field(default_factory=list)


def _axis(row: list[str]) -> list[float] | None:
    if len(row) != 7:
        return None
    try:
        return [float(value) for value in row]
    except (TypeError, ValueError):
        return None


def validate_sidecar_zip(
    payload: bytes,
    *,
    log_id: str,
    duration_ms: int,
) -> list[Check]:
    """Roda o checklist funcional equivalente ao evaluate contra o `.data.zip`."""
    checks: list[Check] = []
    if not payload:
        return [Check("zip", "fail", "sidecar vazio")]
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = {info.filename for info in archive.infolist()}
            if len(names) != len(archive.infolist()):
                return [Check("zip", "fail", "membros duplicados no zip")]
            required = {
                f"{log_id}.metadata.json",
                f"{log_id}.imu.csv",
                f"{log_id}.frames.csv",
            }
            missing = sorted(required - names)
            if missing:
                return [Check("zip", "fail",
                              "membros ausentes: " + ", ".join(missing))]
            metadata = json.loads(
                archive.read(f"{log_id}.metadata.json").decode("utf-8"))
            imu_text = archive.read(f"{log_id}.imu.csv").decode("utf-8", "replace")
            frames_text = archive.read(
                f"{log_id}.frames.csv").decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return [Check("zip", "fail", f"zip inválido: {exc}")]

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

    # --- logId ↔ simulês do upload ------------------------------------------
    meta_log = str(metadata.get("logId") or metadata.get("id") or "")
    checks.append(Check(
        "xcheck.metadata_json_matches_upload",
        "fail" if meta_log != log_id else "pass",
        f"metadata.logId={meta_log!r} vs upload logId={log_id!r}"))

    # --- platform/device (contrato Android do jadx) --------------------------
    platform = metadata.get("platform") or {}
    checks.append(Check(
        "platform.android",
        "fail" if not (
            isinstance(platform, dict)
            and str(platform.get("type") or "").casefold() == "android"
            and isinstance(platform.get("version"), int))
        else "pass",
        json.dumps(platform, ensure_ascii=False) if isinstance(platform, dict)
        else str(platform)))
    device = metadata.get("device") or {}
    checks.append(Check(
        "device.android",
        "fail" if not (
            isinstance(device, dict)
            and device.get("systemName") == "Android"
            and device.get("model") and device.get("systemVersion"))
        else "pass",
        json.dumps(device, ensure_ascii=False) if isinstance(device, dict)
        else str(device)))

    # --- timebase ------------------------------------------------------------
    timebase = metadata.get("timebase") or {}
    clock_domain = str(timebase.get("clockDomain") or "")
    accepted = {config.ANDROID_CLOCK_DOMAIN, "trinet_camera_monotonic"}
    checks.append(Check(
        "timebase.clockDomain",
        "fail" if not clock_domain else
        "pass" if clock_domain in accepted else "warn",
        clock_domain or "(ausente)"))
    for key in ("startNs", "endNs", "startSensorTimestampNs",
                "endSensorTimestampNs", "firstFrameSensorTimestampNs"):
        value = timebase.get(key)
        ok = isinstance(value, str) and value.isdigit()
        checks.append(Check(
            f"timebase.{key}", "pass" if ok else "fail", repr(value)))
    start_ns = int(timebase.get("startNs") or 0)
    end_ns = int(timebase.get("endNs") or 0)
    checks.append(Check(
        "timebase.span",
        "fail" if end_ns <= start_ns else "pass",
        f"{start_ns}..{end_ns}"))

    # --- cameras -------------------------------------------------------------
    cameras = metadata.get("cameras")
    camera_ok = bool(cameras) and isinstance(cameras, list)
    if camera_ok:
        cam = cameras[0]
        intrinsics = cam.get("intrinsics") or {}
        optics = [
            key in intrinsics for key in
            ("fx", "fy", "cx", "cy", "coordinate_frame",
             "intrinsics_reference_dimensions", "distortion_model",
             "distortion_coefficients")]
        camera_ok = bool(cam.get("name")) and all(optics)
        if intrinsics.get("distortion_model") == "brown_conrady" \
                and intrinsics.get("distortion_coefficients_layout") \
                != "brown_conrady_k1_k2_k3_p1_p2":
            camera_ok = False
    checks.append(Check(
        "artifact.cameras_schema",
        "pass" if camera_ok else "fail",
        json.dumps(cameras, ensure_ascii=False)[:300] if cameras else "(sem cameras)"))

    # --- codecActuals ---------------------------------------------------------
    codec = metadata.get("codecActuals") or {}
    checks.append(Check(
        "codecActuals.schema",
        "fail" if not (
            codec.get("mime") in ("video/avc", "video/h264", "video/mp4")
            and "width" in codec and "height" in codec)
        else "pass",
        json.dumps(codec, ensure_ascii=False) if isinstance(codec, dict)
        else str(codec)))

    # --- artifacts -----------------------------------------------------------
    artifacts = metadata.get("artifacts") or []
    artifact_names = [str(item.get("name")) for item in artifacts
                      if isinstance(item, dict)]
    ok_artifacts = {"imu", "frames"}.issubset(set(artifact_names))
    checks.append(Check(
        "artifact.metadata_json_fields",
        "pass" if ok_artifacts and artifacts else "fail",
        json.dumps(artifacts, ensure_ascii=False)[:200] if artifacts
        else "(sem artifacts)"))

    # --- IMU csv -------------------------------------------------------------
    imu_checks = _check_imu_csv(imu_text, duration_ms)
    checks.extend(imu_checks)

    # --- frames csv ----------------------------------------------------------
    frame_checks = _check_frames_csv(frames_text, duration_ms)
    checks.extend(frame_checks)
    checks.append(_check_frames_timebase(frames_text, timebase))

    # --- imuDiagnostics.sampleCount ↔ IMU ------------------------------------
    diag = metadata.get("imuDiagnostics") or {}
    if isinstance(diag, dict) and isinstance(diag.get("sampleCount"), int):
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
    finite = [row for row in parsed if row is not None
              and all(math.isfinite(v) for v in row[1:])]
    if len(finite) != len(parsed):
        return [Check("imu.csv", "fail",
                      f"{len(parsed) - len(finite)} linhas não-finitas")]
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
        pts = [int(r[1]) for r in rows]
        dt = [int(r[2]) for r in rows]
        tns = [int(r[3]) for r in rows]
        keys = [int(r[4]) for r in rows]
    except (TypeError, ValueError) as exc:
        return [Check("frames.csv", "fail", f"valores ilegiveis: {exc}")]
    issues = []
    if indexes != list(range(len(rows))):
        issues.append("i nao sequencial")
    if any(b <= a for a, b in zip(pts, pts[1:])):
        issues.append("ptsNs nao monotonico")
    # dtNs pode variar (jitter de frame), so e invalido se negativo.
    if any(value < 0 for value in dt):
        issues.append("dtNs negativo")
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
    return checks


def _check_frames_timebase(text: str, timebase: dict[str, Any]) -> Check:
    """Cruza o relógio do frames.csv com o timebase do metadata.

    No sidecar Android, ``tNs = ptsNs + firstFrameSensorTimestampNs`` para
    todos os quadros. Isso impede anexar um CSV zero-based a metadata baseada
    em ``elapsedRealtimeNanos``.
    """
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        return Check("xcheck.frames_timebase", "fail",
                     "frames.csv sem amostras suficientes")
    try:
        expected = int(timebase.get("firstFrameSensorTimestampNs") or "")
        offsets = []
        for line in lines[1:]:
            row = line.split(",")
            if len(row) != 5:
                raise ValueError("linha com número incorreto de colunas")
            offsets.append(int(row[3]) - int(row[1]))
    except (TypeError, ValueError) as exc:
        return Check("xcheck.frames_timebase", "fail",
                     f"não foi possível cruzar os relógios: {exc}")
    mismatches = sum(offset != expected for offset in offsets)
    return Check(
        "xcheck.frames_timebase",
        "fail" if mismatches else "pass",
        (f"{mismatches}/{len(offsets)} frames fora do offset {expected} ns"
         if mismatches else f"offset constante {expected} ns"),
    )


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
    checks.append(Check(
        "upload.meta.logId",
        "fail" if str(meta.get("logId") or "") != log_id else "pass",
        f"meta.logId={meta.get('logId')!r} vs {log_id!r}"))
    checks.append(Check(
        "upload.meta.duration",
        "fail" if abs(int(meta.get("durationMs") or 0) - duration_ms) > max(
            500, int(duration_ms * 0.01)) else "pass",
        f"meta.durationMs={meta.get('durationMs')} vs {duration_ms}"))
    platform = meta.get("platform") or {}
    checks.append(Check(
        "upload.meta.platform",
        "pass" if isinstance(platform, dict)
        and str(platform.get("os") or "") == "android" else "fail",
        json.dumps(platform, ensure_ascii=False) if isinstance(platform, dict)
        else str(platform)))
    device = meta.get("device") or {}
    checks.append(Check(
        "upload.meta.device",
        "pass" if isinstance(device, dict) and device.get("model") else "fail",
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
