"""
campaign.py — Orquestração end-to-end de datasets egocêntricos -> Minute.

Responsável por, dado um conjunto de clipes + contas + tasks:
  1. Preparar cada clipe (baixar vídeo + IMU real, normalizar, montar sidecar).
  2. Enviar para cada conta configurada (upload_session com IMU real).
  3. Gerar um relatório estruturado (JSON) com os resultados e status.

Fluxo completo (VÁLIDO, verificado 13/08 — catbear retornou `great` em
Clarity/Variety/Task para montagem de móveis e lavagem de carro, em 2 contas):
  Ego4D(clipe+IMU real) -> normalize_video(1440x1080 yuv420p) -> sidecar nativo
  -> upload_session(register_first, evaluate, finalize) -> relatório.

Regras de ouro (não descumprir):
  - O cenário do vídeo DEVE corresponder à task do Minute (`task` score).
  - IMU real do Ego4D (não sintética) no sidecar (coerência sensor<->vídeo).
  - Vídeos longos: `timeout_blob` alto para o PUT do blob não estourar.
"""
from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import os
import random
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    wait,
)
from contextlib import contextmanager
from functools import lru_cache, partial
from pathlib import Path
from typing import Any

from . import (
    config,
    device_profile,
    ego4d,
    holoassist,
    recording_timeline,
    sent_registry,
    task_matching,
)
from .campaign_types import (
    DEFAULT_ACCOUNT_STAGGER_S,
    DEFAULT_TIMEOUT_BLOB,
    MIN_ACCOUNT_AGE_DAYS,
    AccountSpec,
    CampaignConfig,
    CampaignLog,
    TaskSpec,
)
from .device_profile import DeviceProfile
from .minute_api import AuthError, Session
from .sidecar import (
    build_frames_csv,
    build_frames_csv_from_video,
    build_imu_csv,
    build_sidecar_zip_custom,
    ffmpeg_bin,
    probe_video,
)
from .task_catalog import (
    BOOSTED_TASKS,
    CATEGORY_PT,
    SCENARIO_PT,
    TASK_NAME_PT,
    TASK_TO_SCENARIO,
)
from .upload import UploadError, pump_pending, upload_session

__all__ = [
    "AccountSpec",
    "BOOSTED_TASKS",
    "CampaignConfig",
    "CampaignLog",
    "CATEGORY_PT",
    "DATASET_PROVIDERS",
    "DEFAULT_ACCOUNT_STAGGER_S",
    "DEFAULT_TIMEOUT_BLOB",
    "MIN_ACCOUNT_AGE_DAYS",
    "SCENARIO_PT",
    "TASK_NAME_PT",
    "TASK_TO_SCENARIO",
    "TaskSpec",
    "available_tasks",
    "cleanup_media_cache",
    "list_campaign_logs",
    "normalize_dataset_provider",
    "prepare_clip",
    "prepare_holoassist_clip",
    "run_campaign",
    "session_result",
    "upload_to_account",
    "warm_task_catalog",
]

