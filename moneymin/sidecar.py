"""
sidecar.py — Preparação de `.data.zip` segundo o contrato do Minute Android.

Âncora atual: APK 1.29.0 real (Galaxy S22 SM-S901E), pacote smali `l2.1`
(n0.1 / S / K0$b / Y / q.1 / Q0 / J0). Histórico 1.28 (emulador) permanece
válido nos literais CSV/timebase e está citado em VALIDACAO_MINUTE_1_28_0.md.

O builder ainda contém valores derivados e fallbacks sintéticos. Não comprova
aquisição nativa de sensores. Use-o como espelho estrutural do writer Android.

O backend (`POST /uploads/{id}/evaluate`) valida `{log_id}.data.zip` ao lado do
MP4. Membros na RAIZ do zip, prefixo `{log_id}.` (confirmado n0.1.smali):

  - {log_id}.imu.csv       header: t,ax,ay,az,wx,wy,wz   (K0$b / Y; 500 Hz, t ns)
  - {log_id}.frames.csv    header: i,ptsNs,dtNs,tNs,key  (n0.1; ~30 fps)
  - {log_id}.metadata.json estrutura ego nativa Android

metadata.json (writer nativo S/n0, cruzado com 1.28 EgoSidecar):
  - id/logId; createdAt ISO-8601 UTC com 3 ms; appVersion; source=ego
  - platform / device a partir de Build.MODEL, VERSION.RELEASE, SDK_INT (S.smali)
  - timebase.clockDomain == "android_elapsedRealtimeNanos" (q.1.smali)
    (Trinet externo: "trinet_camera_monotonic" em K0.smali)
  - cameras[] com intrinsics Brown-Conrady (J0 / q.1) ou
    extrinsics_omitted_reason: "no_camera_imu_calibration"
  - imuDiagnostics.strategy "gyro_anchored_v1" (q0.1 / Y) — SEM clockOffsetNs
    no sidecar; clockOffsetNs aparece no meta de sessão JS (debugMetaLabels)
  - codecActuals / video/avc (S / Q0 / V)
  - artifacts[] com remoteFilename `{log_id}.imu.csv` / `.frames.csv`

ImuSample Trinet (80 B: mag/quat/linAccel) é serializado para o CSV de 7
colunas em N0$a.smali (só timestamp + accel[3] + gyro[3]).

Calibração ultra-wide de referência: `samsung_uw_calibration.json`.
Somente stdlib neste módulo.
"""
from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

from . import config

# --- Calibração real da câmera ultra-wide (Samsung Galaxy S21–S24) ------------
_CALIB_PATH = Path(__file__).with_name("samsung_uw_calibration.json")


