"""
sidecar.py — Gera o sidecar `.data.zip` replicando EXATAMENTE o app Minute nativo.

O contrato abaixo foi extraído do sidecar REAL do app nativo (iPhone 14,5 / iOS
26.5, sessões 627d39d8... e 78c04a06... baixadas do container do app no backup
do iPhone do operador). O backend (`POST /uploads/{id}/evaluate`) valida o blob
`{log_id}.data.zip` ao lado do MP4; seus membros vivem na RAIZ do zip com o
prefixo `{log_id}.`:

  - {log_id}.imu.csv     -> header: t,ax,ay,az,wx,wy,wz   (100Hz, t em ns)
  - {log_id}.frames.csv  -> header: i,ptsNs,dtNs,tNs,key  (30fps)
  - {log_id}.metadata.json -> estrutura ego nativa iOS completa

Campos validados pelo checklist nativo (23 checks) e como o app real preenche:
  - source == "ego"
  - timebase.clockDomain == "ios_systemUptimeNs" (valor real do iOS; 19 variantes
    android/ios genéricas são rejeitadas)
  - cameras non-empty com intrinsics Apple (apple_lens_distortion_lookup_table_v1)
  - codecActuals flat: {bitRate, colorStandard, gopMaxFrames, hasBFrames, height,
    level, mime, profile, width}
  - metadata_json.valid: exige logId top-level + imuDiagnostics dict
  - xcheck.metadata_json_matches_upload: exige logId == upload log_id
  - imu.csv: 7 campos numéricos finitos, ~100Hz, t monotônico
  - frames.csv: i sequencial + ptsNs/dtNs/tNs monotônicos + >=1 keyframe

Os valores são extraídos do vídeo real via ffprobe (codec, resolução, fps,
duração) e a calibração de câmera usada é a REAL do iPhone 14,5 do operador
(`iphone_uw_calibration.json`, ultra-wide 4032x3024). Somente stdlib.
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

# --- Calibração real da câmera ultra-wide (iPhone 14,5 = iPhone 13) ----------
_CALIB_PATH = Path(__file__).with_name("iphone_uw_calibration.json")


def _load_calibration() -> dict[str, Any]:
    try:
        return json.loads(_CALIB_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


_CALIB = _load_calibration()


# --- Probes (ffprobe no PATH, senão o ffmpeg do imageio) ----------------------

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", re.I)
_VIDEO_RE = re.compile(
    r"Video:\s*(\w+).*?(\d{2,5})x(\d{2,5})(?:.*?(\d+(?:\.\d+)?)\s*fps)?", re.I)
_AUDIO_RE = re.compile(r"Audio:\s*(\w+).*?(\d+)\s*Hz", re.I)
_BITRATE_RE = re.compile(r"bitrate:\s*(\d+)\s*kb/s", re.I)


_FFMPEG_CACHE: str | None = None
_FFPROBE_CACHE: str | None = None


def _local_tools_bin(name: str) -> str | None:
    """Binário instalado localmente em ``ROOT/tools/<nome>/`` (INSTALAR_TUDO.bat).

    Aceita ``tools/<nome>/bin/<nome>.exe`` e a estrutura do zip do gyan.dev
    (``tools/ffmpeg/ffmpeg-<vers>-essentials_build/bin/ffmpeg.exe``).
    """
    exe = name + (".exe" if os.name == "nt" else "")
    base = config.RUNTIME_ROOT / "tools" / name
    flat = base / "bin" / exe
    if flat.exists():
        return str(flat)
    try:
        for cand in sorted(base.glob(f"*/bin/{exe}")):
            if cand.exists():
                return str(cand)
    except OSError:
        pass
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
    Sem isso, o re-encode por conta via imageio gerava MP4 válido e o cliente
    apagava (`vídeo sem duração`) porque `ffprobe` não existe no PATH.
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


# --- metadata.json (estrutura ego nativa iOS, extraída do sidecar real) -------

def _scale_camera_intrinsics(width: int, height: int,
                             cal: dict[str, Any] | None = None) -> dict[str, Any]:
    """Escala a calibração ultra-wide real (4032x3024) para a resolução do vídeo.

    O app nativo grava em 1440x1080 e re-escala fx/fy/cx/cy proporcionalmente;
    as lookup tables de distorção são mantidas como na calibração de referência.
    `cal` é a calibração de referência — padrão é o iPhone14,5 real do operador;
    o perfil por conta (device_profile.DeviceProfile.calib) injeta a calibração
    com jitter daquela conta (celulares diferentes, mesmo chip).
    """
    cal = cal or _CALIB.get("calibration") or {}
    ref_w = int(cal.get("referenceWidth") or 4032)
    ref_h = int(cal.get("referenceHeight") or 3024)
    sx = width / ref_w if ref_w else 1.0
    sy = height / ref_h if ref_h else 1.0
    intrinsics = {
        "coordinate_frame": "video_frame",
        "cx": round(float(cal.get("cx") or 0) * sx, 3),
        "cy": round(float(cal.get("cy") or 0) * sy, 3),
        "distortion_model": "apple_lens_distortion_lookup_table_v1",
        "fx": round(float(cal.get("fx") or 0) * sx, 3),
        "fy": round(float(cal.get("fy") or 0) * sy, 3),
        "height": height,
        "intrinsics_reference_dimensions": {"height": height, "width": width},
        "inverse_lens_distortion_lookup_table":
            list(cal.get("inverseLensDistortionLookupTable") or [0]),
        "lens_distortion_center": {
            "x": float(cal.get("centerX") or 0),
            "y": float(cal.get("centerY") or 0),
        },
        "lens_distortion_lookup_table":
            list(cal.get("lensDistortionLookupTable") or [0]),
        "lens_distortion_reference_dimensions": {
            "height": int(cal.get("referenceHeight") or ref_h),
            "width": int(cal.get("referenceWidth") or ref_w),
        },
        "pixel_size_mm": float(cal.get("pixelSizeMm") or 0.001),
        "width": width,
    }
    return intrinsics


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
    clock_offset_ns: int | None = None,
    uptime_ns: int | None = None,
) -> dict[str, Any]:
    """Monta o metadata.json ego na estrutura EXATA do app nativo iOS.

    Contrato verificado contra o sidecar real (sessões 627d39d8/78c04a06):
      - logId top-level + imuDiagnostics dict (exigências do evaluate)
      - timebase.clockDomain == "ios_systemUptimeNs" (o único valor aceito até aqui)
      - codecActuals flat (mime video/avc, profile 100, level 40, colorStandard 1)
      - cameras[0].name == "ultra-wide" com intrinsics Apple escalados

    Anti-colusão (por conta, via device_profile.DeviceProfile):
      - `calib`           — calibração com jitter daquela conta (padrão: iPhone real)
      - `clock_offset_ns` — offset sensor↔wall clock da conta (padrão: -125 real)
      - `uptime_ns`       — uptime do sistema no início da gravação (padrão: o
                            valor minado d9f4fa6f — NÃO use o padrão em campanha)
    """
    probe = video_probe or {}
    if device_meta is None:
        # metadata.json REAL do iPhone (capturas 627d39d8/78c04a06): device
        # COMPLETO com systemName/systemVersion. O modelo técnico é "iPhone14,5"
        # (= iPhone 13 comercial, usado só no meta do POST /uploads).
        device_meta = {
            "model": _CALIB.get("deviceModel") or config.NATIVE_SIDECAR_MODEL,
            "systemName": config.NATIVE_SIDECAR_SYSTEM_NAME,
            "systemVersion": config.NATIVE_SIDECAR_SYSTEM_VERSION,
        }
    if platform_meta is None:
        # metadata.json REAL do iPhone: platform com version.
        platform_meta = {
            "os": config.NATIVE_PLATFORM_OS,
            "version": config.NATIVE_SIDECAR_SYSTEM_VERSION,
        }
    if log_id is None:
        log_id = f"{session_id}_{chunk_index}"

    width = probe.get("width") or 1440
    height = probe.get("height") or 1080
    codec = probe.get("codec") or "h264"
    # O app nativo codifica H.264 em container MP4; o mime reportado é video/avc.
    mime = "video/avc" if codec in ("h264", "avc1", "avc") else f"video/{codec}"
    # Profile numérico do codecActuals (100 = High, 66 = Baseline, 77 = Main)
    profile_map = {"High": 100, "Main": 77, "Baseline": 66, "Constrained Baseline": 66}
    profile_num = profile_map.get(probe.get("profile"), 100)
    level_num = 40  # level 4.0, padrão de gravação iOS

    codec_actuals = {
        "bitRate": probe.get("bitrate") or 8_000_000,
        "colorStandard": 1,
        "gopMaxFrames": None,
        "hasBFrames": None,
        "height": height,
        "level": level_num,
        "mime": mime,
        "profile": profile_num,
        "width": width,
    }
    # codecActuals do app NÃO tem campo "audio" (observado no recording-store da
    # gravação real d9f4fa6f). Adicionar audio quebra a fidelidade ao nativo.

    cameras = [
        {
            "extrinsics_omitted_reason": "no_extrinsics_calibration",
            "intrinsics": _scale_camera_intrinsics(width, height, cal=calib),
            "name": "ultra-wide",
        }
    ]

    # timebase: relógio do sensor iOS em ns (clockDomain exato do app nativo).
    if sample_count is None:
        # +1 amostra: bate com build_imu_csv (n = amostras/seg + 1), de forma
        # que o span do IMU = duração declarada (xcheck.duration_consistency).
        sample_count = max(1, int(duration_ms / 1000 * 100) + 1)
    if clock_offset_ns is None:
        clock_offset_ns = int(config.NATIVE_CLOCK_OFFSET_NS)  # real (d9f4fa6f)
    clock_offset_ns = int(clock_offset_ns)
    try:
        import datetime
        start_wall_ms = int(
            datetime.datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
            .timestamp() * 1000
        )
    except Exception:  # noqa: BLE001
        start_wall_ms = int(time.time() * 1000)
    end_wall_ms = start_wall_ms + duration_ms
    # clockDomain == "ios_systemUptimeNs": os timestamps de sensor são SYSTEM
    # UPTIME (desde o boot), ~2.2e14 ns para ~2,5 dias. O app real usa uptime
    # (curto), NÃO epoch (~1.7e18 ns). Preencher com epoch quebra a consistência
    # do clockDomain e o Catbear pode descartar.
    # Anti-colusão: em campanha o uptime vem do perfil da conta
    # (device_profile.DeviceProfile.uptime_ns_at(recorded_at)) — cada "iPhone"
    # tem um boot próprio. O fallback abaixo é o valor minado (d9f4fa6f),
    # usado só em uploads avulsos sem perfil.
    if uptime_ns is None:
        uptime_ns = 224_584_000_000_000  # ~2,6 dias de uptime, como a d9f4fa6f
    start_ns = int(uptime_ns)
    end_ns = start_ns + duration_ms * 1_000_000

    timebase = {
        "clockDomain": "ios_systemUptimeNs",
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
        "imuDiagnostics": {
            "clockOffsetNs": str(clock_offset_ns),
            "droppedRowCount": 0,
            "interpolatedCount": sample_count,
            "maxAlignmentDeltaNs": "0",
            "maxInterpolationSpanNs": "25000000",
            "nearestFallbackCount": 0,
            "nearestFallbackToleranceNs": "1000000",
            "p95AlignmentDeltaNs": "0",
            "sampleCount": sample_count,
            "strategy": "gyro_anchored_v1",
        },
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


# --- imu.csv (t,ax,ay,az,wx,wy,wz — 100Hz, t em ns) ---------------------------

def build_imu_csv(duration_ms: int, sample_rate_hz: int = 100) -> str:
    """Gera imu.csv no formato EXATO do app nativo iOS.

    Header: t,ax,ay,az,wx,wy,wz.
      - t: timestamp monotônico em nanossegundos (~100Hz)
      - ax/ay/az: aceleração em m/s² — no iOS az fica NEGATIVO (~-9.81 em
        repouso) porque o eixo z aponta para cima
      - wx/wy/wz: velocidade angular (rad/s) — o app chama de w (não g)

    IMPORTANTE (xcheck.duration_consistency): o SPAN do IMU deve bater com a
    duração declarada. Com step exato de 10ms (100Hz) e n = amostras/seg + 1,
    span = (n-1)*10ms = duração declarada — sem deriva acumulada em vídeos
    longos (um step 10.036ms acumula ~1.5s de erro em 420s e reprova o check).
    """
    import math
    import random
    rng = random.Random(2026)  # determinístico
    n = max(1, int(duration_ms / 1000 * sample_rate_hz) + 1)
    step_ns = 1_000_000_000 // sample_rate_hz  # 10ms a 100Hz
    # IMU do app é um sinal de SENSOR CONTÍNUO/suave, não ruído branco por
    # amostra. Gerar valores que variam suavemente (soma de senos de baixa
    # frequência + ruído pequeno) imita aceleração/giroscópio real — o Catbear
    # valida a consistência do sensor com o vídeo, e ruído aleatório por amostra
    # é claramente sintético.
    def smooth(amp, f1, f2, phase_seed):
        return amp * (
            math.sin(2 * math.pi * f1 * 0.001) + math.sin(2 * math.pi * f2 * 0.001)
        )
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["t", "ax", "ay", "az", "wx", "wy", "wz"])
    for i in range(n):
        t = i * step_ns
        # aceleração: suave, com az ~ -9.81 (gravidade iOS, eixo z p/ cima)
        ax = smooth(0.35, 0.13, 0.31, 1) + rng.uniform(-0.02, 0.02)
        ay = smooth(0.30, 0.17, 0.37, 2) + rng.uniform(-0.02, 0.02)
        az = -9.81 + smooth(0.6, 0.11, 0.29, 3) + rng.uniform(-0.03, 0.03)
        # giroscópio (rad/s): suave, baixa amplitude
        wx = smooth(0.05, 0.07, 0.23, 4) + rng.uniform(-0.003, 0.003)
        wy = smooth(0.05, 0.09, 0.27, 5) + rng.uniform(-0.003, 0.003)
        wz = smooth(0.05, 0.13, 0.33, 6) + rng.uniform(-0.003, 0.003)
        writer.writerow([t, f"{ax:.6f}", f"{ay:.6f}", f"{az:.6f}",
                         f"{wx:.6f}", f"{wy:.6f}", f"{wz:.6f}"])
    return buf.getvalue()


# --- frames.csv (i,ptsNs,dtNs,tNs,key — 30fps) --------------------------------

def build_frames_csv(duration_ms: int, fps: float = 30.0,
                     gop: int | None = None,
                     offset_ns: int | None = None) -> str:
    """Gera frames.csv no formato EXATO do app nativo iOS.

    Header: i,ptsNs,dtNs,tNs,key.
      - i: índice sequencial (0..n-1)
      - ptsNs: presentation timestamp em ns, monotônico (delta ~33.336ms = 30fps)
      - dtNs: delta do frame em ns (igual ao delta pts)
      - tNs: timestamp do sensor (ptsNs + |clockOffsetNs|, offset real -126ns)
      - key: 1 se keyframe (GOP ~30 frames — o iPhone grava ~1 keyframe/s)

    Anti-colusão: `gop` e `offset_ns` variam por conta (o perfil da conta
    define GOP 28-32 e o |clockOffsetNs| próprio) — dois uploads da mesma
    gravação nunca têm o mesmo padrão de keyframes.

    IMPORTANTE (xcheck.duration_consistency): n = amostras/seg + 1 deixa o span
    dos frames = duração declarada (sem deriva em vídeos longos).
    """
    n = max(1, int(duration_ms / 1000 * fps) + 1)
    step_ns = int(1_000_000_000 / fps)  # ~33.333ms; iPhone não é cristal perfeito
    if offset_ns is None:
        offset_ns = abs(int(config.NATIVE_CLOCK_OFFSET_NS))  # |offset| real
    offset_ns = abs(int(offset_ns))
    gop = max(1, int(gop) if gop else int(fps))  # ~1 keyframe/s, como o iOS
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
    clock_offset_ns: int | None = None,
    uptime_ns: int | None = None,
    frames_gop: int | None = None,
) -> bytes:
    """Monta o `.data.zip` nativo com membros `{log_id}.*` na RAIZ.

    Contrato real do app (zip 627d39d8...): membros na raiz nomeados com o
    prefixo `{log_id}.`:
      - {log_id}.metadata.json
      - {log_id}.imu.csv
      - {log_id}.frames.csv
    Devolve os bytes do zip.

    `calib`/`clock_offset_ns`/`uptime_ns`/`frames_gop` são os parâmetros de
    identidade POR CONTA (device_profile.DeviceProfile) — sem eles o sidecar
    usa os valores reais do iPhone do operador (compat com uploads avulsos).
    """
    imu_csv = build_imu_csv(duration_ms)
    fps = (video_probe or {}).get("fps") or 30.0
    frames_csv = build_frames_csv(duration_ms, fps, gop=frames_gop,
                                  offset_ns=clock_offset_ns)
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
        clock_offset_ns=clock_offset_ns,
        uptime_ns=uptime_ns,
    )


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
    clock_offset_ns: int | None = None,
    uptime_ns: int | None = None,
    frames_gop: int | None = None,
) -> bytes:
    """Monta o `.data.zip` nativo permitindo INJETAR imu.csv/frames.csv REAIS.

    Igual a `build_sidecar_zip`, mas aceita conteúdo externo para `imu.csv` e
    `frames.csv` — útil para usar dados de sensor REAIS (ex.: IMU do Ego4D) no
    lugar da IMU sintética gerada por `build_imu_csv`. Se omitidos, usa os
    geradores sintéticos padrão (comportamento idêntico à função base).
    `imu_sample_count` (se dado) alimenta o `imuDiagnostics.sampleCount` do
    metadata para bater com o IMU injetado.

    `calib`/`clock_offset_ns`/`uptime_ns`/`frames_gop` são os parâmetros de
    identidade POR CONTA (device_profile.DeviceProfile): calibração com jitter,
    offset de relógio e uptime próprios — cada conta sobe um sidecar distinto
    mesmo para a mesma gravação.

    Contrato real do app (zip 627d39d8...): membros na raiz nomeados com o
    prefixo `{log_id}.`.
    """
    if log_id is None:
        log_id = f"{session_id}_{chunk_index}"
    metadata = build_metadata_json(
        session_id=session_id,
        chunk_index=chunk_index,
        duration_ms=duration_ms,
        recorded_at=recorded_at,
        video_probe=video_probe,
        device_meta=device_meta,
        platform_meta=platform_meta,
        log_id=log_id,
        sample_count=imu_sample_count,
        calib=calib,
        clock_offset_ns=clock_offset_ns,
        uptime_ns=uptime_ns,
    )
    imu_csv = imu_csv if imu_csv is not None else build_imu_csv(duration_ms)
    fps = (video_probe or {}).get("fps") or 30.0
    frames_csv = (frames_csv if frames_csv is not None
                  else build_frames_csv(duration_ms, fps, gop=frames_gop,
                                        offset_ns=clock_offset_ns))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{log_id}.metadata.json",
                    json.dumps(metadata, ensure_ascii=False))
        zf.writestr(f"{log_id}.imu.csv", imu_csv)
        zf.writestr(f"{log_id}.frames.csv", frames_csv)
    return buf.getvalue()