# Resolução/bitrate do reencode nativo (replica o app Android 1.22.0).
NATIVE_WIDTH, NATIVE_HEIGHT = 1440, 1080
NATIVE_BITRATE = "8000k"
# Ryzen 9600X: 12 threads lógicas. Seis encodes × 2 threads ocupam a CPU sem
# deixar cada processo x264 tentar monopolizar todos os núcleos.
ACCOUNT_ENCODE_WORKERS = max(1, min(6, (os.cpu_count() or 2) // 2))
_ACCOUNT_ENCODE_SLOTS = threading.BoundedSemaphore(ACCOUNT_ENCODE_WORKERS)
# Teto de PUTs simultâneos. O vídeo-base já está pronto antes dos PUTs e o
# hardware desta estação comporta seis conexões sem disparar todas as contas
# de uma vez. Todas as contas entram no lote; só N voam em cada onda.
DEFAULT_MAX_ACCOUNT_WORKERS = 6


def max_account_workers() -> int:
    """Teto de uploads simultâneos. Env MINUTE_MAX_ACCOUNT_WORKERS sobrescreve."""
    raw = os.environ.get("MINUTE_MAX_ACCOUNT_WORKERS", "").strip()
    if raw:
        try:
            return max(1, min(15, int(raw)))
        except ValueError:
            pass
    return DEFAULT_MAX_ACCOUNT_WORKERS


def clamp_account_workers(requested: int, n_accounts: int) -> int:
    """Quantas contas voam ao mesmo tempo: pedido, tamanho do lote e teto."""
    n = max(1, int(n_accounts or 1))
    want = max(1, int(requested or 1))
    return max(1, min(want, n, max_account_workers()))


def _is_disabled_error(error: str | None) -> bool:
    text = (error or "").lower()
    return "desativad" in text or "disabled" in text or "conta desativada" in text
# Janela de duração aceita (recording-config: min 60s / max 1800s).
MIN_DUR_MS, MAX_DUR_MS = 60000, 1800000
# O app sobe a sessão em pedaços (`{session}_{i}`). Gravação contínua curta
# fica num único `_0`; acima disto parte em janelas ≥ 60s.
NATIVE_CHUNK_TARGET_MS = 480_000
DATASET_PROVIDERS = frozenset({"all", "ego4d", "holoassist"})


# --- meios (helpers) ----------------------------------------------------------

def normalize_dataset_provider(value: str | None) -> str:
    provider = str(value or "all").strip().lower()
    if provider not in DATASET_PROVIDERS:
        raise ValueError("dataset inválido (all|ego4d|holoassist)")
    return provider

def _frames_csv(duration_ms: int, fps: float = 30.0) -> str:
    return build_frames_csv(duration_ms, fps)


def _recorded_at_now() -> str:
    return device_profile.format_recorded_at(time.time())


def _ffmpeg_head(ff: str) -> list[str]:
    """Flags que evitam o ffmpeg pausar no stdin ou abrir console no Windows."""
    return [ff, "-hide_banner", "-nostdin", "-y", "-v", "error"]


def _ffmpeg_run(cmd: list[str], timeout: int = 3600):
    import subprocess
    kwargs: dict[str, Any] = {
        "capture_output": True, "text": True, "timeout": timeout,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, **kwargs)


def _heavy_encode_threads() -> int:
    """Todos os núcleos: o PUT só começa depois do prepare."""
    return max(1, os.cpu_count() or 4)


@lru_cache(maxsize=4)
def _ffmpeg_encoder_available(ff: str, encoder: str) -> bool:
    """Confirma que o ffmpeg instalado anuncia o encoder solicitado."""
    import subprocess
    try:
        res = _ffmpeg_run([ff, "-hide_banner", "-encoders"], timeout=20)
    except (OSError, TimeoutError, subprocess.TimeoutExpired):
        return False
    output = f"{getattr(res, 'stdout', '')}\n{getattr(res, 'stderr', '')}"
    return res.returncode == 0 and encoder in output


def _use_nvenc(ff: str) -> bool:
    """Seleciona RTX/NVENC automaticamente; MINUTE_VIDEO_ENCODER força CPU/GPU."""
    requested = os.environ.get("MINUTE_VIDEO_ENCODER", "auto").strip().lower()
    if requested in {"cpu", "x264", "libx264", "off", "disabled"}:
        return False
    return _ffmpeg_encoder_available(ff, "h264_nvenc")


def _native_video_codec_args(
    *,
    nvenc: bool,
    bitrate: str | None = None,
    gop: int | None = None,
) -> list[str]:
    """H.264 High@4.2 sem B-frames — MediaRecorder do app (profile 8, level 8192).

    O lado do metadata (codecActuals) reporta hasBFrames/gopMaxFrames null,
    exatamente como o EgoCodecActuals do app. GOP padrão de 30 (= ~1
    keyframe/s, como o MediaRecorder) em QUALQUER encode — o app não deixa o
    GOP do encoder crescer para ~8 s.
    """
    br = bitrate or NATIVE_BITRATE
    # GOP ~1s (30 a 30fps): o app grava ~1 keyframe/s e o frames.csv espelha
    # os keyframes reais do MP4. Default 30; o re-encode por aparelho usa o GOP
    # do perfil (28-32) via `gop`.
    gop_value = int(gop) if gop is not None else 30
    gop_args = ["-g", str(gop_value), "-keyint_min", str(gop_value)]
    if nvenc:
        return [
            "-c:v", "h264_nvenc", "-preset", "p4", "-tune", "hq",
            "-rc", "vbr", "-b:v", br, "-maxrate", br, "-bufsize", "16000k",
            "-profile:v", "high", "-level:v", "4.2",
            "-spatial_aq", "1", "-temporal_aq", "1",
            "-rc-lookahead", "20", "-bf", "0",
            *gop_args,
        ]
    return [
        "-c:v", "libx264", "-preset", "veryfast",
        "-threads", str(_heavy_encode_threads()),
        "-b:v", br, "-maxrate", br, "-bufsize", "16000k",
        "-profile:v", "high", "-level", "4.2",
        "-bf", "0",
        *gop_args,
    ]


def _native_container_args(tmp: Path) -> list[str]:
    """Envelope Android: 1440x1080 yuv420p TV, VideoHandler/SoundHandler,
    AAC 48 kHz ESTÉREO 256 kbps (EgoAudioConfig 2ch/48000/256000)."""
    return [
        "-vf", ("scale=1440:1080:force_original_aspect_ratio=increase,"
                "crop=1440:1080,scale=in_range=full:out_range=tv,format=yuv420p"),
        "-r", "30",
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-metadata:s:v:0", "handler_name=VideoHandler",
        "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "256k",
        "-metadata:s:a:0", "handler_name=SoundHandler",
        "-movflags", "+faststart", "-f", "mp4", str(tmp),
    ]


@contextmanager
def _cpu_slots(n: int) -> Iterator[None]:
    """Reserva `n` slots do semáforo de encode (normalize pega todos)."""
    n = max(1, min(int(n), ACCOUNT_ENCODE_WORKERS))
    acquired = 0
    try:
        for _ in range(n):
            _ACCOUNT_ENCODE_SLOTS.acquire()
            acquired += 1
        yield
    finally:
        for _ in range(acquired):
            _ACCOUNT_ENCODE_SLOTS.release()


def _normalize_video(src: Path, out_dir: Path, *,
                    start_s: float | None = None,
                    dur_s: float | None = None,
                    stem: str | None = None) -> Path:
    """Reencoda para 1440x1080 yuv420p + handler Android VideoHandler.

    `start_s`/`dur_s` cortam o vídeo-pai no mesmo passo (sem arquivo .cut).
    """
    ff = _ffmpeg_bin()
    out = out_dir / f"{stem or src.stem}_native.mp4"
    marker = out.with_name(out.name + ".source.json")
    try:
        source_stat = src.stat()
        cache_key = {
            "version": 4,
            "source_size": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "start_s": None if start_s is None else round(float(start_s), 6),
            "dur_s": None if dur_s is None else round(float(dur_s), 6),
            "width": 1440,
            "height": 1080,
            "fps": 30,
        }
    except OSError as exc:
        raise RuntimeError(f"fonte Ego4D inacessível: {src}: {exc}") from exc
    if out.exists():
        # cache: o reencode é determinístico (depende só do src) — reutiliza se
        # o arquivo existente for um vídeo válido; reencoda só se corrompido.
        try:
            saved_key = json.loads(marker.read_text(encoding="utf-8"))
            probe = probe_video(out)
            duration_ok = bool(probe.get("duration_ms"))
            if dur_s is not None and duration_ok:
                duration_ok = abs(
                    int(probe["duration_ms"]) - round(float(dur_s) * 1000)
                ) <= 1000
            if saved_key == cache_key and duration_ok:
                return out
        except Exception:
            pass
        try:
            out.unlink()
        except OSError:
            pass
        marker.unlink(missing_ok=True)
    cmd = [*_ffmpeg_head(ff)]
    # Seek híbrido: -ss no input (rápido) + -ss no output (alinha ao IMU).
    # Só -ss antes de -i pega keyframe anterior e o catbear vê vídeo ≠ sensor.
    if start_s is not None and float(start_s) > 0.05:
        pre = max(0.0, float(start_s) - 2.0)
        skip = float(start_s) - pre
        cmd += ["-ss", f"{pre:.3f}", "-i", str(src), "-ss", f"{skip:.3f}"]
    else:
        cmd += ["-i", str(src)]
        if start_s is not None and float(start_s) > 0:
            cmd += ["-ss", f"{float(start_s):.3f}"]
    if dur_s is not None:
        cmd += ["-t", f"{float(dur_s):.3f}"]
    tmp = out.with_name(f"{out.stem}.tmp.mp4")
    if tmp.exists():
        tmp.unlink()
    input_args = cmd
    common_args = _native_container_args(tmp)

    use_nvenc = _use_nvenc(ff)
    cmd = [*input_args, *_native_video_codec_args(nvenc=use_nvenc), *common_args]
    # O filtro ainda usa CPU, mas NVENC precisa de só dois slots. Isso deixa o
    # restante da máquina livre para I/O, sidecar e uploads já em andamento.
    slots = min(2, ACCOUNT_ENCODE_WORKERS) if use_nvenc else ACCOUNT_ENCODE_WORKERS
    with _cpu_slots(slots):
        res = _ffmpeg_run(cmd)

    # Encoder anunciado mas indisponível (driver/sessões GPU): refaz em CPU.
    # A campanha continua funcional mesmo após atualização de driver ou troca
    # de máquina, sem aceitar um MP4 parcial como cache válido.
    if res.returncode != 0 and use_nvenc:
        if tmp.exists():
            tmp.unlink()
        cmd = [*input_args, *_native_video_codec_args(nvenc=False), *common_args]
        with _cpu_slots(ACCOUNT_ENCODE_WORKERS):
            res = _ffmpeg_run(cmd)
    if res.returncode != 0:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f"falha ao normalizar vídeo: {res.stderr.strip()[:400]}")
    if not probe_video(tmp).get("duration_ms"):
        size = tmp.stat().st_size if tmp.exists() else 0
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(
            f"normalize gerou vídeo sem duração (size={size})")
    tmp.replace(out)
    marker_tmp = marker.with_name(marker.name + ".tmp")
    marker_tmp.write_text(
        json.dumps(cache_key, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    marker_tmp.replace(marker)
    return out


def _ffmpeg_bin() -> str:
    return ffmpeg_bin()


# --- preparação de um clipe ---------------------------------------------------

def _interruptible_sleep(
    delay_s: float,
    should_stop: Callable[[], bool] | None,
    emit: Callable[..., None],
    *,
    kind: str = "delay_tick",
    tick_every: float = 1.0,
) -> bool:
    """Espera `delay_s` em fatias de 0.5s, checando `should_stop()` e emitindo
    ticks (`kind`, a cada `tick_every` s) com o countdown para a UI.
    Devolve True se foi interrompida."""
    remaining = float(delay_s)
    next_tick = remaining  # emite já no primeiro passo
    while remaining > 0:
        if should_stop and should_stop():
            return True
        if remaining <= next_tick:
            emit(kind, remaining_s=int(remaining + 0.999))
            next_tick = remaining - tick_every
        step = min(0.5, remaining)
        time.sleep(step)
        remaining -= step
    return should_stop() if should_stop else False


def _tick_every(total_s: float) -> float:
    """Granularidade do countdown: fina em esperas curtas, grossa em longas."""
    return 1.0 if total_s <= 120 else 5.0


def _window_remaining_s(active_hours: tuple[int, int], now=None) -> float:
    """Segundos até a próxima abertura da janela (0 se já está dentro dela).

    Janela em hora local, início <= hora < fim — ex.: (7, 18) = das 7h às 18h.
    """
    start_h, end_h = active_hours
    now = now or datetime.datetime.now()
    mins = now.hour * 60 + now.minute + now.second / 60
    start_m, end_m = start_h * 60, end_h * 60
    if start_m <= mins < end_m:
        return 0.0
    delta = start_m - mins if mins < start_m else (24 * 60 - mins) + start_m
    return delta * 60


def _wait_for_window(
    active_hours: tuple[int, int],
    should_stop: Callable[[], bool] | None,
    emit: Callable[..., None],
) -> bool:
    """Aguarda a janela de envio abrir (ticks de 60s). True se interrompida."""
    first = True
    while True:
        rem = _window_remaining_s(active_hours)
        if rem <= 0:
            return False
        if first:
            emit("window_wait_start", active_hours=list(active_hours),
                 remaining_s=int(rem))
            first = False
        if _interruptible_sleep(rem, should_stop, emit,
                                kind="window_wait_tick", tick_every=60.0):
            return True

def _ego_clip_inputs(
    clip_info: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Preserva a janela precisa do ranking; UID é só identidade, não storage."""
    parent = str(clip_info.get("parent_video_uid") or "")
    window = clip_info.get("window_s")
    s3_path = str(clip_info.get("s3_path") or "")
    if parent and window and len(window) == 2 and s3_path:
        video = ego4d._cat().videos.get(parent)
        if video is not None:
            start, end = float(window[0]), float(window[1])
            row = dict(clip_info)
            row.update({
                "exported_clip_uid": str(
                    clip_info.get("exported_clip_uid")
                    or clip_info.get("clip_uid") or ""),
                "parent_video_uid": parent,
                "parent_start_sec": str(start),
                "parent_end_sec": str(end),
                "s3_path": s3_path,
            })
            return row, video
    return ego4d.find_clip(str(clip_info.get("clip_uid") or ""))


def prepare_clip(
    clip: dict[str, Any],
    video: dict[str, Any],
    work_dir: Path,
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Baixa clipe + IMU real, normaliza o vídeo e monta o sidecar. (sem upload)"""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    clip_uid = clip["exported_clip_uid"]
    orig_start, orig_end = ego4d.clip_window_s(clip)
    start_s, end_s = ego4d.humanize_window(orig_start, orig_end, clip_uid)
    window_s = (start_s, end_s)
    dur_s = end_s - start_s
    dur_ms = int(round(dur_s * 1000))
    if not (MIN_DUR_MS <= dur_ms <= MAX_DUR_MS):
        raise RuntimeError(f"clipe {clip_uid} fora da janela de duração ({dur_ms}ms)")
    if not ego4d.imu_window_is_covered(video, window_s):
        raise RuntimeError(
            f"clipe {clip_uid} atravessa trecho sem cobertura contínua de IMU")

    parent_uid = str(clip.get("parent_video_uid") or clip_uid)

    def _progress(phase: str, **payload: Any) -> None:
        if progress:
            progress(phase, payload)

    # Valide o sensor ANTES de baixar/codificar gigabytes de vídeo. O catálogo
    # informa a cobertura geral, mas alguns aparelhos têm lacunas finas (por
    # exemplo, acelerômetro ausente por ~1 s) visíveis somente no CSV. Antes a
    # campanha gastava vários minutos no encode e só então descartava o clipe.
    _progress("imu_lookup")
    imu_path = ego4d.download_imu(video, work_dir / f"{parent_uid}_imu.csv")
    if imu_path is None:
        raise RuntimeError(
            f"clipe {clip_uid} sem IMU real — não enviar (coerência sensor falharia)."
            f" Escolha um clipe com has_imu=true.")
    _progress("imu_preflight")
    ego4d.build_imu_csv(
        imu_path, window_s, duration_ms=dur_ms, validate_only=True)
    _progress("imu_ready")

    _progress("video_lookup")
    if clip.get("needs_cut"):
        # Prefere o clip oficial CRF 18 quando ele contém a janela; o IMU
        # continua no relógio absoluto do vídeo canônico.
        media_uid = str(clip.get("media_uid") or parent_uid)
        media_offset = float(clip.get("media_time_offset_s") or 0.0)
        parent_path = work_dir / f"{media_uid}.mp4"
        if not ego4d._valid_mp4_cache(parent_path):
            source = dict(clip)
            source["needs_cut"] = False
            ego4d.download_clip(source, parent_path)
        source_video_path = parent_path
        _progress("encode")
        native = _normalize_video(
            parent_path, work_dir,
            start_s=start_s - media_offset, dur_s=dur_s, stem=clip_uid)
    else:
        clip_path = work_dir / f"{clip_uid}.mp4"
        ego4d.download_clip(clip, clip_path)
        source_video_path = clip_path
        # O mp4 oficial começa em orig_start; o corte humano é relativo a ele.
        rel = start_s - orig_start
        _progress("encode")
        native = _normalize_video(
            clip_path, work_dir,
            start_s=rel if rel > 0.02 else None,
            dur_s=dur_s, stem=clip_uid)
    _progress("video_ready", bytes=native.stat().st_size)
    probe = probe_video(native)
    if not probe.get("duration_ms"):
        raise RuntimeError(
            f"clipe {clip_uid}: native sem duração após normalize "
            f"({native})")

    # Duração do ARQUIVO (probe) — o encoder nunca entrega N.000 s.
    dur_ms = int(probe["duration_ms"])
    _progress("sidecar")
    imu_csv = ego4d.build_imu_csv(imu_path, window_s, duration_ms=dur_ms)
    # 500 Hz (EgoImu.SAMPLING_PERIOD_US=2000) — o n_samples alimenta o
    # imuDiagnostics.sampleCount e precisa bater com as linhas do CSV.
    n_samples = max(
        1, int(dur_ms / 1000 * config.ANDROID_IMU_SAMPLE_RATE_HZ) + 1)
    frames_csv = _frames_csv(dur_ms, fps=(probe.get("fps") or 30.0))

    return {
        "clip_uid": clip_uid,
        "video_path": str(native),
        "duration_ms": dur_ms,
        "device": video.get("device"),
        "scenario": " | ".join(ego4d.scenario_values(video)),
        "imu_real": bool(imu_path),
        # imu.csv/frames.csv por CONTA são remontados no upload com a seed/
        # offset do perfil do aparelho (anti-colusão); o caminho cru + a janela
        # ficam no item para isso. A base (sem seed) segue p/ compatibilidade.
        "imu_csv": imu_csv, "frames_csv": frames_csv,
        "imu_path": str(imu_path),
        "window_s": list(window_s),
        "n_samples": n_samples, "probe": probe,
        "source": "ego4d",
        # Exclusivamente interno: nunca entra no log. Só é consumido após todos
        # os uploads deste item confirmarem sucesso.
        "_cleanup_paths": [
            str(source_video_path), str(native), str(imu_path),
        ],
    }


def prepare_holoassist_clip(
    clip: dict[str, Any],
    work_dir: Path,
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Baixa e prepara uma gravação HoloAssist estritamente elegível.

    Usa o mesmo vídeo nativo, frames e sidecar do motor existente. O único
    adaptador específico converte o acelerômetro/giroscópio sincronizados do
    HoloLens para a grade de 500 Hz exigida pelo sidecar Minute Android.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    clip_uid = str(clip.get("clip_uid") or "")
    video_name = str(clip.get("video_name") or clip.get("video_uid") or "")
    if not clip_uid.startswith("holoassist:") or not video_name:
        raise RuntimeError("identidade HoloAssist inválida")
    if str(clip.get("task_type") or "") not in holoassist.FURNITURE_TASK_TYPES:
        raise RuntimeError("tarefa HoloAssist fora das categorias permitidas")
    if float(clip.get("correct_action_ratio") or 0) < holoassist.MIN_CORRECT_ACTION_RATIO:
        raise RuntimeError("gravação HoloAssist abaixo do limiar de ações corretas")
    if clip.get("has_uncorrected_error"):
        raise RuntimeError("gravação HoloAssist contém erro não corrigido")

    def _progress(phase: str, **payload: Any) -> None:
        if progress:
            progress(phase, payload)

    recording_dir = holoassist.data_dir() / "recordings" / video_name
    pitchshift = recording_dir / "Video_pitchshift.mp4"
    compressed = recording_dir / "Video_compress.mp4"
    _progress("video_cached" if pitchshift.exists() or compressed.exists()
              else "video_lookup")

    def _download_holo_video(*, compressed: bool = False) -> Path:
        if progress:
            return holoassist.download_video(
                video_name, compressed=compressed,
                progress=lambda phase, current, total: _progress(
                    phase, current=current, total=total
                ),
            )
        return holoassist.download_video(video_name, compressed=compressed)

    try:
        source_video = _download_holo_video()
    except FileNotFoundError:
        # Algumas sessões anotadas não existem no TAR pitch-shifted oficial,
        # mas estão no TAR comprimido. É a mesma gravação e mantém o IMU.
        _progress("video_fallback", source="Video_compress.mp4")
        source_video = _download_holo_video(compressed=True)
    _progress("video_ready", bytes=source_video.stat().st_size)

    sensor_dir = holoassist.data_dir() / "recordings" / video_name / "IMU"
    sensor_names = (
        "Accelerometer_sync.txt", "Gyroscope_sync.txt", "Magnetometer_sync.txt",
    )
    sensors_cached = all((sensor_dir / name).exists() for name in sensor_names)
    _progress("imu_cached" if sensors_cached else "imu_lookup")
    if progress:
        sensors = holoassist.download_imu(
            video_name,
            progress=lambda phase, current, total: _progress(
                phase, current=current, total=total
            ),
        )
    else:
        sensors = holoassist.download_imu(video_name)
    _progress("imu_ready")
    safe_stem = "holoassist_" + video_name.replace("/", "_").replace("\\", "_")
    _progress("encode")
    native = _normalize_video(source_video, work_dir, stem=safe_stem)
    _progress(
        "encode_ready",
        encoder="NVENC" if _use_nvenc(_ffmpeg_bin()) else "CPU",
    )
    probe = probe_video(native)
    dur_ms = int(probe.get("duration_ms") or 0)
    if not (MIN_DUR_MS <= dur_ms <= MAX_DUR_MS):
        raise RuntimeError(
            f"gravação {video_name} fora da janela de duração ({dur_ms}ms)"
        )
    _progress("sidecar")
    imu_csv = holoassist.build_imu_csv(
        sensors["Accelerometer_sync.txt"],
        sensors["Gyroscope_sync.txt"],
        duration_ms=dur_ms,
    )
    frames_csv = _frames_csv(dur_ms, fps=(probe.get("fps") or 30.0))
    return {
        "clip_uid": clip_uid,
        "video_path": str(native),
        "duration_ms": dur_ms,
        "device": clip.get("device") or "Microsoft HoloLens 2",
        "scenario": str(clip.get("task_type") or clip.get("scenario") or ""),
        "imu_real": True,
        "imu_csv": imu_csv,
        "frames_csv": frames_csv,
        # 500 Hz (Android) — sampleCount do imuDiagnostics deve bater com o CSV.
        "n_samples": max(1, int(dur_ms / 1000 * config.ANDROID_IMU_SAMPLE_RATE_HZ) + 1),
        "probe": probe,
        "source": "holoassist",
        "_cleanup_paths": [
            str(pitchshift), str(compressed), str(native),
            *(str(path) for path in sensors.values()),
        ],
    }


def _new_identity(duration_s: float, email: str,
                  recorded_at: str | None = None) -> tuple[str, str, str]:
    """Gera session_id/log_id/recorded_at NOVOS (identidade única por conta).

    `recorded_at` é o INÍCIO da gravação, no formato nativo (3 dígitos, UTC)
    e dentro do backlog de 4h. Sem valor pré-agendado, a gravação termina
    `gap` (1.5min–1.5h) antes do upload — nunca "agora".
    """
    session_id = str(uuid.uuid4())
    if recorded_at:
        recorded_at = device_profile.normalize_recorded_at(
            recorded_at, duration_s=duration_s)
        return session_id, f"{session_id}_0", recorded_at
    rng = random.Random(f"{email}|{session_id}")
    gap_s = rng.uniform(90.0, 5400.0)
    start = device_profile.recording_start_epoch(duration_s, gap_s=gap_s)
    recorded_at = device_profile.format_recorded_at(start)
    return session_id, f"{session_id}_0", recorded_at


def _chunk_plan(duration_ms: int, target_ms: int | None = None
                ) -> list[tuple[int, int]]:
    """Janelas (start_ms, dur_ms) respeitando os limites EFETIVOS do
    recording-config remoto (min/max duração) — sem gerar chunk que o preflight
    viria a recusar se o servidor apertar o teto.

    Alvo = menor(8min nominal, max remoto); piso = min remoto.
    """
    limits = config.recording_limits()
    eff_min = max(1_000, int(limits.get("min_duration_ms") or MIN_DUR_MS))
    eff_max = max(eff_min, int(limits.get("max_duration_ms") or MAX_DUR_MS))
    if target_ms is None:
        target_ms = min(NATIVE_CHUNK_TARGET_MS, eff_max)
    target_ms = min(eff_max, max(eff_min, int(target_ms)))
    duration_ms = max(1, int(duration_ms))
    if duration_ms < eff_min:
        raise ValueError(
            f"duração {duration_ms} ms abaixo do mínimo remoto {eff_min} ms")
    if duration_ms <= target_ms:
        return [(0, duration_ms)]

    durations: list[int] = []
    remaining = duration_ms
    while remaining > target_ms:
        durations.append(target_ms)
        remaining -= target_ms
    durations.append(remaining)

    # Uma sobra menor que o mínimo não pode virar um chunk. Primeiro tenta
    # incorporá-la ao anterior sem ultrapassar o teto. Se isso não couber,
    # transfere duração dos chunks anteriores até a sobra alcançar o piso.
    if len(durations) > 1 and durations[-1] < eff_min:
        tail = durations.pop()
        if durations[-1] + tail <= eff_max:
            durations[-1] += tail
        else:
            needed = eff_min - tail
            for index in range(len(durations) - 1, -1, -1):
                transferable = max(0, durations[index] - eff_min)
                moved = min(needed, transferable)
                durations[index] -= moved
                tail += moved
                needed -= moved
                if needed == 0:
                    break
            if needed:
                raise ValueError(
                    f"duração {duration_ms} ms não pode ser dividida em chunks "
                    f"de {eff_min}–{eff_max} ms")
            durations.append(tail)

    if any(not eff_min <= dur <= eff_max for dur in durations):
        raise ValueError(
            f"plano fora dos limites remotos {eff_min}–{eff_max} ms: "
            f"{durations}")

    plan: list[tuple[int, int]] = []
    start = 0
    for dur in durations:
        plan.append((start, dur))
        start += dur
    return plan


def _slice_imu_csv(imu_csv: str, start_ms: int, duration_ms: int) -> tuple[str, int]:
    """Recorta o imu.csv no intervalo do chunk e zera o relógio do pedaço."""
    start_ns = int(start_ms) * 1_000_000
    end_ns = start_ns + int(duration_ms) * 1_000_000
    lines = (imu_csv or "").splitlines()
    if not lines:
        return "", 0
    header = lines[0]
    out = [header]
    for line in lines[1:]:
        if not line.strip():
            continue
        first, _, rest = line.partition(",")
        try:
            t_ns = int(float(first))
        except ValueError:
            continue
        if t_ns < start_ns or t_ns >= end_ns:
            continue
        out.append(f"{t_ns - start_ns},{rest}" if rest else str(t_ns - start_ns))
    return "\n".join(out) + ("\n" if len(out) > 1 else ""), max(0, len(out) - 1)


_CHUNK_VIDEO_LOCKS_GUARD = threading.Lock()
_chunk_video_locks: dict[str, threading.Lock] = {}


def _chunk_video_signature(src: Path, start_s: float, dur_s: float) -> str:
    stat = src.stat()
    value = (
        f"v1|{src.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
        f"{float(start_s):.3f}|{float(dur_s):.3f}"
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _chunk_video_path(
    src: Path,
    chunk_index: int,
    start_ms: int,
    duration_ms: int,
) -> Path:
    """Nome imutável: contas paralelas compartilham o mesmo corte pronto."""
    signature = _chunk_video_signature(
        src, start_ms / 1000.0, duration_ms / 1000.0)
    return src.with_name(
        f"{src.stem}_ch{int(chunk_index)}_{signature[:12]}{src.suffix}")


def _chunk_video_lock(dest: Path) -> threading.Lock:
    key = os.path.normcase(str(dest.resolve(strict=False)))
    with _CHUNK_VIDEO_LOCKS_GUARD:
        return _chunk_video_locks.setdefault(key, threading.Lock())


def _cut_video_chunk(src: Path, dest: Path, start_s: float, dur_s: float) -> Path:
    """Corta uma vez e publica atomicamente para todas as contas do lote."""
    expected = _chunk_video_signature(src, start_s, dur_s)
    ready = dest.with_name(dest.name + ".chunk.ok")
    with _chunk_video_lock(dest):
        try:
            if (dest.is_file() and dest.stat().st_size > 0 and ready.is_file()
                    and ready.read_text(encoding="utf-8").strip() == expected):
                return dest
        except OSError:
            pass
        ff = _ffmpeg_bin()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.stem + ".tmp.mp4")
        if tmp.exists():
            tmp.unlink()
        cmd = [
            *_ffmpeg_head(ff),
            "-ss", f"{max(0.0, float(start_s)):.3f}",
            "-i", str(src),
            "-t", f"{max(0.1, float(dur_s)):.3f}",
            "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp),
        ]
        res = _ffmpeg_run(cmd)
        if res.returncode != 0 or not probe_video(tmp).get("duration_ms"):
            if tmp.exists():
                tmp.unlink()
            cmd = [
                *_ffmpeg_head(ff),
                "-ss", f"{max(0.0, float(start_s)):.3f}",
                "-i", str(src),
                "-t", f"{max(0.1, float(dur_s)):.3f}",
                *_native_video_codec_args(nvenc=_use_nvenc(ff)),
                *_native_container_args(tmp),
            ]
            res = _ffmpeg_run(cmd)
        if res.returncode != 0:
            if tmp.exists():
                tmp.unlink()
            raise UploadError(
                f"corte de chunk falhou: {(res.stderr or '')[:400]}")
        tmp.replace(dest)
        ready.write_text(expected, encoding="utf-8")
        return dest


def _build_sidecar(item: dict[str, Any], session_id: str, log_id: str,
                   recorded_at: str, profile: DeviceProfile,
                   video_probe: dict[str, Any], imu_csv: str,
                   frames_csv: str, *, chunk_index: int = 0,
                   duration_ms: int | None = None) -> bytes:
    """Monta o sidecar .data.zip para uma identidade específica (por conta).

    Usa o perfil do APARELHO Samsung da conta: intrinsics Brown-Conrady com
    jitter e GOP próprios + elapsedRealtimeNanos DERIVADO do recorded_at (cada
    aparelho tem boot próprio — nunca o relógio congelado).
    """
    duration_ms = int(duration_ms if duration_ms is not None else item["duration_ms"])
    wall_ms = (device_profile.recorded_at_to_wall_ms(recorded_at)
               or int(time.time() * 1000))
    return build_sidecar_zip_custom(
        session_id=session_id, chunk_index=chunk_index, duration_ms=duration_ms,
        recorded_at=recorded_at, video_probe=video_probe, log_id=log_id,
        imu_csv=imu_csv, frames_csv=frames_csv,
        imu_sample_count=item.get("n_samples"),
        calib=profile.calib,
        uptime_ns=profile.uptime_ns_at(wall_ms),
        frames_gop=profile.frames_gop,
        device_meta=profile.sidecar_device_meta(),
        platform_meta=profile.sidecar_platform_meta(),
    )


# Invalida cache `_acc*.mp4` gerado antes do envelope Android (handler
# VideoHandler, High@4.2, áudio estéreo 256k) / com B-frames.
_ACCOUNT_VIDEO_ENCODE_VERSION = "4"

_ACCOUNT_VIDEO_LOCKS = threading.Lock()
_account_video_locks: dict[str, threading.Lock] = {}


def _account_video_path(base: Path, profile: DeviceProfile) -> Path:
    """`<stem>_acc<8 hex do device_id>.mp4` — um arquivo por aparelho, estável."""
    tag = profile.device_id.replace("-", "")[:8]
    return base.with_name(f"{base.stem}_acc{tag}.mp4")


def _lock_for_account_video(path: Path) -> threading.Lock:
    key = str(path)
    with _ACCOUNT_VIDEO_LOCKS:
        lock = _account_video_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _account_video_locks[key] = lock
        return lock


def _account_video_ok_path(out: Path) -> Path:
    return out.with_name(out.name + ".ok")


def _account_video_tmp_path(out: Path) -> Path:
    """Temporário atômico com extensão `.mp4`.

    `foo.mp4.tmp` faz o ffmpeg recusar o muxer (`Unable to choose an output
    format`). `foo.tmp.mp4` + `-f mp4` é o que o encode nativo do Ego4D já usa.
    """
    return out.with_name(f"{out.stem}.tmp.mp4")


def _valid_account_video(out: Path, base: Path) -> bool:
    """Cache hit: arquivo completo, mais novo que o `_native.mp4`.

    O marcador `.ok` evita ffprobe em todo envio (abrir 500MB+ N vezes).
    Sem marcador (cache antigo), sonda uma vez e grava o `.ok`.
    """
    if not out.exists() or not base.exists():
        return False
    try:
        st = out.stat()
        if st.st_size <= 0 or st.st_mtime < base.stat().st_mtime:
            return False
        ok = _account_video_ok_path(out)
        if (not ok.exists() or ok.stat().st_mtime < st.st_mtime
                or ok.read_text(encoding="utf-8").strip()
                != _ACCOUNT_VIDEO_ENCODE_VERSION):
            return False
        return bool(probe_video(out).get("duration_ms"))
    except Exception:  # noqa: BLE001 — probe/stat falhou: trata como miss
        return False


def _per_account_video(base: Path, profile: DeviceProfile) -> Path:
    """Vídeo POR APARELHO: re-encode com ABR/GOP do perfil, cacheado no disco.

    O MESMO MP4 byte-idêntico em N contas é a assinatura de colusão mais barata.
    A identidade do arquivo é o `device_id`, não a posição no lote — a conta
    que cai em índice 0 numa campanha e outra conta índice 0 na seguinte não
    podem mais subir o mesmo `_native.mp4`.

    Encode atômico (`*.tmp.mp4` → replace) para o upload não ler arquivo
    pela metade. Cache hit (mtime ≥ native) pula o ffmpeg: retry, reset do
    sent_registry e conta nova no mesmo clipe reaproveitam o trabalho.
    """
    out = _account_video_path(base, profile)
    lock = _lock_for_account_video(out)
    with lock:
        if _valid_account_video(out, base):
            return out
        with _cpu_slots(1):
            # Pode ter ficado pronto enquanto esta conta aguardava o lock/slot.
            if _valid_account_video(out, base):
                return out
            ff = _ffmpeg_bin()
            mb = profile.video_bitrate_mbps
            br = f"{mb:.1f}M"
            tmp = _account_video_tmp_path(out)
            stale = out.with_name(out.name + ".tmp")  # legado .mp4.tmp
            for leftover in (tmp, stale):
                if leftover.exists():
                    leftover.unlink()
            input_args = [*_ffmpeg_head(ff), "-i", str(base)]
            common_args = _native_container_args(tmp)
            use_nvenc = _use_nvenc(ff)
            cmd = [
                *input_args,
                *_native_video_codec_args(
                    nvenc=use_nvenc, bitrate=br, gop=profile.frames_gop),
                *common_args,
            ]
            try:
                res = _ffmpeg_run(cmd)
            except FileNotFoundError as exc:
                raise UploadError(
                    "re-encode por conta precisa de ffmpeg") from exc
            if res.returncode != 0 and use_nvenc:
                if tmp.exists():
                    tmp.unlink()
                cmd = [
                    *input_args,
                    *_native_video_codec_args(
                        nvenc=False, bitrate=br, gop=profile.frames_gop),
                    *common_args,
                ]
                res = _ffmpeg_run(cmd)
            probed = probe_video(tmp) if res.returncode == 0 else {}
            if res.returncode != 0:
                if tmp.exists():
                    tmp.unlink()
                raise UploadError(
                    f"re-encode por conta falhou: {res.stderr.strip()[:400]}")
            try:
                if not probed.get("duration_ms"):
                    size = tmp.stat().st_size if tmp.exists() else 0
                    raise UploadError(
                        "re-encode por conta gerou vídeo sem duração "
                        f"(size={size})")
                tmp.replace(out)
                _account_video_ok_path(out).write_text(
                    _ACCOUNT_VIDEO_ENCODE_VERSION, encoding="utf-8")
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
    return out


class _ClipPrefetch:
    """Prepara o PRÓXIMO clipe (download + normalize) enquanto o atual sobe.

    O encode nativo espera os slots de CPU, então não briga com o re-encode
    por conta do clipe em voo. `take()` devolve o item se já estava pronto
    (ou espera o prefetch terminar); senão o loop principal prepara na hora.
    """

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="moneymin-prefetch")
        self._fut: Future[dict[str, Any]] | None = None
        self._uid: str | None = None
        self._guard_paths: set[Path] = set()
        self._retired: list[tuple[Future[dict[str, Any]], set[Path]]] = []

    def start(self, clip_info: dict[str, Any]) -> None:
        uid = str(clip_info.get("clip_uid") or "")
        if not uid:
            return
        if self._uid == uid and self._fut is not None:
            return
        self.cancel()
        self._uid = uid
        self._guard_paths = set(_expected_prefetch_paths(clip_info, self.work_dir))
        if clip_info.get("source") == "holoassist":
            self._fut = self._pool.submit(
                prepare_holoassist_clip, clip_info, self.work_dir
            )
            return
        try:
            clip, video = _ego_clip_inputs(clip_info)
        except Exception:  # noqa: BLE001
            self._uid = None
            self._guard_paths = set()
            return
        if clip is None or video is None:
            self._uid = None
            self._guard_paths = set()
            return
        self._fut = self._pool.submit(prepare_clip, clip, video, self.work_dir)

    def take(self, uid: str) -> dict[str, Any] | None:
        if self._fut is None or self._uid != uid:
            return None
        fut = self._fut
        self._fut = None
        self._uid = None
        self._guard_paths = set()
        try:
            return fut.result()
        except Exception:  # noqa: BLE001 — o loop principal tenta de novo
            return None

    def cancel(self) -> None:
        if self._fut is not None:
            if not self._fut.cancel():
                # Um ffmpeg/download já iniciado não pode ser cancelado à
                # força. Guarde os caminhos só enquanto ele ainda os usa.
                self._retired.append((self._fut, set(self._guard_paths)))
            self._fut = None
            self._uid = None
            self._guard_paths = set()

    def protected_paths(self) -> set[Path]:
        """Arquivos que um prefetch ativo ou já pronto ainda pode consumir."""
        active = set(self._guard_paths) if self._fut is not None else set()
        remaining: list[tuple[Future[dict[str, Any]], set[Path]]] = []
        for future, paths in self._retired:
            if not future.done():
                active.update(paths)
                remaining.append((future, paths))
        self._retired = remaining
        return active

    def shutdown(self) -> None:
        self.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)


def _prefetch_following(
    prep: _ClipPrefetch,
    clips: list[dict[str, Any]],
    current_uid: str,
    emails: list[str],
    registry_key: str,
) -> None:
    """Agenda o primeiro clipe seguinte que ainda não foi enviado a todos."""
    seen = False
    for candidate in clips:
        uid = candidate["clip_uid"]
        if not seen:
            if uid == current_uid:
                seen = True
            continue
        if sent_registry.is_sent_to_all(registry_key, uid, emails):
            continue
        prep.start(candidate)
        return


def _warm_account_videos(base: Path, emails: list[str]) -> ThreadPoolExecutor | None:
    """Dispara o re-encode por aparelho em fundo (cache hit = no-op).

    Roda durante a espera da janela / o PUT da primeira conta: o upload
    encontra o arquivo pronto ou espera o mesmo lock.
    """
    if not emails or not base.exists():
        return None
    pool = ThreadPoolExecutor(
        max_workers=ACCOUNT_ENCODE_WORKERS,
        thread_name_prefix="moneymin-acc-encode")
    for email in emails:
        pool.submit(_per_account_video, base, device_profile.get_profile(email))
    return pool


def _enforce_account_video_cache(work_dir: Path) -> tuple[int, int]:
    """Apaga variantes `_acc*.mp4` mais antigas se o cache passar do teto.

    Não mexe no `_native.mp4` (fonte do re-encode) nem no original Ego4D.
    Arquivos recém-gerados têm mtime novo e saem por último.
    """
    if not work_dir.exists():
        return 0, 0
    budget = int(float(getattr(config, "VIDEO_CACHE_GB", 40.0)) * 1024 ** 3)
    if budget <= 0:
        return 0, 0
    files: list[tuple[float, int, Path]] = []
    total = 0
    for path in work_dir.glob("*_native_acc*.mp4"):
        name = path.name
        if (not path.is_file() or name.endswith(".tmp")
                or ".tmp." in name):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, path))
        total += st.st_size
    if total <= budget:
        return 0, 0
    files.sort()  # mais antigo primeiro
    removed = freed = 0
    for _mtime, size, path in files:
        if total <= budget:
            break
        try:
            path.unlink()
        except OSError:
            continue
        ok = _account_video_ok_path(path)
        try:
            if ok.exists():
                ok.unlink()
        except OSError:
            pass
        total -= size
        removed += 1
        freed += size
    return removed, freed


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _delete_media_files(
    paths: list[Path],
    *,
    allowed_roots: tuple[Path, ...],
) -> dict[str, Any]:
    """Remove somente arquivos validados dentro dos diretórios de mídia.

    A lista é interna, mas ainda assim cada caminho é resolvido e confinado.
    Falha de limpeza nunca transforma um upload confirmado em falha.
    """
    roots = tuple(root.resolve() for root in allowed_roots)
    unique: dict[str, Path] = {}
    skipped = 0
    for raw in paths:
        candidate = Path(raw)
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            skipped += 1
            continue
        if not any(_within(resolved, root) for root in roots):
            skipped += 1
            continue
        unique[os.path.normcase(str(resolved))] = candidate

    removed = freed = 0
    errors: list[str] = []
    parents: set[Path] = set()
    for candidate in unique.values():
        try:
            if not candidate.is_file() and not candidate.is_symlink():
                continue
            size = candidate.stat().st_size if candidate.is_file() else 0
            parents.add(candidate.parent)
            candidate.unlink(missing_ok=True)
            removed += 1
            freed += size
        except OSError as exc:
            errors.append(f"{candidate.name}: {exc}")

    # Sensores HoloAssist vivem em subpastas; retire apenas diretórios vazios e
    # nunca o próprio diretório-raiz permitido.
    for parent in sorted(parents, key=lambda value: len(value.parts), reverse=True):
        current = parent
        while True:
            try:
                resolved = current.resolve(strict=False)
            except OSError:
                break
            matching_root = next(
                (root for root in roots if _within(resolved, root)), None
            )
            if matching_root is None or resolved == matching_root:
                break
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    return {
        "files": removed,
        "bytes": freed,
        "errors": errors,
        "skipped": skipped,
    }


def _media_companions(path: Path) -> list[Path]:
    """Marcadores e temporários associados a um MP4 preparado."""
    if path.suffix.lower() != ".mp4":
        return []
    return [
        path.with_name(path.name + ".ok"),
        path.with_name(path.name + ".source.json"),
        path.with_name(path.name + ".source.json.tmp"),
        path.with_name(f"{path.stem}.tmp.mp4"),
        path.with_name(path.name + ".tmp"),
        path.with_name(path.name + ".part"),
        path.with_name(path.name + ".chunk.ok"),
    ]


def _expected_prefetch_paths(
    clip_info: dict[str, Any],
    work_dir: Path,
) -> list[Path]:
    """Prevê os caminhos usados pelo próximo preparo para evitar uma corrida."""
    work = Path(work_dir)
    if clip_info.get("source") == "holoassist":
        video_name = str(
            clip_info.get("video_name") or clip_info.get("video_uid") or ""
        )
        if not video_name:
            return []
        recording = holoassist.data_dir() / "recordings" / video_name
        safe_stem = "holoassist_" + video_name.replace("/", "_").replace("\\", "_")
        paths = [
            recording / "Video_pitchshift.mp4",
            recording / "Video_compress.mp4",
            recording / "IMU" / "Accelerometer_sync.txt",
            recording / "IMU" / "Gyroscope_sync.txt",
            recording / "IMU" / "Magnetometer_sync.txt",
            work / f"{safe_stem}_native.mp4",
        ]
    else:
        clip_uid = str(
            clip_info.get("exported_clip_uid") or clip_info.get("clip_uid") or ""
        )
        parent_uid = str(clip_info.get("parent_video_uid") or clip_uid)
        media_uid = (
            str(clip_info.get("media_uid") or parent_uid)
            if clip_info.get("needs_cut") else clip_uid
        )
        if not clip_uid or not media_uid:
            return []
        paths = [
            work / f"{media_uid}.mp4",
            work / f"{clip_uid}_native.mp4",
            work / f"{parent_uid}_imu.csv",
        ]
    expanded = list(paths)
    for path in paths:
        expanded.extend(_media_companions(path))
    return expanded


def _cleanup_uploaded_item(
    item: dict[str, Any],
    work_dir: Path,
    *,
    protected_paths: set[Path] | None = None,
) -> dict[str, Any]:
    """Apaga fonte, IMU, normalizado e variantes após o lote confirmar."""
    candidates = [Path(value) for value in item.get("_cleanup_paths", [])
                  if isinstance(value, str) and value]
    base_value = item.get("video_path")
    base = Path(base_value) if isinstance(base_value, str) and base_value else None
    if base is not None:
        candidates.append(base)
        try:
            variants = [
                path for path in base.parent.glob("*_acc*.mp4")
                if path.name.startswith(base.stem + "_acc")
            ]
            chunk_variants = [
                *base.parent.glob(f"{base.stem}_ch*{base.suffix}"),
                *base.parent.glob(f"{base.stem}_acc*_ch*{base.suffix}"),
            ]
        except OSError:
            variants = []
            chunk_variants = []
        candidates.extend(variants)
        candidates.extend(chunk_variants)
    for path in list(candidates):
        candidates.extend(_media_companions(path))
    protected_keys: set[str] = set()
    for protected in protected_paths or set():
        try:
            protected_keys.add(os.path.normcase(str(protected.resolve(strict=False))))
        except OSError:
            continue
    filtered: list[Path] = []
    protected_count = 0
    for candidate in candidates:
        try:
            key = os.path.normcase(str(candidate.resolve(strict=False)))
        except OSError:
            key = ""
        if key and key in protected_keys:
            protected_count += 1
            continue
        filtered.append(candidate)
    result = _delete_media_files(
        filtered,
        allowed_roots=(Path(work_dir), holoassist.data_dir() / "recordings"),
    )
    result["protected"] = protected_count
    return result


def cleanup_media_cache(work_dir: Path | None = None) -> dict[str, Any]:
    """Limpa downloads/derivados, preservando catálogos e estado da campanha."""
    work = Path(work_dir or config.MEDIA_DATA_DIR / "ego4d")
    candidates: list[Path] = []
    if work.is_dir():
        for path in work.iterdir():
            if not path.is_file() and not path.is_symlink():
                continue
            name = path.name.lower()
            if (
                path.suffix.lower() in {".mp4", ".mkv", ".mov", ".avi", ".webm"}
                or name.endswith((
                    "_imu.csv",
                    ".mp4.ok",
                    ".mp4.source.json",
                    ".mp4.source.json.tmp",
                    ".part",
                ))
                or ".tmp." in name
            ):
                candidates.append(path)
    recordings = holoassist.data_dir() / "recordings"
    if recordings.is_dir():
        candidates.extend(
            path for path in recordings.rglob("*")
            if path.is_file() or path.is_symlink()
        )
    return _delete_media_files(
        candidates,
        allowed_roots=(work, recordings),
    )


def _all_pending_uploads_succeeded(
    pending_accounts: list[AccountSpec],
    results: dict[str, dict[str, Any]],
) -> bool:
    """Só libera a mídia quando nenhuma conta enviada ficou pendente."""
    return bool(pending_accounts) and all(
        bool(results.get(account.email, {}).get("ok"))
        for account in pending_accounts
    )


# --- envio para uma conta -----------------------------------------------------

def upload_to_account(item: dict[str, Any], account: AccountSpec,
                      task_id: str, timeout_blob: int,
                      evaluate: bool, finalize: bool,
                      on_progress: Callable[..., None] | None = None,
                      unique_video: bool = False,
                      session: Session | None = None,
                      session_cache: dict[str, Session] | None = None,
                      recorded_at: str | None = None,
                      **_legacy: Any,
                      ) -> dict[str, Any]:
    """Sobe um item JÁ PREPARADO para uma conta (o MP4 não é refeito).

    session_id é por conta (senão o Azure colide a chave). O arquivo de vídeo
    é o `_native.mp4` compartilhado, salvo `unique_video=True`.
    """
    result: dict[str, Any] = {"email": account.email, "ok": False}
    try:
        sess = session
        if sess is None and session_cache is not None:
            sess = session_cache.get(account.email)
        if sess is None:
            sess = Session.from_email(account.email)
            if session_cache is not None:
                session_cache[account.email] = sess
        if not getattr(sess, "_live", False):
            # Gate de org (quality-screen/userState + disabled) + version gate.
            sess.ensure_auth(org_key=account.org_key)
        else:
            sess.warmup()
        result["org_key"] = account.org_key
        profile = device_profile.get_profile(account.email)
        if not getattr(sess, "_moneymin_pending_pumped", False):
            recovered = pump_pending(sess, account_email=account.email)
            sess._moneymin_pending_pumped = True
            if recovered:
                result["recovered_uploads"] = len(recovered)
        dur_s = item["duration_ms"] / 1000
        session_id, log_id, recorded_at = _new_identity(
            dur_s, account.email, recorded_at=recorded_at)
        base_video = Path(item["video_path"])
        if unique_video:
            if on_progress:
                on_progress("encode", "start", 1)
            video_path = _per_account_video(base_video, profile)
            if on_progress:
                on_progress("encode", "done", 1)
        else:
            video_path = base_video
        video_probe = item.get("probe") if not unique_video else None
        if not video_probe:
            video_probe = probe_video(video_path)
        imu_csv = item.get("imu_csv") or ""
        if unique_video and item.get("imu_path") and item.get("window_s"):
            imu_csv = ego4d.build_imu_csv(
                item["imu_path"], tuple(item["window_s"]),
                duration_ms=item["duration_ms"],
                seed=f"{item['clip_uid']}|{account.email}")
        if not imu_csv:
            # Sem IMU real: gera a do APARELHO (sinal próprio por conta — nunca
            # a mesma IMU sintética para N contas).
            imu_csv = build_imu_csv(
                int(item["duration_ms"]),
                seed=f"moneymin.imu:{profile.device_id}")
        fps = video_probe.get("fps") or 30.0
        start_wall = device_profile.recorded_at_to_wall_ms(recorded_at)
        frames_offset = profile.uptime_ns_at(
            start_wall or int(time.time() * 1000))
        # frames.csv SEMPRE derivado do MP4 REAL (PTS/keyframes) + offset do
        # uptime Android do 1º frame. Nunca reusar o CSV preparado com relógio
        # em zero (desync: o metadata usa firstFrameSensorTimestampNs = uptime).
        frames_csv = build_frames_csv_from_video(
            video_path, duration_ms=int(item["duration_ms"]),
            fps=fps, gop=profile.frames_gop, offset_ns=frames_offset)
        plan = _chunk_plan(int(item["duration_ms"]))
        chunk_paths: list[Path] = []
        chunk_zips: list[bytes] = []
        chunk_recorded: list[str] = []
        for index, (start_ms, dur_ms) in enumerate(plan):
            log_id = f"{session_id}_{index}"
            rec_at = recorded_at
            if start_wall is not None:
                rec_at = device_profile.format_recorded_at(
                    start_wall / 1000.0 + start_ms / 1000.0)
            if len(plan) == 1:
                part = video_path
                part_imu, n_imu = imu_csv, int(item.get("n_samples") or 0)
                part_frames = frames_csv
            else:
                part = _chunk_video_path(video_path, index, start_ms, dur_ms)
                _cut_video_chunk(
                    video_path, part, start_ms / 1000.0, dur_ms / 1000.0)
                part_imu, n_imu = _slice_imu_csv(imu_csv, start_ms, dur_ms)
                part_frames = build_frames_csv_from_video(
                    part, duration_ms=dur_ms, fps=fps,
                    gop=profile.frames_gop,
                    offset_ns=profile.uptime_ns_at(
                        device_profile.recorded_at_to_wall_ms(rec_at)
                        or start_wall or int(time.time() * 1000)))
            part_probe = probe_video(part) if len(plan) > 1 else video_probe
            chunk_item = dict(item)
            if len(plan) > 1:
                chunk_item["n_samples"] = n_imu
            chunk_zips.append(_build_sidecar(
                chunk_item, session_id, log_id, rec_at, profile=profile,
                video_probe=part_probe, imu_csv=part_imu,
                frames_csv=part_frames, chunk_index=index,
                duration_ms=dur_ms))
            chunk_paths.append(part)
            chunk_recorded.append(rec_at)
        res = upload_session(
            sess, chunk_paths if len(chunk_paths) > 1 else chunk_paths[0],
            account.org_key,
            task_id=task_id, session_id=session_id,
            recorded_at=chunk_recorded if len(chunk_recorded) > 1 else recorded_at,
            sidecar=True,
            sidecar_data=chunk_zips if len(chunk_zips) > 1 else chunk_zips[0],
            normalize=False, register_first=True,
            persist_sidecar=True,
            evaluate=evaluate, finalize=finalize,
            timeout_blob=timeout_blob,
            profile=profile,
            suppress_per_chunk_catbear=len(chunk_paths) > 1,
            on_progress=on_progress,
        )
        result["session_id"] = res.session_id
        result["finalized"] = res.finalized
        result["finalize_status"] = res.finalize_status
        chunks_ok = bool(res.chunks) and all(c.state == "done" for c in res.chunks)
        finalize_ok = not finalize or res.finalized
        # Um blob completo sem finalize não aparece como sessão entregue. Nunca
        # registre esse estado intermediário como sucesso da conta.
        result["ok"] = chunks_ok and finalize_ok
        # consolida evaluate
        counts: dict[str, int] = {}
        fails: list[str] = []
        for c in res.chunks:
            ev = c.evaluate_result or {}
            for chk in ev.get("checks") or []:
                st = chk.get("status", "?")
                counts[st] = counts.get(st, 0) + 1
                if st == "fail":
                    fails.append(chk.get("id", "?"))
        result["evaluate"] = counts
        result["evaluate_fails"] = fails
        result["uploads"] = [c.upload_id for c in res.chunks]
        # Falhas de transporte/create/complete são devolvidas como ChunkResult,
        # não como exceção. Preserve a causa no log da campanha em vez de gravar
        # apenas ok=false com um upload_id vazio.
        chunk_errors = [c.error for c in res.chunks if c.error]
        if chunk_errors:
            result["error"] = "; ".join(dict.fromkeys(chunk_errors))
        elif chunks_ok and not finalize_ok:
            result["error"] = (
                f"sessão não finalizada (HTTP {res.finalize_status})"
            )
    except (AuthError, UploadError) as exc:
        result["error"] = str(exc)
    return result


# --- orquestração -------------------------------------------------------------

def list_campaign_logs(data_dir: Path | None = None) -> list[Path]:
    """Campaign logs salvos (`data/campaign_*.json`), mais recentes primeiro.

    Usado pela interface web (aba Histórico) para listar campanhas anteriores.
    Ignora `campaign.example.json` (não é um log de execução).
    """
    data_dir = Path(data_dir) if data_dir else config.DATA_DIR
    paths: list[Path] = []
    if data_dir.exists():
        for p in data_dir.iterdir():
            if (p.name.startswith("campaign_") and p.name.endswith(".json")
                    and p.name != "campaign.example.json"):
                paths.append(p)
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths


def run_campaign(
    config: CampaignConfig,
    log: CampaignLog | None = None,
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> CampaignLog:
    """Executa a campanha: para cada task, escolhe clipes, prepara e envia para
    todas as contas. Devolve o log com os resultados por item.

    Hooks opcionais (usados pela interface web):
      - `progress(kind, payload)` — eventos de avanço: campaign_start,
        task_start, clip_prepare_start, clip_ready, account_start,
        account_done, item_done, campaign_done, campaign_stopped,
        storage_cleanup,
        delay_start/delay_tick (intervalo entre vídeos),
        window_wait_start/window_wait_tick (aguardando a janela de horário)
        e log (mensagens).
      - `should_stop() -> bool` — se True, a campanha para cooperativamente
        entre clipes/contas e durante as esperas; o log parcial é salvo mesmo assim.
    O log é salvo de forma INCREMENTAL após cada item (um crash não perde a
    campanha inteira).
    - Dedup: a seleção automática pula clipes já enviados a TODAS as contas
      (`data/sent_videos.json`, ver `sent_registry`); no envio, contas que já
      receberam o clipe são puladas (`account_done` com `skipped=True`). Se não
      sobrar nenhum clipe novo de um cenário (100% enviado), o registro daquele
      cenário é resetado (evento `sent_reset`) e a seleção recomeça do início.
    - `config.delay_mode`: aplicado na TROCA de vídeo (nunca entre contas do
      mesmo vídeo): "clip" = espera a duração do vídeo recém-enviado
      (parece gravação real), "fixed" = `delay_s` segundos, "off" = sem espera.
    - `config.account_workers`: contas em voo por vídeo, cortado por
      `max_account_workers()` (teto padrão 6 depois de um único ffmpeg).
      `account_gap_s` 0 = sem espera entre contas.
      A espera de gravação corre em paralelo para todas as contas do lote,
      sem ocupar workers; cada envio só é liberado ao fim da sua reserva.
    - `config.active_hours`: janela local de envio (ex.: (7, 18)) — fora dela a
      campanha aguarda a próxima abertura antes de cada envio.
    - `config.cleanup_after_upload`: mantém o prefetch do próximo vídeo e apaga
      a mídia somente quando todas as contas pendentes do item confirmaram
      sucesso. Arquivos compartilhados com o prefetch ficam protegidos até o
      próximo item; em falha, parada ou lote parcial tudo é mantido.
    """
    def _emit(kind: str, **payload: Any) -> None:
        if progress:
            progress(kind, payload)

    def _log(msg: str) -> None:
        try:
            print(msg)
        except (OSError, UnicodeError):
            # stdout quebrado (ex.: servidor web órfão, sem console/pipe vivo) —
            # ou console com encoding limitado. O print nunca pode derrubar a
            # campanha; o evento basta para a UI.
            pass
        _emit("log", message=msg)

    def _maybe_shuffle(items: list) -> list:
        out = list(items)
        if config.shuffle_schedule and len(out) > 1:
            random.shuffle(out)
        return out

    dataset_provider = normalize_dataset_provider(config.dataset_provider)

    log = log or CampaignLog(started_at=_recorded_at_now(),
                             accounts=[a.email for a in config.accounts])
    work_dir = Path(config.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    prefetch = _ClipPrefetch(work_dir)
    sessions: dict[str, Session] = {}
    banned: set[str] = set()
    abort_reason: list[str] = []
    account_seconds: dict[str, float] = {}
    quota_s = max(0.0, float(config.target_hours_per_account or 0) * 3600.0)
    n_tasks = max(1, len(config.tasks))
    per_task_cap_s = (quota_s / n_tasks) * 1.3 if quota_s else 0.0
    _emit("campaign_start", accounts=[a.email for a in config.accounts],
          tasks=[t.task_name or t.scenario for t in config.tasks],
          dataset=dataset_provider,
          cleanup_after_upload=bool(config.cleanup_after_upload))

    # Fica True somente depois que um vídeo foi realmente enviado. O intervalo
    # é consumido uma vez antes do próximo candidato útil; falhas de prepare e
    # clipes pulados não podem criar uma segunda espera.
    delay_pending = False
    last_dur_s = 0.0  # duração do último clipe enviado (p/ delay_mode="clip")
    last_batch_started_at: float | None = None
    for tsk in _maybe_shuffle(list(config.tasks)):
        if should_stop and should_stop():
            _log("  [!] campanha interrompida pelo usuário")
            _emit("campaign_stopped", reason="parada pelo usuário")
            break
        display_name = tsk.task_label or tsk.task_name or tsk.scenario
        registry_key = tsk.registry_key
        _log(f"\n=== categoria={display_name} (n={tsk.count}) ===")
        _emit("task_start", scenario=tsk.scenario, task_name=display_name,
              count=tsk.count)
        automatic_selection = not tsk.clip_uids
        try:
            if tsk.clip_uids:
                # Seleção explícita continua sujeita às mesmas regras da
                # automática; config antiga/manual não pode furar o matching.
                clips = []
                ego_compatible: dict[str, dict[str, Any]] | None = None
                if tsk.task_name and task_matching.rule_for(tsk.task_name):
                    ego_compatible = {
                        candidate["clip_uid"]: candidate
                        for candidate in _compatible_task_clips(
                            tsk.task_name, "ego4d")
                        if tsk.min_dur_s <= float(candidate.get("dur_s") or 0)
                        <= tsk.max_dur_s
                    }
                for uid in tsk.clip_uids:
                    if uid.startswith("holoassist:"):
                        if dataset_provider == "ego4d":
                            _log(f"  [!] clipe {uid} pertence ao HoloAssist — pulando")
                            continue
                        # O UID explícito não pode furar categoria, duração ou
                        # política de qualidade. Só aceita se também estiver no
                        # pool estrito da tarefa selecionada.
                        compatible = {
                            candidate["clip_uid"]: candidate
                            for candidate in holoassist.list_clips(
                                tsk.task_name or "",
                                min_dur_s=tsk.min_dur_s,
                                max_dur_s=tsk.max_dur_s,
                            )
                        }
                        holo_clip = compatible.get(uid)
                        if holo_clip is None:
                            _log(
                                f"  [!] clipe {uid} incompatível com "
                                f"'{display_name}' — pulando"
                            )
                            continue
                        clips.append(holo_clip)
                        continue
                    if dataset_provider == "holoassist":
                        _log(f"  [!] clipe {uid} não pertence ao HoloAssist — pulando")
                        continue
                    if ego_compatible is not None:
                        candidate = ego_compatible.get(uid)
                        if candidate is None:
                            _log(
                                f"  [!] clipe {uid} incompatível com "
                                f"'{display_name}' — pulando"
                            )
                            continue
                        clips.append(candidate)
                        continue
                    clip, video = ego4d.find_clip(uid)
                    if clip is None or video is None:
                        _log(f"  [!] clipe {uid} não encontrado no manifest — pulando")
                        continue
                    candidate = ego4d._clip_record(clip, video)
                    if not (tsk.min_dur_s <= float(candidate.get("dur_s") or 0)
                            <= tsk.max_dur_s):
                        _log(f"  [!] clipe {uid} fora da duração permitida — pulando")
                        continue
                    clips.append(candidate)
            else:
                # seleção automática: pula clipes já enviados a TODAS as contas
                # (registro em data/sent_videos.json — ver sent_registry)
                emails = [a.email for a in config.accounts]
                shorts: list[dict[str, Any]] = []
                if dataset_provider in ("all", "ego4d"):
                    if tsk.task_name and task_matching.rule_for(tsk.task_name):
                        if ego4d.has_timed_narrations():
                            # Biblioteca completa: calcula trechos diretamente
                            # das narrações temporizadas licenciadas.
                            shorts = ego4d.list_task_spans(
                                tsk.task_name, min_dur_s=tsk.min_dur_s,
                                max_dur_s=tsk.max_dur_s)
                        else:
                            # Instalação nova: usa o índice portátil mínimo. A
                            # mídia e a IMU continuam sendo baixadas sob demanda
                            # com a credencial Ego4D do próprio usuário.
                            shorts = [
                                clip for clip in _compatible_task_clips(
                                    tsk.task_name, "ego4d")
                                if tsk.min_dur_s <= float(clip.get("dur_s") or 0)
                                <= tsk.max_dur_s
                            ]
                    else:
                        shorts = ego4d.list_clips(
                            scenario=tsk.scenario,
                            min_dur_s=tsk.min_dur_s, max_dur_s=tsk.max_dur_s,
                            gopro_minor=tsk.gopro_minor, max_results=None)
                if tsk.task_name and dataset_provider in ("all", "holoassist"):
                    try:
                        strict_holoassist = holoassist.list_clips(
                            tsk.task_name,
                            min_dur_s=tsk.min_dur_s,
                            max_dur_s=tsk.max_dur_s,
                        )
                    except FileNotFoundError:
                        strict_holoassist = []
                    # Fonte complementar primeiro: são sessões inteiras de uma
                    # tarefa rotulada, não inferências por texto de narração.
                    shorts = [*strict_holoassist, *shorts]
                all_clips = shorts
                fresh = [c for c in all_clips
                         if not sent_registry.is_sent_to_all(
                             registry_key, c["clip_uid"], emails)]
                skipped_n = len(all_clips) - len(fresh)
                if skipped_n:
                    _log(f"  (pulando {skipped_n} clipe(s) já enviado(s) anteriormente)")
                if not fresh and all_clips:
                    # 100% do cenário já foi enviado: reseta e recomeça do início
                    sent_registry.reset(registry_key)
                    _log(f"  [i] todos os {len(all_clips)} clipe(s) de "
                         f"'{tsk.scenario}' já foram enviados — registro resetado, "
                         "recomeçando do início")
                    _emit("sent_reset", scenario=tsk.scenario,
                          eligible=len(all_clips))
                    fresh = all_clips
                hours = sum(float(c.get("dur_s") or 0) for c in fresh) / 3600
                merged_n = sum(1 for c in fresh if c.get("needs_cut"))
                _log(f"  pool: {len(shorts)} trechos puros → {len(fresh)} "
                     f"sessões ({merged_n} cortes do vídeo-pai, {hours:.1f}h)")
                clips = ego4d.prefer_long_clips(
                    fresh, shuffle=config.shuffle_schedule)
        except Exception as exc:  # noqa: BLE001 — uma categoria não mata as demais
            error = f"{type(exc).__name__}: {exc}"
            _log(f"  [!] falha ao selecionar clipes: {error}")
            _emit("task_error", scenario=tsk.scenario, task_name=display_name,
                  error=error)
            continue
        if not clips:
            _log(f"  [!] nenhum clipe encontrado p/ cenário '{tsk.scenario}'")
            continue
        if tsk.clip_uids:
            clips = _maybe_shuffle(clips)
        task_accounts = _maybe_shuffle(list(config.accounts))
        completed_items = 0
        needed_items = (10 ** 9 if quota_s else
                        (tsk.count if config.share_clips
                         else tsk.count * max(1, len(config.accounts))))
        task_sends: dict[str, int] = {}
        task_seconds: dict[str, float] = {}
        for clip_info in clips:
            if abort_reason:
                break
            if quota_s and all(
                    account_seconds.get(a.email, 0) >= quota_s
                    for a in config.accounts):
                break
            if automatic_selection and completed_items >= needed_items:
                break
            if should_stop and should_stop():
                _log("  [!] campanha interrompida pelo usuário")
                _emit("campaign_stopped", reason="parada pelo usuário")
                break
            # anti-desperdício: se TODAS as contas da campanha já receberam este
            # clipe (seleção explícita do wizard ou corrida entre campanhas),
            # pula ANTES do intervalo e de baixar/reencodar.
            sent_to = sent_registry.sent_emails(registry_key, clip_info["clip_uid"])
            if all(a.email in sent_to for a in config.accounts):
                _log(f"  clipe {clip_info['clip_uid'][:12]} já enviado a todas as "
                     f"contas — pulando (sem baixar/reencodar)")
                # account_done mantém o progresso da UI consistente; o item nem
                # chega a existir (nada a registrar no log da campanha)
                for account in config.accounts:
                    _emit("account_done", clip_uid=clip_info["clip_uid"],
                          task=tsk.scenario, email=account.email, ok=True,
                          skipped=True)
                continue
            # Intervalo ENTRE VÍDEOS: é devido uma única vez depois de um envio
            # bem-sucedido. Assim, um prepare que falhar após esta espera não faz
            # o próximo clipe aguardar tudo novamente.
            if delay_pending:
                gap = 0.0
                if config.delay_mode == "clip":
                    elapsed = (time.monotonic() - last_batch_started_at
                               if last_batch_started_at is not None else 0.0)
                    # Ritmo entre INÍCIOS de vídeos. O tempo consumido em
                    # encode/upload já conta; não espere a duração inteira duas
                    # vezes quando o lote levou tanto quanto o próprio vídeo.
                    gap = max(0.0, last_dur_s - elapsed)
                elif config.delay_mode == "fixed":
                    gap = config.delay_s
                if config.shuffle_schedule and config.delay_mode != "off":
                    # Jitter no intervalo: não corta vídeo, só tira o metrônomo.
                    gap += random.uniform(20.0, 90.0)
                if gap > 0:
                    _emit("delay_start", delay_s=gap, mode=config.delay_mode)
                    if _interruptible_sleep(gap, should_stop, _emit,
                                            tick_every=_tick_every(gap)):
                        _log("  [!] campanha interrompida durante o intervalo")
                        _emit("campaign_stopped",
                              reason="parada durante o intervalo")
                        break
                delay_pending = False
            _log(f"  clipe: {clip_info['clip_uid'][:12]} "
                 f"{clip_info.get('dur_s')}s "
                 f"device={clip_info.get('device')}")
            _emit("clip_prepare_start", clip_uid=clip_info["clip_uid"],
                  task=tsk.scenario, dur_s=clip_info["dur_s"])
            try:
                item = prefetch.take(clip_info["clip_uid"])
                if item is None:
                    def _prepare_progress(
                        phase: str,
                        payload: dict[str, Any],
                        _clip_uid: str = str(clip_info["clip_uid"]),
                    ) -> None:
                        _emit(
                            "clip_prepare_progress",
                            clip_uid=_clip_uid,
                            phase=phase,
                            **payload,
                        )

                if item is None and clip_info.get("source") == "holoassist":
                    item = prepare_holoassist_clip(
                        clip_info, work_dir, progress=_prepare_progress
                    )
                if item is None:
                    clip, video = _ego_clip_inputs(clip_info)
                    if clip is None or video is None:
                        raise RuntimeError("clipe ou vídeo pai ausente no manifest")
                    item = prepare_clip(
                        clip, video, work_dir, progress=_prepare_progress)
                # A duração real pode diferir das anotações ou de um cache antigo.
                # Nunca envie um MP4 que ultrapasse o teto escolhido.
                if float(item["duration_ms"]) > tsk.max_dur_s * 1000:
                    raise ValueError(
                        f"vídeo preparado excede a duração máxima de {tsk.max_dur_s:g}s"
                    )
            except Exception as exc:  # noqa: BLE001 — pula o clipe, segue a campanha
                error = f"{type(exc).__name__}: {exc}"
                _log(f"    [!] prepare falhou: {error}")
                _emit("clip_prepare_done", clip_uid=clip_info["clip_uid"],
                      ok=False, error=error)
                continue
            _emit("clip_ready", clip_uid=clip_info["clip_uid"],
                  duration_ms=item["duration_ms"], imu_real=item["imu_real"])
            # Só antecipa outro clipe quando ele será realmente necessário.
            # Uma campanha n=1 não deve baixar material residual em fundo.
            current_dur_s = float(item.get("duration_ms") or 0) / 1000.0
            if quota_s:
                # Adiante o próximo quando ele ainda será necessário, mas não
                # baixe um clipe órfão se este já completa a meta/cota da task.
                should_prefetch = any(
                    account_seconds.get(account.email, 0.0) + current_dur_s
                    < quota_s
                    and task_seconds.get(account.email, 0.0) + current_dur_s
                    < per_task_cap_s
                    for account in config.accounts
                )
            else:
                should_prefetch = completed_items + 1 < needed_items
            if should_prefetch:
                _prefetch_following(
                    prefetch,
                    clips,
                    clip_info["clip_uid"],
                    [account.email for account in config.accounts],
                    registry_key,
                )
            item["task_id"] = tsk.task_id
            item["task_name"] = display_name
            item["task_scenario"] = tsk.scenario
            item["accounts"] = []
            pending_accounts: list[AccountSpec] = []
            account_results: dict[str, dict[str, Any]] = {}
            for account in task_accounts:
                if should_stop and should_stop():
                    _log("  [!] campanha interrompida pelo usuário")
                    _emit("campaign_stopped", reason="parada pelo usuário")
                    break
                if account.email in banned:
                    skip_res = {"email": account.email, "org_key": account.org_key,
                                "ok": False, "skipped": True,
                                "error": "conta desativada — fora desta campanha"}
                    account_results[account.email] = skip_res
                    _emit("account_done", clip_uid=clip_info["clip_uid"],
                          task=tsk.scenario, email=account.email, ok=False,
                          skipped=True, error=skip_res["error"])
                    continue
                if quota_s:
                    if account_seconds.get(account.email, 0) >= quota_s:
                        continue
                    if task_seconds.get(account.email, 0) >= per_task_cap_s:
                        continue
                elif not config.share_clips:
                    if task_sends.get(account.email, 0) >= tsk.count:
                        continue
                if not config.allow_new_accounts:
                    age = device_profile.profile_age_days(account.email)
                    if age < float(config.min_account_age_days or 0):
                        skip_res = {
                            "email": account.email, "org_key": account.org_key,
                            "ok": True, "skipped": True,
                            "error": (f"nova demais: {age:.1f}d de aparelho "
                                      f"(mínimo {config.min_account_age_days:g}d)"),
                        }
                        account_results[account.email] = skip_res
                        _log(f"      -> {account.email} (nova demais — pulando)")
                        _emit("account_done", clip_uid=clip_info["clip_uid"],
                              task=tsk.scenario, email=account.email, ok=True,
                              skipped=True, error=skip_res["error"])
                        continue
                # dedup por conta: quem já recebeu este clipe é pulado
                if account.email in sent_registry.sent_emails(registry_key,
                                                              clip_info["clip_uid"]):
                    _log(f"      -> {account.email} (já recebeu este clipe — pulando)")
                    skip_res = {"email": account.email, "org_key": account.org_key,
                                "ok": True, "skipped": True}
                    account_results[account.email] = skip_res
                    # account_done mantém o progresso da UI consistente
                    _emit("account_done", clip_uid=clip_info["clip_uid"],
                          task=tsk.scenario, email=account.email, ok=True,
                          skipped=True)
                    continue
                pending_accounts.append(account)

            if should_stop and should_stop():
                _log("  [!] campanha interrompida pelo usuário")
                _emit("campaign_stopped", reason="parada pelo usuário")
                break

            if pending_accounts and not config.share_clips:
                pending_accounts = pending_accounts[:1]
                _log(f"  clipe exclusivo para {pending_accounts[0].email} "
                     "(não replica o mesmo vídeo nas outras contas)")

            workers = clamp_account_workers(
                int(config.account_workers or 1),
                len(pending_accounts) or 1)
            warm_pool = None
            if config.unique_video:
                # Re-encode só das que vão sair agora (+1 de folga).
                warm_n = min(len(pending_accounts), workers + 1)
                warm_pool = _warm_account_videos(
                    Path(item["video_path"]),
                    [account.email for account in pending_accounts[:warm_n]])
            warm_state: list[ThreadPoolExecutor | None] = [warm_pool]

            def _stop_warm(
                state: list[ThreadPoolExecutor | None] = warm_state,
            ) -> None:
                pool = state[0]
                state[0] = None
                if pool is not None:
                    pool.shutdown(wait=False, cancel_futures=True)

            # A janela vale para o lote: todas as contas deste vídeo começam
            # dentro do horário permitido.
            if pending_accounts and config.active_hours:
                if _wait_for_window(config.active_hours, should_stop, _emit):
                    _log("  [!] campanha interrompida fora do horário de envio")
                    _emit("campaign_stopped", reason="parada fora do horário")
                    _stop_warm()
                    break

            def _send_account(
                upload_idx: int,
                account: AccountSpec,
                scheduled_recorded_at: str | None = None,
                clip_uid: str = clip_info["clip_uid"],
                task_scenario: str = tsk.scenario,
                task_id: str = tsk.task_id,
                upload_item: dict[str, Any] = item,
            ) -> dict[str, Any]:
                _log(f"      -> {account.email}")
                _emit("account_start", clip_uid=clip_uid,
                      task=task_scenario, email=account.email)

                def _account_progress(phase: str, state: str,
                                      attempt: int, **details: Any) -> None:
                    _emit("account_progress", clip_uid=clip_uid,
                          task=task_scenario, email=account.email,
                          phase=phase, state=state, attempt=attempt,
                          **details)  # noqa: B023 — kwargs pertence ao callback

                return upload_to_account(
                    upload_item, account, task_id, config.timeout_blob,
                    config.evaluate, config.finalize,
                    on_progress=_account_progress,
                    unique_video=bool(config.unique_video),
                    session_cache=sessions,
                    recorded_at=scheduled_recorded_at)

            def _safe_send_account(upload_idx: int,
                                   account: AccountSpec,
                                   scheduled_recorded_at: str | None = None,
                                   ) -> dict[str, Any]:
                """Isola qualquer falha inesperada, inclusive no modo sequencial."""
                try:
                    return _send_account(
                        upload_idx, account, scheduled_recorded_at)
                except Exception as exc:  # noqa: BLE001 — uma conta não mata o lote
                    return {"email": account.email, "org_key": account.org_key,
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"}

            def _send_account_with_recovery(
                upload_idx: int,
                account: AccountSpec,
                scheduled_recorded_at: str | None = None,
            ) -> dict[str, Any]:
                """Repete a sessão inteira até a conta concluir ou a campanha parar."""
                max_attempts = max(1, int(config.account_max_attempts or 1))
                last_result: dict[str, Any] = {
                    "email": account.email, "org_key": account.org_key,
                    "ok": False, "error": "envio não iniciado",
                }
                for attempt in range(1, max_attempts + 1):
                    if should_stop and should_stop():
                        return {**last_result, "stopped": True}
                    last_result = _safe_send_account(
                        upload_idx, account, scheduled_recorded_at)
                    last_result["campaign_attempts"] = attempt
                    if last_result.get("ok"):
                        return last_result
                    if _is_disabled_error(last_result.get("error")):
                        # Retry em conta já derrubada só acelera o lote inteiro.
                        return last_result
                    if should_stop and should_stop():
                        return last_result
                    if attempt < max_attempts:
                        retry_s = min(
                            max(0.0, float(config.account_retry_s))
                            * (2 ** (attempt - 1)),
                            120.0,
                        )
                        err = str(last_result.get("error") or "falhou")[:220]
                        _log(f"      [retry] {account.email}: tentativa "
                             f"{attempt}/{max_attempts} falhou ({err}) — nova "
                             f"tentativa em {retry_s:.0f}s")
                        _emit("account_retry", email=account.email,
                              attempt=attempt, max_attempts=max_attempts,
                              delay_s=retry_s,
                              error=last_result.get("error"))
                        if _interruptible_sleep(
                                retry_s, should_stop, _emit,
                                kind="account_retry_tick",
                                tick_every=_tick_every(retry_s)):
                            return last_result
                return last_result

            def _record_account(
                account: AccountSpec,
                acc_res: dict[str, Any],
                results: dict[str, dict[str, Any]] = account_results,
                sent_key: str = registry_key,
                clip_uid: str = clip_info["clip_uid"],
                task_scenario: str = tsk.scenario,
                sends: dict[str, int] = task_sends,
                seconds: dict[str, float] = task_seconds,
                duration_s: float = float(item.get("duration_ms") or 0) / 1000.0,
            ) -> None:
                results[account.email] = acc_res
                ok = acc_res.get("ok")
                if ok:
                    sent_registry.mark_sent(sent_key, clip_uid,
                                            account.email)
                    if not acc_res.get("skipped"):
                        sends[account.email] = sends.get(account.email, 0) + 1
                        account_seconds[account.email] = (
                            account_seconds.get(account.email, 0.0) + duration_s)
                        seconds[account.email] = (
                            seconds.get(account.email, 0.0) + duration_s)
                ev = acc_res.get("evaluate") or {}
                _log(f"         ok={ok}  finalized={acc_res.get('finalized')} "
                     f"evaluate={ev}" + (f"  err={acc_res.get('error','')[:80]}" if not ok else ""))
                _emit("account_done", clip_uid=clip_uid,
                      task=task_scenario, email=account.email, ok=ok,
                      finalized=acc_res.get("finalized"),
                      evaluate=ev, error=acc_res.get("error"),
                      session_id=acc_res.get("session_id"))
                if _is_disabled_error(acc_res.get("error")):
                    banned.add(account.email)
                    abort_reason.append(account.email)

            batch_started_at = time.monotonic() if pending_accounts else None
            # Reserva TODAS as contas no mesmo instante, antes de disputar
            # vagas de upload. O fim anterior de cada conta continua valendo.
            # A fila ordenada permite enviar contas livres mesmo se outra tem
            # uma reserva futura (por exemplo, após retomar uma campanha).
            batch_epoch = time.time()
            duration_s = float(item.get("duration_ms") or 0) / 1000.0
            queued: list[tuple[float, int, AccountSpec, str | None]] = []
            for upload_idx, account in enumerate(pending_accounts):
                if should_stop and should_stop():
                    break
                ready_at, recorded_at = batch_epoch, None
                if config.realistic_timeline:
                    slot = recording_timeline.reserve(
                        account.email, duration_s, now=batch_epoch)
                    ready_at, recorded_at = slot.end_epoch, slot.recorded_at
                    wait_s = max(0.0, ready_at - time.time())
                    _log(f"      [recording] {account.email}: intervalo "
                         f"{recorded_at} ({duration_s:.0f}s); envio após a gravação")
                    _emit("recording_wait_start", email=account.email,
                          recorded_at=recorded_at,
                          duration_s=duration_s, delay_s=wait_s)
                queued.append((ready_at, upload_idx, account, recorded_at))
            queued.sort(key=lambda entry: (entry[0], entry[1]))

            if queued:
                _log(f"  enviando para {len(pending_accounts)} conta(s) "
                     f"({workers} simultânea(s), teto {max_account_workers()})")
                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="moneymin-upload") as pool:
                    futures = {}
                    launched = 0
                    accepting = True
                    next_batch_tick = time.monotonic() + 5.0

                    def _stagger_launch(launched_count: int) -> bool:
                        """Espera o jitter antes da próxima vaga. True = parou."""
                        if config.account_gap_s <= 0 or launched_count <= 0:
                            return False
                        gap = random.uniform(0.6, 1.1) * float(config.account_gap_s)
                        _emit("account_gap_start", delay_s=gap)
                        if _interruptible_sleep(
                                gap, should_stop, _emit,
                                kind="account_gap_tick",
                                tick_every=_tick_every(gap)):
                            _log("  [!] campanha interrompida no intervalo entre contas")
                            _emit("campaign_stopped",
                                  reason="parada no intervalo entre contas")
                            return True
                        return False

                    while queued or futures:
                        # Colhe todas as conclusões antes de abrir novas vagas:
                        # uma conta desativada ou parada não libera a fila.
                        for future in [f for f in futures if f.done()]:
                            account = futures.pop(future)
                            _record_account(account, future.result())
                        if abort_reason or (should_stop and should_stop()):
                            accepting = False
                        while accepting and queued and len(futures) < workers:
                            if queued[0][0] > time.time():
                                break
                            if _stagger_launch(launched) or (should_stop and should_stop()):
                                accepting = False
                                break
                            _ready_at, idx, account, recorded_at = queued.pop(0)
                            future = pool.submit(
                                _send_account_with_recovery, idx, account, recorded_at)
                            futures[future] = account
                            launched += 1
                        if not futures:
                            if not accepting or not queued:
                                break
                            # Nenhum worker fica dormindo pela gravação. Espera
                            # só até a próxima conta ficar pronta, não a última.
                            wait_s = max(0.0, queued[0][0] - time.time())
                            if _interruptible_sleep(
                                    wait_s, should_stop,
                                    partial(_emit, email=queued[0][2].email,
                                            pending_accounts=len(queued)),
                                    kind="recording_wait_tick",
                                    tick_every=_tick_every(wait_s)):
                                accepting = False
                            continue
                        timeout = 0.5
                        if accepting and queued and len(futures) < workers:
                            timeout = min(timeout, max(0.0, queued[0][0] - time.time()))
                        wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
                        if time.monotonic() >= next_batch_tick:
                            _emit("batch_tick", clip_uid=clip_info["clip_uid"],
                                  pending_accounts=len(futures),
                                  elapsed_s=int(time.monotonic() - batch_started_at))
                            next_batch_tick = time.monotonic() + 5.0

            item["accounts"] = [account_results[a.email] for a in config.accounts
                                if a.email in account_results]
            sent_now = any(
                result.get("ok") and not result.get("skipped")
                for result in account_results.values()
            )
            if sent_now:
                last_dur_s = item["duration_ms"] / 1000
                last_batch_started_at = batch_started_at
                delay_pending = True
            # Salva inclusive um lote parcial interrompido: os envios que já
            # terminaram não desaparecem do histórico nem da retomada.
            if account_results:
                log.add_item({k: v for k, v in item.items()
                              if k not in (
                                  "imu_csv", "frames_csv", "probe",
                                  "_cleanup_paths",
                              )})
                log.save()  # incremental: não perde a campanha em caso de crash
            if should_stop and should_stop():
                if account_results:
                    _emit("item_done", clip_uid=clip_info["clip_uid"],
                          task=tsk.scenario, partial=True)
                _log("  [!] campanha interrompida pelo usuário")
                _emit("campaign_stopped", reason="parada pelo usuário")
                _stop_warm()
                break
            all_pending_succeeded = _all_pending_uploads_succeeded(
                pending_accounts, account_results
            )
            # Não espera tarefa de fundo: todas as variantes usadas por contas
            # bem-sucedidas já terminaram; o prefetch do próximo segue ativo.
            _stop_warm()
            if abort_reason:
                _log("  [!] conta desativada no HUB "
                     f"({abort_reason[0]}) — parando a campanha para não "
                     "queimar as outras contas")
                _emit("campaign_stopped", reason="conta desativada",
                      email=abort_reason[0])
                break
            failed_accounts = [
                result for result in account_results.values()
                if not result.get("ok") and not result.get("skipped")
            ]
            if config.require_all_accounts and failed_accounts:
                details = "; ".join(
                    f"{result.get('email')}: {result.get('error') or 'erro desconhecido'}"
                    for result in failed_accounts
                )
                _emit("item_incomplete", clip_uid=clip_info["clip_uid"],
                      task=tsk.scenario,
                      accounts=[r.get("email") for r in failed_accounts])
                prefetch.shutdown()
                raise RuntimeError(
                    "lote incompleto após todas as tentativas; "
                    "a campanha não avançou. Contas pendentes: " + details
                )
            if pending_accounts:
                completed_items += 1
                if config.cleanup_after_upload and all_pending_succeeded:
                    cleanup = _cleanup_uploaded_item(
                        item,
                        work_dir,
                        protected_paths=prefetch.protected_paths(),
                    )
                    _emit(
                        "storage_cleanup",
                        clip_uid=clip_info["clip_uid"],
                        files=cleanup["files"],
                        bytes=cleanup["bytes"],
                        errors=cleanup["errors"],
                        protected=cleanup["protected"],
                    )
                    _log(
                        "  armazenamento: removeu "
                        f"{cleanup['files']} arquivo(s), "
                        f"{cleanup['bytes'] / (1024 ** 2):.1f} MB"
                        + (f"; {cleanup['protected']} arquivo(s) em uso pelo próximo preparo"
                           if cleanup["protected"] else "")
                        + (f"; falhas: {len(cleanup['errors'])}"
                           if cleanup["errors"] else "")
                    )
                elif config.cleanup_after_upload and not all_pending_succeeded:
                    _log(
                        "  armazenamento: mídia mantida porque há conta "
                        "pendente ou envio com falha"
                    )
                else:
                    removed, freed = _enforce_account_video_cache(work_dir)
                    if removed:
                        _log(f"  cache de variantes: liberou {removed} arquivo(s), "
                             f"{freed / (1024 ** 3):.1f} GB")
                _emit("item_done", clip_uid=clip_info["clip_uid"],
                      task=tsk.scenario, partial=False)
            # (log: sem os blobs/csv brutos — grandes; identity fica por conta)

        if abort_reason:
            break
        if quota_s and all(account_seconds.get(a.email, 0) >= quota_s
                           for a in config.accounts):
            _log(f"  [i] meta de {quota_s / 3600:.1f}h por conta atingida")
            break

    prefetch.shutdown()
    log_path = log.save()
    _log(f"\nlog salvo: {log_path.name}")
    if not abort_reason:
        # Só o nome do arquivo vai para a UI — nada de caminhos absolutos.
        _emit("campaign_done", log_path=log_path.name)
    return log


def session_result(
    email: str,
    org_key: str,
    session_id: str,
    *,
    session: Session | None = None,
) -> dict[str, Any]:
    """Consulta o estado de uma sessão (preview + quality scores) numa conta."""
    sess = session or Session.from_email(email)
    http_status, body = sess.get(
        f"/api/v1/organizations/{org_key}/sessions/{session_id}")
    import json as _json
    if http_status != 200:
        raise RuntimeError(
            f"Minute devolveu HTTP {http_status} ao consultar a sessão")
    d = _json.loads(body) if isinstance(body, str) else body
    if not isinstance(d, dict):
        raise RuntimeError("Minute devolveu um estado de sessão ilegível")
    files = d.get("files") or []
    uf = d.get("unprocessedFiles") or []
    status = "processing"
    quality = None
    if files and not uf:
        quality = files[0].get("quality")
        status = "preview_ready"
    elif uf:
        states = [str(item.get("previewStatus") or "pending") for item in uf]
        if any(value == "unavailable" for value in states):
            status = "unprocessed:unavailable"
        elif any(value not in {"pending", "processing"} for value in states):
            status = "unprocessed:" + next(
                value for value in states
                if value not in {"pending", "processing"})
        else:
            status = "processing"
    preview_states = [str(item.get("previewStatus") or "pending") for item in uf]
    return {
        "session_id": session_id, "email": email, "status": status,
        "quality": quality, "task": d.get("taskName"),
        "ready_files": len(files),
        "pending_files": sum(
            value in {"pending", "processing"} for value in preview_states),
        "unavailable_files": sum(
            value == "unavailable" for value in preview_states),
        "total_files": len(files) + len(uf),
    }


@lru_cache(maxsize=1)
def _task_candidates() -> tuple[dict[str, Any], ...]:
    """Catálogo Ego4D+IMU (clipes oficiais). A junção por task vem depois."""
    return tuple(ego4d.list_clips(
        scenario=None, min_dur_s=60, max_dur_s=1800,
        require_imu=True, max_results=None))


_RANK_LOCK = threading.Lock()


def _rank_cache_path() -> Path:
    return config.DATA_DIR / "task_rank_cache.pkl"


def _rank_seed_path() -> Path:
    """Índice portátil mínimo distribuído com o motor local."""
    return Path(__file__).with_name("resources") / "ego4d_task_rank_seed.json.gz"


@lru_cache(maxsize=1)
def _load_rank_seed() -> dict[str, tuple[dict[str, Any], ...]] | None:
    """Carrega IDs e janelas; o arquivo não contém mídia, segredo ou narração."""
    path = _rank_seed_path()
    try:
        payload = json.loads(gzip.decompress(path.read_bytes()))
    except (OSError, ValueError, TypeError, gzip.BadGzipFile):
        return None
    if payload.get("schema") != 1 or not isinstance(payload.get("tasks"), dict):
        return None
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for name, items in payload["tasks"].items():
        if not isinstance(name, str) or not isinstance(items, list):
            return None
        valid: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                return None
            if (str(item.get("parent_video_uid") or "")
                    in ego4d.KNOWN_INCOMPLETE_IMU_VIDEO_UIDS):
                continue
            try:
                duration = float(item.get("dur_s") or 0)
            except (TypeError, ValueError):
                return None
            if (not item.get("clip_uid") or not item.get("parent_video_uid")
                    or not str(item.get("s3_path") or "").startswith("s3://")
                    or duration < 60):
                return None
            valid.append(dict(item))
        result[name] = tuple(valid)
    return result


@lru_cache(maxsize=1)
def _rank_cache_stamp() -> tuple[tuple[str, int, str], ...]:
    """Assinatura portátil dos arquivos que alimentam o ranking."""
    files = (
        config.MEDIA_DATA_DIR / "ego4d" / "ego4d.json",
        config.MEDIA_DATA_DIR / "ego4d" / "clip_narrations.json",
        config.MEDIA_DATA_DIR / "ego4d" / "timed_narrations.jsonl",
        Path(ego4d.__file__),
        Path(task_matching.__file__),
        _rank_seed_path(),
    )
    stamp: list[tuple[str, int, str]] = []
    for path in files:
        # Caminhos absolutos e mtimes mudam ao extrair em outro computador.
        try:
            relative = path.resolve().relative_to(config.ROOT.resolve()).as_posix()
        except ValueError:
            # Testes, instalações portáteis e DATA_DIR externo podem ficar fora
            # do checkout; o nome lógico ainda produz uma assinatura estável.
            relative = path.name
        if not path.exists():
            stamp.append((relative, 0, "missing"))
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        stamp.append((relative, path.stat().st_size, digest.hexdigest()))
    return tuple(stamp)


def _merge_rank_seed(
    buckets: dict[str, tuple[dict[str, Any], ...]],
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Completa qualquer cache local com o índice portátil do executável.

    Versões antigas podiam gravar um cache válido, porém vazio, antes de o
    catálogo Ego4D terminar de ser preparado. Como o cache persistia entre
    atualizações, uma instalação nova continuava mostrando todas as categorias
    desabilitadas mesmo depois de receber o índice portátil. O índice embutido
    agora é sempre a base mínima; dados locais completos apenas o enriquecem.
    """
    seed = _load_rank_seed() or {}
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for name in buckets.keys() | seed.keys():
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in (*buckets.get(name, ()), *seed.get(name, ())):
            if (str(item.get("parent_video_uid") or "")
                    in ego4d.KNOWN_INCOMPLETE_IMU_VIDEO_UIDS):
                continue
            try:
                duration = float(item.get("dur_s") or 0)
            except (TypeError, ValueError):
                continue
            if not min_dur_s <= duration <= max_dur_s:
                continue
            identity = str(item.get("clip_uid") or item.get("s3_path") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            merged.append(dict(item))
        result[name] = tuple(merged)
    return result


def _load_rank_cache(
    path: Path | None = None,
) -> dict[str, tuple[dict[str, Any], ...]] | None:
    path = path or _rank_cache_path()
    if not path.exists():
        return None
    try:
        import pickle
        stamp, buckets = pickle.loads(path.read_bytes())
    except Exception:  # noqa: BLE001 — cache corrompido = recompute
        return None
    if stamp != _rank_cache_stamp() or not isinstance(buckets, dict):
        return None
    return {name: tuple(items) for name, items in buckets.items()}


def _save_rank_cache(
    buckets: dict[str, tuple[dict[str, Any], ...]],
    path: Path | None = None,
) -> None:
    try:
        import pickle
        payload = pickle.dumps(
            (_rank_cache_stamp(),
             {name: list(items) for name, items in buckets.items()}),
            protocol=4,
        )
        path = path or _rank_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
    except OSError:
        pass


@lru_cache(maxsize=1)
def _ranked_pools_cached() -> dict[str, tuple[dict[str, Any], ...]]:
    cached = _load_rank_cache()
    if cached is not None:
        return _merge_rank_seed(cached)
    if ego4d.has_timed_narrations():
        buckets = ego4d.rank_all_task_spans()
    else:
        buckets = task_matching.rank_all_tasks(_task_candidates())
    result = _merge_rank_seed(
        {name: tuple(items) for name, items in buckets.items()})
    _save_rank_cache(result)
    return result


def _ranked_pools() -> dict[str, tuple[dict[str, Any], ...]]:
    """Todas as tasks de uma vez. Cache em disco para o GET /api/tasks não congelar a UI."""
    with _RANK_LOCK:
        return _ranked_pools_cached()


@lru_cache(maxsize=8)
def _duration_ranked_pools(min_dur_s: float, max_dur_s: float):
    """Trechos Ego4D para o teto usado pelo motor, com cache entre reinícios."""
    cache_key = hashlib.sha256(
        f"{float(min_dur_s):.6f}|{float(max_dur_s):.6f}".encode("ascii")
    ).hexdigest()[:16]
    path = config.DATA_DIR / f"task_rank_cache_{cache_key}.pkl"
    cached = _load_rank_cache(path)
    if cached is not None:
        return _merge_rank_seed(
            cached, min_dur_s=min_dur_s, max_dur_s=max_dur_s)
    buckets = ego4d.rank_all_task_spans(
        min_dur_s=min_dur_s, max_dur_s=max_dur_s)
    result = _merge_rank_seed(
        {name: tuple(items) for name, items in buckets.items()},
        min_dur_s=min_dur_s,
        max_dur_s=max_dur_s,
    )
    _save_rank_cache(result, path)
    return result


def _compatible_task_clips(
    task_name: str,
    dataset_provider: str = "all",
) -> tuple[dict[str, Any], ...]:
    """Combina fontes compatíveis sem reinterpretar categorias.

    HoloAssist só participa das duas tarefas de móveis explicitamente
    mapeadas; ausência do índice local não afeta o catálogo Ego4D.
    """
    provider = normalize_dataset_provider(dataset_provider)
    ego_clips: tuple[dict[str, Any], ...] = ()
    if provider in ("all", "ego4d"):
        pools = _ranked_pools()
        if task_name in pools:
            ego_clips = pools[task_name]
        else:
            ego_clips = tuple(task_matching.ranked_clips(task_name, _task_candidates()))
    if provider == "ego4d":
        return ego_clips
    holo_clips: tuple[dict[str, Any], ...]
    try:
        holo_clips = tuple(holoassist.list_clips(task_name))
    except FileNotFoundError:
        holo_clips = ()
    return (*holo_clips, *ego_clips)


def warm_task_catalog() -> None:
    """Preenche o cache de clipes rankeados. Sem isso o 1º GET /api/tasks leva ~1 min."""
    _ranked_pools()


def available_tasks(email: str, org_key: str, *, min_dur_s: float = 60,
                    max_dur_s: float = 1800,
                    include_unavailable: bool = False,
                    dataset_provider: str = "all",
                    session: Session | None = None) -> list[dict[str, Any]]:
    """Devolve as tasks do Minute que têm mídia elegível com IMU real.

    Cada item traz clip_count e dur_range_s já calculados para a faixa pedida.
    Com include_unavailable, tarefas compatíveis apenas fora da faixa pedida
    também são devolvidas para a UI explicar por que estão desabilitadas.
    `session` reusa a Session já autenticada (o GET /api/tasks não pode
    refreshar o Firebase duas vezes na mesma conta).
    """
    sess = session
    if sess is None:
        sess = Session.from_email(email)
        sess.ensure_auth(org_key=org_key)
    elif not getattr(sess, "_live", False):
        sess.ensure_auth(org_key=org_key)
    tasks = sess.all_tasks(org_key)
    duration_pools = None
    if (normalize_dataset_provider(dataset_provider) in ("all", "ego4d")
            and (min_dur_s, max_dur_s) != (60, 1800)
            and ego4d.has_timed_narrations()):
        # Os trechos de 30 min do cache geral não representam os cortes menores
        # que a seleção real pode gerar. Recalcule todas as tarefas em uma passada.
        with _RANK_LOCK:
            duration_pools = _duration_ranked_pools(min_dur_s, max_dur_s)
    out = []
    for t in tasks:
        name = (t.get("name") or "").strip()
        rule = task_matching.rule_for(name)
        if not rule:
            continue
        all_clips = [c for c in _compatible_task_clips(name, dataset_provider)
                     if 60 <= c["dur_s"] <= 1800]
        clips = [c for c in all_clips if min_dur_s <= c["dur_s"] <= max_dur_s]
        if duration_pools is not None:
            clips = [c for c in clips if c.get("source") == "holoassist"] + list(
                duration_pools.get(name, ()))
        if clips or include_unavailable:
            source_counts: dict[str, int] = {}
            for clip in clips:
                source = str(clip.get("source") or "ego4d")
                source_counts[source] = source_counts.get(source, 0) + 1
            categories = t.get("categories") or []
            category = categories[0] if categories else {}
            category_slug = str(category.get("slug") or "other")
            out.append({
                "id": t.get("id"), "name": name,
                "description": str(t.get("description") or ""),
                # scenario permanece por compatibilidade; a seleção nova usa rule.
                "scenario": rule.primary[0],
                "name_pt": TASK_NAME_PT.get(name, name),
                "boosted": name in BOOSTED_TASKS,
                "category_slug": category_slug,
                "category_label": CATEGORY_PT.get(
                    category_slug, str(category.get("label") or "Outras")),
                "clip_count": len(clips),
                "clip_sources": source_counts,
                "dur_range_s": ((min(c["dur_s"] for c in clips),
                                 max(c["dur_s"] for c in clips)) if clips else None),
                "overall_clip_count": len(all_clips),
                "overall_dur_range_s": ((min(c["dur_s"] for c in all_clips),
                                         max(c["dur_s"] for c in all_clips))
                                        if all_clips else None),
                "available_for_duration": bool(clips),
                "match_confidence": rule.confidence,
                "match_scenarios": list(rule.primary),
            })
    return out