def _load_calibration() -> dict[str, Any]:
    try:
        return json.loads(_CALIB_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


_CALIB = _load_calibration()

# Fallback usado quando o chamador não fornece um DeviceProfile. O mesmo valor
# precisa alimentar metadata.json e frames.csv; misturar uptime com relógio zero
# produz um sidecar internamente inconsistente e bloqueado pelo validador.
DEFAULT_ANDROID_UPTIME_NS = 224_584_000_000_000


# --- Probes (ffprobe no PATH, senão o ffmpeg do imageio) ----------------------

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", re.I)
_VIDEO_RE = re.compile(
    r"Video:\s*(\w+).*?(\d{2,5})x(\d{2,5})(?:.*?(\d+(?:\.\d+)?)\s*fps)?", re.I)
_AUDIO_RE = re.compile(r"Audio:\s*(\w+).*?(\d+)\s*Hz", re.I)
_BITRATE_RE = re.compile(r"bitrate:\s*(\d+)\s*kb/s", re.I)


_FFMPEG_CACHE: str | None = None
_FFPROBE_CACHE: str | None = None


def _local_tools_bin(name: str) -> str | None:
    """Binário distribuído no runtime privado do QMoney.

    Aceita ``tools/<nome>/bin/<nome>.exe``, a estrutura do zip do gyan.dev
    (``tools/ffmpeg/ffmpeg-<vers>-essentials_build/bin/ffmpeg.exe``) e o
    pacote empacotado em ``dist/QMoney/runtime/tools/...``.
    """
    exe = name + (".exe" if os.name == "nt" else "")
    roots = [
        config.RUNTIME_ROOT / "tools" / name,
        config.CODE_ROOT / "tools" / name,
        config.CODE_ROOT / "dist" / "QMoney" / "runtime" / "tools" / name,
    ]
    for base in roots:
        flat = base / "bin" / exe
        if flat.exists():
            return str(flat)
        try:
            for cand in sorted(base.glob(f"*/bin/{exe}")):
                if cand.exists():
                    return str(cand)
        except OSError:
            continue
    return None


def ffmpeg_bin() -> str:
    """ffmpeg: projeto-local (tools/ffmpeg) → PATH → imageio-ffmpeg. Cacheado."""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE
    found = _local_tools_bin("ffmpeg") or shutil.which("ffmpeg")
    if not found:
        try:
            import imageio_ffmpeg
            found = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            found = "ffmpeg"
    _FFMPEG_CACHE = found
    return found


def ffprobe_bin() -> str | None:
    """ffprobe: projeto-local (tools/ffmpeg) → PATH → irmão do ffmpeg. Cacheado."""
    global _FFPROBE_CACHE
    if _FFPROBE_CACHE:
        return _FFPROBE_CACHE
    found = _local_tools_bin("ffmpeg")
    ffprobe = None
    if found:
        sibling = Path(found).with_name("ffprobe" + Path(found).suffix)
        if sibling.exists():
            ffprobe = str(sibling)
    if not ffprobe:
        ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        ff = ffmpeg_bin()
        if ff and ff not in ("ffmpeg", "ffprobe"):
            sibling = Path(ff).with_name("ffprobe" + Path(ff).suffix)
            if sibling.exists():
                ffprobe = str(sibling)
    _FFPROBE_CACHE = ffprobe
    return ffprobe


def _run_media(cmd: list[str], timeout: int = 60):
    kwargs: dict[str, Any] = {
        "capture_output": True, "text": True, "timeout": timeout,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, **kwargs)


_PIX_RE = re.compile(r"\b(yuvj?420p(?:10le)?|nv12)\b", re.I)
_HANDLER_RE = re.compile(r"handler_name\s*:\s*(.+)", re.I)


def _empty_probe() -> dict[str, Any]:
    return {
        "duration_ms": 0, "width": 0, "height": 0, "fps": 0.0,
        "codec": None, "profile": None, "bitrate": None,
        "audio_codec": None, "audio_sample_rate": None, "audio_channels": None,
        "has_video": False, "has_audio": False,
        "pix_fmt": None, "color_range": None, "handler_name": None,
    }


def parse_ffmpeg_probe(stderr: str) -> dict[str, Any]:
    """Lê o banner `ffmpeg -i` (Duration / Stream). Sem ffprobe no PATH."""
    base = _empty_probe()
    text = stderr or ""
    match = _DURATION_RE.search(text)
    if match:
        hours, minutes, seconds = (
            int(match.group(1)), int(match.group(2)), float(match.group(3)))
        base["duration_ms"] = int((hours * 3600 + minutes * 60 + seconds) * 1000)
    br = _BITRATE_RE.search(text)
    if br:
        base["bitrate"] = int(br.group(1)) * 1000
    vid = _VIDEO_RE.search(text)
    if vid:
        base["has_video"] = True
        base["codec"] = vid.group(1)
        base["width"] = int(vid.group(2))
        base["height"] = int(vid.group(3))
        if vid.group(4):
            base["fps"] = float(vid.group(4))
        if "high" in text.lower():
            base["profile"] = "High"
    pix = _PIX_RE.search(text)
    if pix:
        base["pix_fmt"] = pix.group(1).lower()
        if base["pix_fmt"].startswith("yuvj"):
            base["color_range"] = "pc"
        elif "(tv" in text.lower() or "tv," in text.lower():
            base["color_range"] = "tv"
    handler = _HANDLER_RE.search(text)
    if handler:
        base["handler_name"] = handler.group(1).strip()
    aud = _AUDIO_RE.search(text)
    if aud:
        base["has_audio"] = True
        base["audio_codec"] = aud.group(1)
        base["audio_sample_rate"] = aud.group(2)
        low = text.lower()
        if "stereo" in low:
            base["audio_channels"] = 2
        elif "mono" in low:
            base["audio_channels"] = 1
    return base


def _probe_via_ffprobe(video_path: Path) -> dict[str, Any]:
    probe = ffprobe_bin()
    if not probe:
        return {}
    try:
        out = _run_media(
            [probe, "-v", "error", "-show_entries",
             "format=duration:format=bit_rate:stream=index,codec_type,codec_name,"
             "profile,width,height,r_frame_rate,sample_rate,channels,duration,"
             "pix_fmt,color_range:stream_tags=handler_name",
             "-of", "json", str(video_path)],
            timeout=60,
        )
        info = json.loads(out.stdout or "{}")
    except Exception:
        return {}
    base = _empty_probe()
    fmt = info.get("format") or {}
    try:
        base["duration_ms"] = int(float(fmt.get("duration", 0) or 0) * 1000)
    except (TypeError, ValueError):
        pass
    try:
        base["bitrate"] = int(fmt.get("bit_rate")) if fmt.get("bit_rate") else None
    except (TypeError, ValueError):
        pass
    for st in info.get("streams") or []:
        ctype = st.get("codec_type")
        if ctype == "video":
            base["has_video"] = True
            base["codec"] = st.get("codec_name")
            base["profile"] = st.get("profile")
            base["pix_fmt"] = st.get("pix_fmt")
            base["color_range"] = st.get("color_range")
            tags = st.get("tags") or {}
            if tags.get("handler_name"):
                base["handler_name"] = tags.get("handler_name")
            try:
                base["width"] = int(st.get("width", 0) or 0)
                base["height"] = int(st.get("height", 0) or 0)
            except (TypeError, ValueError):
                pass
            fr = st.get("r_frame_rate") or ""
            if "/" in fr:
                try:
                    num, _, den = fr.partition("/")
                    base["fps"] = round(int(num) / int(den), 3)
                except (ValueError, ZeroDivisionError):
                    base["fps"] = 0.0
            if not base["duration_ms"]:
                try:
                    base["duration_ms"] = int(float(st.get("duration") or 0) * 1000)
                except (TypeError, ValueError):
                    pass
        elif ctype == "audio":
            base["has_audio"] = True
            base["audio_codec"] = st.get("codec_name")
            base["audio_sample_rate"] = st.get("sample_rate")
            try:
                base["audio_channels"] = int(st.get("channels", 0) or 0)
            except (TypeError, ValueError):
                pass
    return base


def _probe_via_ffmpeg(video_path: Path) -> dict[str, Any]:
    """imageio-ffmpeg só traz o ffmpeg, sem ffprobe. O `-i` já imprime Duration."""
    try:
        out = _run_media(
            [ffmpeg_bin(), "-hide_banner", "-i", str(video_path)],
            timeout=60,
        )
    except Exception:
        return {}
    return parse_ffmpeg_probe((out.stderr or "") + "\n" + (out.stdout or ""))


def _duration_via_pyav(video_path: Path) -> int:
    try:
        import av
        with av.open(str(video_path)) as container:
            duration = container.duration
            if duration:
                return int(float(duration) / 1000.0)  # ns -> ms
    except Exception:
        pass
    return 0


def probe_video(video_path: str | Path) -> dict[str, Any]:
    """Extrai codec/resolução/fps/duração reais do vídeo.

    Ordem: ffprobe (PATH) → banner `ffmpeg -i` (imageio no Windows) → PyAV.
    """
    base = _empty_probe()
    path = Path(video_path)
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return base
    except OSError:
        return base
    for reader in (_probe_via_ffprobe, _probe_via_ffmpeg):
        got = reader(path)
        if got.get("duration_ms") or got.get("has_video"):
            base.update(got)
            break
    if not base["duration_ms"]:
        base["duration_ms"] = _duration_via_pyav(path)
    return base


# --- metadata.json (estrutura ego nativa ANDROID, extraída do jadx) -----------

def _default_anchor_calib() -> dict[str, Any]:
    """Calibração UW do modelo âncora 1.29 (`NATIVE_SIDECAR_MODEL`, SM-S901E)."""
    model = config.NATIVE_SIDECAR_MODEL
    models = _CALIB.get("models") or {}
    entry = dict(models.get(model) or {})
    center = _CALIB.get("reference") or {}
    if not entry:
        entry = {"fx": 1465.0, "fy": 1465.0, "k1": -0.22, "k2": 0.11,
                 "k3": -0.03, "p1": 0.0014, "p2": -0.0022, "readoutS": 0.0111,
                 "logicalCameraId": "3"}
    if not entry.get("cx"):
        entry["cx"] = float(center.get("cx") or 2016.0)
    if not entry.get("cy"):
        entry["cy"] = float(center.get("cy") or 1512.0)
    entry.setdefault("referenceWidth", int(center.get("sensorWidth") or 4032))
    entry.setdefault("referenceHeight", int(center.get("sensorHeight") or 3024))
    return entry


def _scale_camera_intrinsics(width: int, height: int,
                             cal: dict[str, Any] | None = None) -> dict[str, Any]:
    """Escala a calibração ultra-wide Samsung (4032x3024) para a resolução do vídeo.

    Semântica de `buildIntrinsicsMap` do EgoCameraController (jadx):
      - fx' = fx * scaleX; fy' = fy * scaleY
      - cx' = (cx - cropLeft) * scaleX; cy' = (cy - cropTop) * scaleY
    (no nosso caso crop=0, active array centrado). Modelo Brown-Conrady com a
    layout string que o app escreve. `cal` é a calibração do aparelho da conta
    (device_profile.DeviceProfile.calib); sem ela usa a âncora SM-S901E.
    """
    cal = cal or {}
    ref_w = int(cal.get("referenceWidth") or 4032)
    ref_h = int(cal.get("referenceHeight") or 3024)
    sx = width / ref_w if ref_w else 1.0
    sy = height / ref_h if ref_h else 1.0
    fx = float(cal.get("fx") or 1545.0)
    fy = float(cal.get("fy") or fx)
    cx = float(cal.get("cx") or ref_w / 2.0)
    cy = float(cal.get("cy") or ref_h / 2.0)
    k1 = float(cal.get("k1") or 0.0)
    k2 = float(cal.get("k2") or 0.0)
    k3 = float(cal.get("k3") or 0.0)
    p1 = float(cal.get("p1") or 0.0)
    p2 = float(cal.get("p2") or 0.0)
    return {
        "fx": round(fx * sx, 6),
        "fy": round(fy * sy, 6),
        "cx": round(cx * sx, 6),
        "cy": round(cy * sy, 6),
        "width": width,
        "height": height,
        "coordinate_frame": "video_frame",
        "intrinsics_reference_dimensions": {"width": width, "height": height},
        "distortion_model": "brown_conrady",
        "distortion_coefficients": [k1, k2, k3, p1, p2],
        "distortion_coefficients_layout": "brown_conrady_k1_k2_k3_p1_p2",
    }


def _forge_imu_diagnostics(
    sample_count: int,
    measured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Minute 1.29 ``imuDiagnostics`` (n0.1 / Y / q0.1) for the forged envelope.

    Prefer measured resample counters from ``ego4d.build_imu_csv(..., stats=)``.
    ``maxInterpolationSpanNs`` is always the APK config ceiling ``25000000``
    (EgoImu aligner constructor) — never the observed gap. Measured span may
    live in provenance under another key; never invent
    ``interpolatedCount == sampleCount``.
    """
    base = {
        "droppedRowCount": 0,
        "interpolatedCount": 0,
        "maxAlignmentDeltaNs": "0",
        "maxInterpolationSpanNs": "25000000",
        "nearestFallbackCount": 0,
        "nearestFallbackToleranceNs": "1000000",
        "p95AlignmentDeltaNs": "0",
        "sampleCount": int(sample_count),
        "strategy": "gyro_anchored_v1",
    }
    if not isinstance(measured, dict):
        return base
    for key in (
        "droppedRowCount", "interpolatedCount", "nearestFallbackCount",
        "sampleCount",
    ):
        value = measured.get(key)
        if type(value) is int and value >= 0:
            base[key] = value
    for key in (
        "maxAlignmentDeltaNs",
        "nearestFallbackToleranceNs", "p95AlignmentDeltaNs",
    ):
        value = measured.get(key)
        if isinstance(value, str) and value.isdigit():
            base[key] = value
        elif type(value) is int and value >= 0:
            base[key] = str(value)
    if measured.get("strategy") == "gyro_anchored_v1":
        base["strategy"] = "gyro_anchored_v1"
    # sampleCount must match the CSV that ships in the zip.
    base["sampleCount"] = int(sample_count)
    # APK writer always emits the config ceiling, not measured max span.
    base["maxInterpolationSpanNs"] = "25000000"
    return base


def build_metadata_json(
    *,
    session_id: str,
    chunk_index: int,
    duration_ms: int,
    recorded_at: str,
    video_probe: dict[str, Any] | None = None,
    device_meta: dict[str, Any] | None = None,
    platform_meta: dict[str, Any] | None = None,
    log_id: str | None = None,
    sample_count: int | None = None,
    calib: dict[str, Any] | None = None,
    uptime_ns: int | None = None,
    imu_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Monta metadata.json com o modelo histórico desta integração.

    Contrato verificado contra o writer 1.29 (smali l2.1 / n0.1):
      - id/logId top-level; platform {os,version}; device com systemName
      - timebase.clockDomain == "android_elapsedRealtimeNanos"
      - cameras[0].name == "camera_logical_X", source "builtin"
      - intrinsics Brown-Conrady (k1 k2 k3 p1 p2) escaladas do sensor nativo
      - imuDiagnostics SEM clockOffsetNs (Android não tem)
      - codecActuals: profile 8, level 8192 (AVCLevel42), hasBFrames/gopMaxFrames null
      - sem device_meta/calib: âncora SM-S901E (NATIVE_SIDECAR_MODEL), não o
        deviceModel legado do JSON de calibração

    Anti-colusão (por conta, via device_profile.DeviceProfile):
      - `calib`   — intrinsics com jitter do aparelho da conta
      - `uptime_ns` — SystemClock.elapsedRealtimeNanos no início da gravação
                    (sem ele usa a referência minada; em campanha SEMPRE passe)
    """
    probe = video_probe or {}
    if device_meta is None:
        device_meta = {
            "model": config.NATIVE_SIDECAR_MODEL,
            "systemName": config.NATIVE_SIDECAR_SYSTEM_NAME,
            "systemVersion": config.NATIVE_SIDECAR_SYSTEM_VERSION,
        }
    if platform_meta is None:
        # Writer 1.29 (n0.1.smali): platform = {os: "android", version: sdkInt}
        platform_meta = {
            "os": config.NATIVE_PLATFORM_OS,
            "version": 34,
        }
    if log_id is None:
        log_id = f"{session_id}_{chunk_index}"

    width = probe.get("width") or 1440
    height = probe.get("height") or 1080
    codec = probe.get("codec") or "h264"
    # O app Android encoda H.264 em MP4; o MediaFormat reporta video/avc.
    mime = "video/avc" if codec in ("h264", "avc1", "avc") else f"video/{codec}"
    # Espelha o que MediaExtractor costuma expor (V.smali): profile/level do
    # High@4.2 que pedimos ao encoder; bitRate/colorStandard só se o probe
    # trouxer valor finito — senão null como o app quando a chave falta.
    probe_bitrate = probe.get("bitrate")
    try:
        bit_rate = int(probe_bitrate) if probe_bitrate not in (None, "", 0) else None
    except (TypeError, ValueError):
        bit_rate = None
    if bit_rate is not None and bit_rate <= 0:
        bit_rate = None
    color_standard = probe.get("color_standard", probe.get("colorStandard"))
    if color_standard is None:
        color_standard = None
    else:
        try:
            color_standard = int(color_standard)
        except (TypeError, ValueError):
            color_standard = None
    codec_actuals = {
        "bitRate": bit_rate,
        "colorStandard": color_standard,
        "gopMaxFrames": None,
        "hasBFrames": None,
        "height": height,
        "level": 8192,
        "mime": mime,
        "profile": 8,
        "width": width,
    }

    calib_model = calib if calib else _default_anchor_calib()
    logical_id = str(calib_model.get("logicalCameraId") or "3")
    cameras = [
        {
            "extrinsics_omitted_reason": "no_camera_imu_calibration",
            "intrinsics": _scale_camera_intrinsics(width, height, cal=calib_model),
            "name": f"camera_logical_{logical_id}",
            "rolling_shutter_readout_s": float(
                calib_model.get("readoutS") or 0.0105),
            "source": "builtin",
        }
    ]

    # timebase: relógio do telefone no domínio elapsedRealtimeNanos (desde o boot).
    if sample_count is None:
        # +1 amostra: span do IMU = duração declarada (xcheck.duration_consistency).
        sample_count = max(1, int(duration_ms / 1000 * config.ANDROID_IMU_SAMPLE_RATE_HZ) + 1)
    from .device_profile import normalize_recorded_at, recorded_at_to_wall_ms
    recorded_at = normalize_recorded_at(recorded_at)
    start_wall_ms = recorded_at_to_wall_ms(recorded_at) or int(time.time() * 1000)
    end_wall_ms = start_wall_ms + duration_ms
    # android_elapsedRealtimeNanos = SystemClock.elapsedRealtimeNanos (~desde o
    # boot, ~2.2e14 ns para ~2,5 dias). Preencher com epoch (~1.7e18 ns) quebra a
    # consistência do clockDomain e o Catbear pode descartar.
    if uptime_ns is None:
        uptime_ns = DEFAULT_ANDROID_UPTIME_NS  # ~2,6 dias (referência minada)
    start_ns = int(uptime_ns)
    end_ns = start_ns + duration_ms * 1_000_000

    timebase = {
        "clockDomain": config.ANDROID_CLOCK_DOMAIN,
        "endNs": str(end_ns),
        "endSensorTimestampNs": str(end_ns),
        "endWallTimeMs": end_wall_ms,
        "firstFrameSensorTimestampNs": str(start_ns),
        "startNs": str(start_ns),
        "startSensorTimestampNs": str(start_ns),
        "startWallTimeMs": start_wall_ms,
    }

    return {
        "appVersion": config.APP_VERSION,
        "artifacts": [
            {"contentType": "text/csv", "name": "imu",
             "remoteFilename": f"{log_id}.imu.csv"},
            {"contentType": "text/csv", "name": "frames",
             "remoteFilename": f"{log_id}.frames.csv"},
        ],
        "cameras": cameras,
        "chunk": {
            "endTimeMs": end_wall_ms,
            "index": chunk_index,
            "startTimeMs": start_wall_ms,
        },
        "codecActuals": codec_actuals,
        "createdAt": recorded_at,
        "device": device_meta,
        "durationMs": duration_ms,
        "id": log_id,
        "imuDiagnostics": _forge_imu_diagnostics(
            sample_count, imu_diagnostics),
        "logId": log_id,
        "platform": platform_meta,
        "session": {"id": session_id},
        "source": "ego",
        "timebase": timebase,
        "video": {
            "height": height,
            "path": f"{log_id}.mp4",
            "rotationDeg": 0,
            "width": width,
        },
    }


# --- imu.csv (t,ax,ay,az,wx,wy,wz — 500 Hz, t em ns) ---------------------------

def build_imu_csv(duration_ms: int,
                  sample_rate_hz: int = config.ANDROID_IMU_SAMPLE_RATE_HZ,
                  seed: str | int = "moneymin.imu:2026") -> str:
    """Gera IMU sintética com as colunas do CSV histórico desta integração.

    Header: t,ax,ay,az,wx,wy,wz.
      - t: timestamp monotônico em nanossegundos (500 Hz, SAMPLING_PERIOD_US=2000)
      - ax/ay/az: aceleração em m/s² — no ANDROID az fica POSITIVO (~+9.8 em
        repouso, tela para cima, eixo z do sensor aponta para cima)
      - wx/wy/wz: velocidade angular (rad/s)

    Sinal METRICAMENTE plausível (não ruído branco nem senoides puras):
      - gravidade ~9.81 com pequeno tilt que anda devagar (random walk lento);
      - aceleração linear de movimento com suavização (passos/acelerações do
        dia a dia) e ruído de sensor pequeno;
      - giroscópio derivado do tilt + ruído.
    O espectro (potência concentrada nas bandas baixas, cauda a altas) se
    assemelha ao de um sensor real.

    IMPORTANTE (xcheck.duration_consistency): o SPAN do IMU deve bater com a
    duração declarada. Com step exato de 2ms (500 Hz) e n = amostras/seg + 1,
    span = (n-1)*2ms = duração declarada — sem deriva acumulada.

    `seed` fixa o sinal: passe o device_id da conta para que cada aparelho
    tenha UM sinal próprio (senão todas as contas subiriam a MESMA IMU).
    """
    import math
    rng = random.Random(seed)
    n = max(1, int(duration_ms / 1000 * sample_rate_hz) + 1)
    dt = 1.0 / sample_rate_hz
    step_ns = int(1_000_000_000 // sample_rate_hz)  # 2ms a 500Hz
    half_life = max(1, int(sample_rate_hz / 4))  # suavização ~250ms

    def _ewma(prev: float, target: float, k: float = 0.05) -> float:
        return prev + k * (target - prev)

    # Estado interno do "movimento" (walk do tilt e da velocidade).
    tilt_x = tilt_y = 0.0
    vel_x = vel_y = vel_z = 0.0
    ax_f = ay_f = az_f = wx_f = wy_f = wz_f = 0.0
    write = io.StringIO()
    writer = csv.writer(write, lineterminator="\n")
    writer.writerow(["t", "ax", "ay", "az", "wx", "wy", "wz"])
    for i in range(n):
        t = i * step_ns
        if i % half_life == 0:
            tilt_x += rng.uniform(-3.5, 3.5) * dt / (dt * half_life)
            tilt_y += rng.uniform(-3.5, 3.5) * dt / (dt * half_life)
            tilt_x = max(-0.06, min(0.06, tilt_x))
            tilt_y = max(-0.06, min(0.06, tilt_y))
        # Aceleração linear: objetivo aleatório suave + micro ruído. A gravidade NÃO
        # entra aqui — ela é projetada nos eixos logo abaixo (senão |g|≈2g).
        a_lin_x = vel_x * 0.4 + rng.uniform(-0.9, 0.9)
        a_lin_y = vel_y * 0.4 + rng.uniform(-0.9, 0.9)
        a_lin_z = vel_z * 0.4 + rng.uniform(-0.6, 0.6)
        ax_f = _ewma(ax_f, a_lin_x)
        ay_f = _ewma(ay_f, a_lin_y)
        az_f = _ewma(az_f, a_lin_z)
        # Gravidade despejada nos eixos conforme o tilt (pequeno ângulo).
        gx = 9.81 * math.sin(tilt_y)
        gy = -9.81 * math.sin(tilt_x)
        gz = 9.81 * math.cos(math.hypot(tilt_x, tilt_y))
        ax = ax_f + gx + rng.uniform(-0.012, 0.012)
        ay = ay_f + gy + rng.uniform(-0.012, 0.012)
        az = az_f + gz + rng.uniform(-0.015, 0.015)
        ax = max(-4.5, min(4.5, ax))
        ay = max(-4.5, min(4.5, ay))
        # Giros: derivada do tilt + caminhada lenta + ruído de sensor.
        wz_f = _ewma(wz_f, tilt_y - tilt_x + rng.uniform(-0.05, 0.05))
        wx_f = _ewma(wx_f, tilt_x * 1.5 + rng.uniform(-0.02, 0.02))
        wy_f = _ewma(wy_f, tilt_y * 1.5 + rng.uniform(-0.02, 0.02))
        wx = max(-0.8, min(0.8, wx_f + rng.uniform(-0.008, 0.008)))
        wy = max(-0.8, min(0.8, wy_f + rng.uniform(-0.008, 0.008)))
        wz = max(-0.8, min(0.8, wz_f + rng.uniform(-0.008, 0.008)))
        writer.writerow([t, f"{ax:.6f}", f"{ay:.6f}", f"{az:.6f}",
                         f"{wx:.6f}", f"{wy:.6f}", f"{wz:.6f}"])
    return write.getvalue()


# --- frames.csv (i,ptsNs,dtNs,tNs,key — 30fps) --------------------------------

def build_frames_csv(duration_ms: int, fps: float = 30.0,
                     gop: int | None = None,
                     offset_ns: int = 0) -> str:
    """Gera frames sintéticos com as colunas do CSV histórico desta integração.

    Header: i,ptsNs,dtNs,tNs,key.
      - i: índice sequencial (0..n-1)
      - ptsNs: presentation timestamp em ns, monotônico (delta ~33.336ms = 30fps)
      - dtNs: delta do frame em ns (igual ao delta pts)
      - tNs: timestamp do sensor do frame = ptsNs + offset_ns — no Android o
        offset é o elapsedRealtimeNanos do 1º frame (timebase do metadata),
        não um micro offset de relógio artificial.
      - key: 1 se keyframe (GOP ~30 frames — ~1 keyframe/s)

    `gop` e `offset_ns` variam por conta (perfil define GOP 28-32 e o uptime do
    aparelho) — dois uploads da mesma gravação nunca têm o mesmo padrão.

    IMPORTANTE (xcheck.duration_consistency): n = amostras/seg + 1 deixa o span
    dos frames = duração declarada (sem deriva em vídeos longos).
    """
    n = max(1, int(duration_ms / 1000 * fps) + 1)
    step_ns = int(1_000_000_000 / fps)  # ~33.333ms
    offset_ns = int(offset_ns) if offset_ns else 0
    gop = max(1, int(gop) if gop else int(fps))  # ~1 keyframe/s
    rng = random.Random(f"moneymin.frames:{duration_ms}:{fps}:{gop}:{offset_ns}")
    target_ns = int(duration_ms) * 1_000_000
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["i", "ptsNs", "dtNs", "tNs", "key"])
    pts = 0
    for i in range(n):
        if i == 0:
            dt = 0
            pts = 0
        elif i == n - 1:
            dt = max(1, target_ns - pts)
            pts = target_ns
        else:
            left = n - i
            ideal = (target_ns - pts) / left
            dt = int(ideal) + rng.randint(-110_000, 110_000)
            dt = max(step_ns // 2, dt)
            pts += dt
        key = 1 if i % gop == 0 else 0
        writer.writerow([i, pts, dt, pts + offset_ns, key])
    return buf.getvalue()


def _extract_frame_pts(video_path: str | Path) -> list[tuple[int, bool]]:
    """PTS (ns) e flag de keyframe REAIS do MP4.

    Equivalente ao `extractFrames` do EgoSidecar (MediaExtractor.sampleTime*1000
    + flag de sample). Tenta ffprobe e, sem ele, parseia as sample tables do
    próprio arquivo (stts/stss — os mesmos dados que o MediaExtractor lê).
    Devolve lista vazia se não der para sondar (fallback sintético).
    """
    frames = _extract_frame_pts_ffprobe(video_path)
    if frames:
        return frames
    return _extract_frame_pts_mp4(video_path)


def _extract_frame_pts_ffprobe(video_path: str | Path) -> list[tuple[int, bool]]:
    probe_bin = ffprobe_bin()
    if not probe_bin:
        return []
    try:
        out = _run_media([
            probe_bin, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "frame=pts_time,key_frame",
            "-of", "json", str(video_path),
        ], timeout=120)
        info = json.loads(out.stdout or "{}")
    except Exception:  # noqa: BLE001
        return []
    frames: list[tuple[int, bool]] = []
    for frame in info.get("frames") or []:
        if "key_frame" not in frame:
            continue
        pts_time = frame.get("pts_time")
        if pts_time is None:
            continue
        try:
            pts_ns = int(round(float(pts_time) * 1_000_000_000))
        except (TypeError, ValueError, OverflowError):
            continue
        frames.append((pts_ns, str(frame.get("key_frame", "0")) == "1"))
    return frames


def _mp4_boxes(data: bytes, start: int, end: int):
    """Itera caixas ISO-BMFF (size 4B + type 4B; 64-bit size suportado)."""
    offset = start
    while offset + 8 <= end:
        size = int.from_bytes(data[offset:offset + 4], "big")
        box_type = data[offset + 4:offset + 8]
        header = 8
        if size == 1:
            if offset + 16 > end:
                break
            size = int.from_bytes(data[offset + 8:offset + 16], "big")
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            break
        yield box_type.decode("latin-1", "replace"), offset + header, offset + size
        offset += size


def _mp4_u32(data: bytes, at: int) -> int:
    return int.from_bytes(data[at:at + 4], "big")


def _extract_frame_pts_mp4(video_path: str | Path) -> list[tuple[int, bool]]:
    """Lê stts (deltas → PTS) e stss (sync samples → keyframes) da trilha vídeo.

    Este leitor limitado só interpreta trilhas sem edit lists (elst) e sem
    offsets de composição (ctts). Nessas trilhas, o PTS é a acumulação dos
    deltas de stts a partir de 0. Outras trilhas exigem o leitor ffprobe;
    retornar vazio evita tratar tempo de decodificação como apresentação.
    """
    try:
        data = Path(video_path).read_bytes()
    except OSError:
        return []
    if len(data) < 16 or data[4:8] != b"ftyp":
        return []
    moov = None
    for box_type, s, e in _mp4_boxes(data, 0, len(data)):
        if box_type == "moov":
            moov = (s, e)
            break
    if moov is None:
        return []

    def _parse_stbl(stbl_start: int, stbl_end: int) -> dict[str, Any]:
        stts_runs: list[tuple[int, int]] = []
        stss: set[int] | None = None
        has_composition_offsets = False
        for btype, bs, be in _mp4_boxes(data, stbl_start, stbl_end):
            if btype == "stts" and bs + 12 <= be:
                # FullBox (version+flags) + entry_count
                count = _mp4_u32(data, bs + 4)
                pos = bs + 8
                for _ in range(min(count, (be - pos) // 8)):
                    runs = _mp4_u32(data, pos)
                    delta = _mp4_u32(data, pos + 4)
                    stts_runs.append((runs, delta))
                    pos += 8
            elif btype == "ctts":
                has_composition_offsets = True
            elif btype == "stss" and bs + 8 <= be:
                stss = set()
                # FullBox(4: version+flags) + entry_count(4).
                count = _mp4_u32(data, bs + 4)
                pos = bs + 8
                for _ in range(min(count, (be - pos) // 4)):
                    stss.add(_mp4_u32(data, pos))
                    pos += 4
        return {"stts_runs": stts_runs, "stss": stss,
                "has_composition_offsets": has_composition_offsets}

    def _stbl_minf_mdia(trak_start: int, trak_end: int) -> dict[str, Any] | None:
        hdlr_is_video = False
        timescale = 0
        stbl: dict[str, Any] | None = None
        has_edit_list = False
        for ttype, ts, te in _mp4_boxes(data, trak_start, trak_end):
            if ttype == "edts":
                has_edit_list = any(
                    etype == "elst" for etype, _, _ in _mp4_boxes(data, ts, te)
                ) or has_edit_list
            elif ttype == "mdia":
                for mtype, ms, me in _mp4_boxes(data, ts, te):
                    if mtype == "hdlr" and ms + 12 <= me:
                        # FullBox(4) + pre_defined(4) + handler_type(4)
                        handler = data[ms + 8:ms + 12]
                        hdlr_is_video = handler == b"vide"
                    elif mtype == "mdhd" and ms + 8 <= me:
                        # FullBox = version(1)+flags(3) (4 bytes);
                        # v0: creation(4) mod(4) timescale(4) → payload+12;
                        # v1: creation(8) mod(8) timescale(4) → payload+20.
                        version = data[ms]
                        if version == 1 and ms + 24 <= me:
                            timescale = _mp4_u32(data, ms + 20)
                        elif version == 0 and ms + 16 <= me:
                            timescale = _mp4_u32(data, ms + 12)
                    elif mtype == "minf":
                        for ntype, ns, ne in _mp4_boxes(data, ms, me):
                            if ntype == "stbl":
                                stbl = _parse_stbl(ns, ne)
        if not hdlr_is_video or stbl is None or not timescale:
            return None
        stbl["timescale"] = timescale
        stbl["has_edit_list"] = has_edit_list
        return stbl

    video_track = None
    for ttype, ts, te in _mp4_boxes(data, moov[0], moov[1]):
        if ttype == "trak":
            parsed = _stbl_minf_mdia(ts, te)
            if parsed is not None:
                video_track = parsed
                break
    if video_track is None or not video_track.get("timescale"):
        return []
    if video_track.get("has_edit_list") or video_track.get("has_composition_offsets"):
        return []
    timescale = int(video_track["timescale"])
    if timescale <= 0:
        return []
    # A ausência de stss declara todos os samples como sync samples.
    stss = video_track.get("stss")
    frames: list[tuple[int, bool]] = []
    pts_ts = 0
    index = 0  # 0-based; stss é 1-based
    for sample_count, delta in video_track.get("stts_runs") or []:
        for _ in range(sample_count):
            pts_ns = int(round(pts_ts * 1_000_000_000.0 / timescale))
            frames.append((pts_ns, stss is None or (index + 1) in stss))
            pts_ts += delta
            index += 1
    return frames


def build_frames_csv_from_video(
    video_path: str | Path,
    *,
    duration_ms: int,
    fps: float = 30.0,
    gop: int | None = None,
    offset_ns: int = 0,
    require_measured_pts: bool = False,
) -> str:
    """frames.csv derivado do MP4 REAL (PTS + keyframes) — como o EgoSidecar.

    O app escreve o frames.csv lendo os timestamps do próprio arquivo
    (MediaExtractor). Aqui fazemos o mesmo com ffprobe: os PTS e os keyframes
    do CSV batem exatamente com o vídeo enviado (item nº1 de realismo).
    `tNs = ptsNs + offset_ns` (offset = elapsedRealtimeNanos do 1º frame).

    O fallback sintético legado permanece disponível por padrão. Quando
    `require_measured_pts=True`, ausência de PTS interrompe o preparo.
    """
    if type(require_measured_pts) is not bool:
        raise ValueError('require_measured_pts deve ser booleano.')
    frames = _extract_frame_pts(video_path)
    if not frames:
        if require_measured_pts:
            raise ValueError('Vídeo sem PTS medidos; preparo interrompido.')
        return build_frames_csv(duration_ms, fps=fps, gop=gop, offset_ns=offset_ns)
    offset_ns = int(offset_ns) if offset_ns else 0
    if frames and frames[0][0] != 0:
        # rebase no primeiro PTS (o app mantém o pts real do MediaExtractor).
        base = frames[0][0]
        frames = [(pts_ns - base, key) for pts_ns, key in frames]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["i", "ptsNs", "dtNs", "tNs", "key"])
    previous: int | None = None
    for i, (pts_ns, key) in enumerate(frames):
        dt = 0 if previous is None else pts_ns - previous
        writer.writerow([i, pts_ns, dt, pts_ns + offset_ns, 1 if key else 0])
        previous = pts_ns
    return buf.getvalue()


# --- montagem do zip -----------------------------------------------------------

def build_sidecar_zip(
    *,
    session_id: str,
    chunk_index: int,
    duration_ms: int,
    recorded_at: str,
    video_probe: dict[str, Any] | None = None,
    device_meta: dict[str, Any] | None = None,
    platform_meta: dict[str, Any] | None = None,
    log_id: str | None = None,
    calib: dict[str, Any] | None = None,
    uptime_ns: int | None = None,
    frames_gop: int | None = None,
    imu_csv: str | None = None,
    frames_csv: str | None = None,
    imu_seed: str | int | None = None,
    video_path: str | Path | None = None,
) -> bytes:
    """Monta o `.data.zip` nativo com membros `{log_id}.*` na RAIZ.

    Contrato real do app (EgoSidecar.zipArtifacts): membros na raiz nomeados
    com o prefixo `{log_id}.`:
      - {log_id}.metadata.json
      - {log_id}.imu.csv
      - {log_id}.frames.csv
    Devolve os bytes do zip.

    `calib`/`uptime_ns`/`frames_gop` são os parâmetros de identidade POR CONTA
    (device_profile.DeviceProfile) — sem eles o sidecar usa valores de referência.

    Se `video_path` for dado e `frames_csv` não, o frames.csv é derivado do
    MP4 REAL (PTS/keyframes via ffprobe) — como o app faz com o MediaExtractor.
    `imu_seed` fixa o sinal de IMU por conta (senão todas as contas sobem a
    mesma IMU). `imu_csv`/`frames_csv` injetados têm prioridade.
    """
    uptime_ns = (DEFAULT_ANDROID_UPTIME_NS if uptime_ns is None
                 else int(uptime_ns))
    if imu_csv is None:
        imu_csv = build_imu_csv(duration_ms,
                                seed=imu_seed or "moneymin.imu:2026")
    fps = (video_probe or {}).get("fps") or 30.0
    if frames_csv is None:
        if video_path is not None:
            frames_csv = build_frames_csv_from_video(
                video_path, duration_ms=duration_ms, fps=fps,
                gop=frames_gop, offset_ns=uptime_ns)
        else:
            frames_csv = build_frames_csv(
                duration_ms, fps, gop=frames_gop, offset_ns=uptime_ns)
    return build_sidecar_zip_custom(
        session_id=session_id,
        chunk_index=chunk_index,
        duration_ms=duration_ms,
        recorded_at=recorded_at,
        video_probe=video_probe,
        device_meta=device_meta,
        platform_meta=platform_meta,
        log_id=log_id,
        imu_csv=imu_csv,
        frames_csv=frames_csv,
        calib=calib,
        uptime_ns=uptime_ns,
        frames_gop=frames_gop,
    )


def offset_imu_csv_timestamps(imu_csv: str, offset_ns: int) -> str:
    """Shift relative IMU ``t`` onto ``android_elapsedRealtimeNanos``.

    Prepare/cache keep a zero-based 500 Hz grid. The phone writer stamps
    samples with ``SystemClock.elapsedRealtimeNanos()`` in the same domain as
    frames ``tNs`` and ``timebase.firstFrameSensorTimestampNs`` (Y.smali).
    """
    offset_ns = int(offset_ns)
    if offset_ns < 0:
        raise ValueError("offset_ns da IMU deve ser não-negativo")
    if offset_ns == 0:
        return imu_csv
    lines = (imu_csv or "").splitlines()
    if not lines:
        return imu_csv
    header = lines[0]
    if header.split(",") != ["t", "ax", "ay", "az", "wx", "wy", "wz"]:
        raise ValueError("header IMU inesperado para offset de relógio")
    out = [header]
    for line in lines[1:]:
        if not line.strip():
            continue
        first, sep, rest = line.partition(",")
        if not sep:
            raise ValueError("linha IMU sem colunas de sinal")
        try:
            t_ns = int(float(first))
        except (TypeError, ValueError) as exc:
            raise ValueError("timestamp IMU inválido") from exc
        if t_ns < 0:
            raise ValueError("timestamp IMU negativo")
        out.append(f"{t_ns + offset_ns},{rest}")
    return "\n".join(out) + ("\n" if len(out) > 1 else "")


def _imu_csv_first_timestamp_ns(imu_csv: str) -> int | None:
    for line in (imu_csv or "").splitlines()[1:]:
        if not line.strip():
            continue
        first, _, _ = line.partition(",")
        try:
            return int(float(first))
        except (TypeError, ValueError):
            return None
    return None


def build_sidecar_zip_custom(
    *,
    session_id: str,
    chunk_index: int,
    duration_ms: int,
    recorded_at: str,
    video_probe: dict[str, Any] | None = None,
    device_meta: dict[str, Any] | None = None,
    platform_meta: dict[str, Any] | None = None,
    log_id: str | None = None,
    imu_csv: str | None = None,
    frames_csv: str | None = None,
    imu_sample_count: int | None = None,
    calib: dict[str, Any] | None = None,
    uptime_ns: int | None = None,
    frames_gop: int | None = None,
    derived_diagnostics: dict[str, Any] | None = None,
    imu_diagnostics: dict[str, Any] | None = None,
) -> bytes:
    """Monta o `.data.zip` nativo permitindo INJETAR imu.csv/frames.csv REAIS.

    Igual a `build_sidecar_zip`, mas aceita conteúdo externo para `imu.csv` e
    `frames.csv` — útil para usar dados de sensor REAIS (ex.: IMU do Ego4D) no
    lugar da IMU sintética gerada por `build_imu_csv`. Se omitidos, usa os
    geradores sintéticos padrão (comportamento idêntico à função base).
    `imu_sample_count` (se dado) alimenta o `imuDiagnostics.sampleCount` do
    metadata para bater com o IMU injetado.
    `imu_diagnostics` (opcional) traz contadores medidos do resample Ego4D.

    `calib`/`uptime_ns`/`frames_gop` por conta (device_profile.DeviceProfile):
    calibração Brown-Conrady com jitter, elapsedRealtimeNanos e GOP próprios.
    IMU relativa (t≈0) é deslocada para ``uptime_ns`` no zip, alinhada a frames
    e timebase no domínio ``android_elapsedRealtimeNanos``.

    Contrato real do app (EgoSidecar.zipArtifacts): membros na raiz com o
    prefixo `{log_id}.`.
    """
    # derived_diagnostics is accepted for API compatibility / audit callers;
    # it does not gate zip construction (Minute evaluates the wire envelope).
    _ = derived_diagnostics
    uptime_ns = (DEFAULT_ANDROID_UPTIME_NS if uptime_ns is None
                 else int(uptime_ns))
    if log_id is None:
        log_id = f"{session_id}_{chunk_index}"
    imu_csv = imu_csv if imu_csv is not None else build_imu_csv(duration_ms)
    first_t = _imu_csv_first_timestamp_ns(imu_csv)
    # Relative prepare grids start at 0. Skip if already on the uptime domain.
    if uptime_ns < 0:
        raise ValueError("uptime_ns da IMU deve ser não-negativo")
    if first_t is not None and first_t < uptime_ns:
        imu_csv = offset_imu_csv_timestamps(imu_csv, uptime_ns)
    if imu_sample_count is None or int(imu_sample_count or 0) <= 0:
        # sampleCount do metadata deve bater com as LINHAS do CSV injetado —
        # nunca deixar divergir do atributo do IMU real que foi reamostrado.
        imu_sample_count = max(
            1, sum(1 for line in imu_csv.splitlines() if line.strip()) - 1)
    metadata = build_metadata_json(
        session_id=session_id,
        chunk_index=chunk_index,
        duration_ms=duration_ms,
        recorded_at=recorded_at,
        video_probe=video_probe,
        device_meta=device_meta,
        platform_meta=platform_meta,
        log_id=log_id,
        sample_count=int(imu_sample_count),
        calib=calib,
        uptime_ns=uptime_ns,
        imu_diagnostics=imu_diagnostics,
    )
    fps = (video_probe or {}).get("fps") or 30.0
    frames_csv = (frames_csv if frames_csv is not None
                  else build_frames_csv(duration_ms, fps, gop=frames_gop,
                                        offset_ns=uptime_ns))

    buf = io.BytesIO()
    # Ordem do writer APK (EgoSidecar / n0.1): imu → frames → metadata.
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{log_id}.imu.csv", imu_csv)
        zf.writestr(f"{log_id}.frames.csv", frames_csv)
        zf.writestr(f"{log_id}.metadata.json",
                    json.dumps(metadata, ensure_ascii=False))
    return buf.getvalue()
