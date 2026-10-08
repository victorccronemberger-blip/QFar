"""
campaign.py — Orquestração end-to-end de datasets egocêntricos -> Minute.

Responsável por, dado um conjunto de clipes + contas + tasks:
  1. Preparar cada clipe (baixar vídeo + IMU real, normalizar, montar sidecar).
  2. Enviar para cada conta configurada (upload_session com IMU real).
  3. Gerar um relatório estruturado (JSON) com os resultados e status.

Fluxo completo (contrato wire Minute 1.29 / SM-S901E):
  Ego4D(clipe+IMU real) -> normalize_video(1440x1080 yuv420p) -> sidecar nativo
  -> upload_session(register_first, evaluate, finalize) -> relatório.

Regras de ouro (não descumprir):
  - O cenário do vídeo DEVE corresponder à task do Minute (`task` score).
  - IMU real do Ego4D (não sintética) no sidecar; grade 500 Hz no domínio
    ``android_elapsedRealtimeNanos`` (mesmo eixo de frames/timebase).
  - Gate de entrega: envelope 1.29 válido + proveniência honesta third_party.
  - Vídeos longos: `timeout_blob` alto para o PUT do blob não estourar.
"""
from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import math
import os
import random
import re
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
    nymeria,
    org_policy,
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
from .content_selection import diverse_order, diversity_summary, parent_key
from .minute_api import AuthError, Session, validate_task_catalog
from .media_lifecycle import cleanup_operation
from .campaign_state import campaign_state_operation
from .campaign_history import is_campaign_history_name
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
from .upload import UploadError, pump_pending, upload_session, list_sidecars, save_sidecar
from .upload_types import journal_delivery_confirmed, is_pending_evaluation

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
    "prepare_nymeria_clip",
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
# Serializa apenas a aquisição de reservas. Sem isso, dois prepares podem
# segurar metade dos slots cada um e esperar eternamente pela outra metade.
# A execução e a liberação permanecem concorrentes.
_ACCOUNT_ENCODE_RESERVATION_LOCK = threading.Lock()
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
    # Só a resposta explícita conhecida confirma desativação; palavras soltas não.
    return bool(re.search(r"\(403\):\s*user account is disabled\.(?:\s|$)", text))
# Janela de duração aceita (recording-config: min 60s / max 1800s).
MIN_DUR_MS, MAX_DUR_MS = 60000, 1800000
# Cada vídeo selecionado deve permanecer um único envio sempre que couber no
# limite devolvido pelo recording-config. Dividir nominalmente a cada 8 min
# fazia uma gravação humana de 15 min aparecer no parceiro como dois envios.
# O sufixo `{session}_{i}` continua existindo apenas para gravações que
# realmente excedam o teto remoto.
DATASET_PROVIDERS = frozenset({"all", "ambos", "ego4d", "holoassist", "nymeria"})
CONTENT_MODES = frozenset({"both", "cache", "dataset"})


# --- meios (helpers) ----------------------------------------------------------

def normalize_dataset_provider(value: str | None) -> str:
    provider = str(value or "all").strip().lower()
    if provider not in DATASET_PROVIDERS:
        raise ValueError("dataset inválido (all|ambos|ego4d|holoassist|nymeria)")
    return provider


def normalize_content_mode(value: str | None) -> str:
    mode = str(value or "both").strip().lower()
    if mode not in CONTENT_MODES:
        raise ValueError("modo de conteúdo inválido (both|cache|dataset)")
    return mode

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
    """Confirma o encoder e, para NVENC, testa o hardware uma vez por processo."""
    import subprocess
    try:
        res = _ffmpeg_run([ff, "-hide_banner", "-encoders"], timeout=20)
    except (OSError, TimeoutError, subprocess.TimeoutExpired):
        return False
    output = f"{getattr(res, 'stdout', '')}\n{getattr(res, 'stderr', '')}"
    if res.returncode != 0 or encoder not in output:
        return False
    if encoder == "h264_nvenc":
        # Builds Windows anunciam NVENC mesmo em PCs sem placa NVIDIA/driver.
        # Valide um frame com a configuração real antes de preparar mídia longa.
        try:
            probe = _ffmpeg_run([
                *_ffmpeg_head(ff), "-f", "lavfi", "-i",
                "color=c=black:s=1440x1080:r=30", "-frames:v", "1", "-an",
                *_native_video_codec_args(nvenc=True), "-f", "null", "-",
            ], timeout=20)
        except (OSError, TimeoutError, subprocess.TimeoutExpired):
            return False
        return probe.returncode == 0
    return True


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
            "-spatial-aq", "1", "-temporal-aq", "1",
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


def _measured_pts_container_args(tmp: Path) -> list[str]:
    """Keep the existing container envelope without converting measured VFR."""
    args = _native_container_args(tmp)
    rate = args.index("-r")
    del args[rate:rate + 2]
    return [*args[:-1], "-fps_mode", "passthrough", "-enc_time_base", "1:1000000",
            "-video_track_timescale", "1000000", args[-1]]


def _measured_video_pts(path: Path) -> list[int]:
    from .sidecar import _extract_frame_pts
    frames = _extract_frame_pts(path)
    if not frames or any(b[0] <= a[0] for a, b in zip(frames, frames[1:])):
        raise UploadError("Vídeo sem PTS medidos válidos.", transient=False, phase="prepare")
    return [frame[0] - frames[0][0] for frame in frames]


def _require_same_video_pts(source: Path, output: Path, *, start_ns: int = 0,
                            end_ns: int | None = None) -> int:
    original = [ts for ts in _measured_video_pts(source)
                if ts >= start_ns and (end_ns is None or ts < end_ns)]
    measured = _measured_video_pts(output)
    if (not original or len(original) != len(measured)
            or any(abs((ts - original[0]) - actual) > 1000
                   for ts, actual in zip(original, measured))):
        raise UploadError("PTS alterados durante o recorte ou encode Nymeria.",
                          transient=False, phase="prepare")
    return original[0]


def _measured_packet_tail_args(source: Path, *, start_ns: int = 0,
                               end_ns: int | None = None) -> list[str]:
    pts = [ts for ts in _measured_video_pts(source)
           if ts >= start_ns and (end_ns is None or ts < end_ns)]
    source_end = int(probe_video(source).get("duration_ms") or 0) * 1_000_000
    end = source_end if end_ns is None else min(source_end, end_ns)
    if not pts or end <= pts[-1]:
        raise UploadError("Duração medida do último frame Nymeria inválida.",
                          transient=False, phase="prepare")
    tail_us = round((end - pts[-1]) / 1000)
    return ["-bsf:v", f"setts=duration=if(eq(N\\,{len(pts) - 1})\\,{tail_us}\\,DURATION)"]


@contextmanager
def _cpu_slots(n: int) -> Iterator[None]:
    """Reserva `n` slots do semáforo de encode (normalize pega todos)."""
    n = max(1, min(int(n), ACCOUNT_ENCODE_WORKERS))
    acquired = 0
    try:
        with _ACCOUNT_ENCODE_RESERVATION_LOCK:
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
    """Prepare a bound cache without destroying the prior pair on failure.

    Source windows and encoder parameters are unchanged. Publication is
    serialized for cooperating threads; two-file crash atomicity and external
    writers are not guaranteed. Legacy v4 caches migrate only after success.
    """
    ff = _ffmpeg_bin()
    out = out_dir / f"{stem or src.stem}_native.mp4"
    marker = out.with_name(out.name + ".source.json")
    with _native_cache_guard(out):
        try:
            cache_key = _native_cache_key(src, start_s, dur_s)
        except OSError as exc:
            raise RuntimeError(f"fonte Ego4D inacessível: {src}: {exc}") from exc
        if out.exists():
            try:
                saved = json.loads(marker.read_text(encoding="utf-8"))
                if (_native_cache_marker_matches(saved, src, out, start_s, dur_s)
                        and _native_cache_duration_ok(probe_video(out), dur_s)
                        and _native_cache_marker_matches(saved, src, out, start_s, dur_s)):
                    _record_generated_media(out, root=out_dir, role="prepared_video")
                    return out
            except (OSError, ValueError, TypeError, RuntimeError):
                pass
        tmp = marker_tmp = None
        backups: dict[Path, Path | None] = {}
        published = False
        restoration_failed = False
        try:
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
            tmp = _native_cache_temp(out, ".tmp.mp4")
            input_args = cmd
            common_args = _native_container_args(tmp)
            use_nvenc = _use_nvenc(ff)
            cmd = [*input_args, *_native_video_codec_args(nvenc=use_nvenc), *common_args]
            slots = min(2, ACCOUNT_ENCODE_WORKERS) if use_nvenc else ACCOUNT_ENCODE_WORKERS
            with _cpu_slots(slots):
                res = _ffmpeg_run(cmd)
            if res.returncode != 0 and use_nvenc:
                tmp.unlink(missing_ok=True)
                cmd = [*input_args, *_native_video_codec_args(nvenc=False), *common_args]
                with _cpu_slots(ACCOUNT_ENCODE_WORKERS):
                    res = _ffmpeg_run(cmd)
            if res.returncode != 0:
                raise RuntimeError(f"falha ao normalizar vídeo: {res.stderr.strip()[:400]}")
            if not _native_cache_duration_ok(probe_video(tmp), dur_s):
                raise RuntimeError("normalize gerou vídeo com duração inválida")
            prepared_size, prepared_hash = _native_cache_fingerprint(tmp)
            if _native_cache_key(src, start_s, dur_s) != cache_key:
                raise RuntimeError("fonte alterada durante normalização")
            saved = {**cache_key, "prepared_size": prepared_size,
                     "prepared_sha256": prepared_hash}
            serialized = json.dumps(saved, sort_keys=True, separators=(",", ":"))
            marker_tmp = _native_cache_temp(marker, ".tmp")
            marker_tmp.write_text(serialized, encoding="utf-8")
            if json.loads(marker_tmp.read_text(encoding="utf-8")) != saved:
                raise RuntimeError("marcador de cache não corresponde ao candidato")
            if (_native_cache_key(src, start_s, dur_s) != cache_key
                    or _native_cache_fingerprint(tmp) != (prepared_size, prepared_hash)):
                raise RuntimeError("fonte ou candidato alterado antes de publicação")
            # Hardlinks preserve exact old bytes without copying a large MP4.
            # Failure creating either backup happens before any publication.
            for path in (out, marker):
                backups[path] = _native_cache_backup(path)
            if _native_cache_key(src, start_s, dur_s) != cache_key:
                raise RuntimeError("fonte alterada antes de publicação")
            tmp.replace(out)
            published = True
            marker_tmp.replace(marker)
            _record_generated_media(out, root=out_dir, role="prepared_video")
            return out
        except BaseException as exc:
            if published:
                try:
                    for path in (out, marker):
                        backup = backups[path]
                        if backup is None:
                            path.unlink(missing_ok=True)
                        elif (not path.exists()
                              or _native_cache_fingerprint(path, allow_empty=True)
                              != _native_cache_fingerprint(backup, allow_empty=True)):
                            os.replace(backup, path)
                except (OSError, RuntimeError):
                    restoration_failed = True
                    exc.add_note("cache rollback incomplete; rollback files retained")
            raise
        finally:
            for path in (tmp, marker_tmp):
                if path is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
            if not restoration_failed:
                for backup in backups.values():
                    if backup is not None:
                        try:
                            backup.unlink(missing_ok=True)
                        except OSError:
                            pass

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
    clip, video = ego4d.find_clip(str(clip_info.get("clip_uid") or ""))
    if clip is not None and clip_info.get("selection_evidence") is not None:
        clip = dict(clip)
        clip["selection_evidence"] = clip_info["selection_evidence"]
    return clip, video


# v4 stat-only markers cannot prove source/output bytes. They are not reused;
# failed migration preserves the old pair, successful preparation publishes v5.
_NATIVE_CACHE_VERSION = 5
_NATIVE_CACHE_LOCKS: dict[str, Any] = {}
_NATIVE_CACHE_LOCKS_GUARD = threading.Lock()


@contextmanager
def _native_cache_guard(native: Path) -> Iterator[None]:
    """Serialize cooperating threads, including reclaim read/match/delete.

    This is a process-local guard, not a cross-process lock or a crash journal.
    """
    key = os.path.normcase(str(Path(native).resolve()))
    with _NATIVE_CACHE_LOCKS_GUARD:
        lock = _NATIVE_CACHE_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def _native_cache_fingerprint(path: Path, *, allow_empty: bool = False) -> tuple[int, str]:
    """Hash actual bytes, rejecting observed replacement/change while reading."""
    def identity(info: Any) -> tuple[int, int, int, int]:
        # Windows fstat/stat ctime semantics differ; compare file ID and mtime.
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        before = os.fstat(handle.fileno())
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
        after = os.fstat(handle.fileno())
    if ((size == 0 and not allow_empty) or size != before.st_size or identity(before) != identity(after)
            or identity(after) != identity(Path(path).stat())):
        raise RuntimeError("arquivo de cache vazio ou alterado durante leitura")
    return size, digest.hexdigest()


def _record_generated_media(path: Path, *, root: Path, role: str) -> None:
    """Bind a successfully produced derivative to its actual bytes."""
    from .media_lifecycle import record_managed_media
    _size, digest = _native_cache_fingerprint(path)
    provider = ("nymeria" if path.name.startswith("nymeria_") else
                "holoassist" if path.name.startswith("holoassist_") else "ego4d")
    record_managed_media(path, root=root, provider=provider, role=role,
                         expected_digest=digest)


def _native_cache_key(
    src: Path, start_s: float | None, dur_s: float | None,
) -> dict[str, Any]:
    """Same source bytes and unchanged source-window/encoder identity."""
    source_stat = src.stat()
    size, digest = _native_cache_fingerprint(src)
    if source_stat.st_size != size or source_stat.st_mtime_ns != src.stat().st_mtime_ns:
        raise RuntimeError("fonte alterada durante identificação")
    return {
        "version": _NATIVE_CACHE_VERSION,
        "source_size": size,
        "source_mtime_ns": source_stat.st_mtime_ns,
        "source_sha256": digest,
        "start_s": None if start_s is None else round(float(start_s), 6),
        "dur_s": None if dur_s is None else round(float(dur_s), 6),
        "width": 1440,
        "height": 1080,
        "fps": 30,
    }


def _native_cache_marker(
    src: Path, native: Path, start_s: float | None, dur_s: float | None,
) -> dict[str, Any]:
    """Describe current bytes explicitly; does not establish media provenance."""
    with _native_cache_guard(native):
        key = _native_cache_key(src, start_s, dur_s)
        size, digest = _native_cache_fingerprint(native)
        if _native_cache_key(src, start_s, dur_s) != key:
            raise RuntimeError("fonte alterada durante validação de cache")
        return {**key, "prepared_size": size, "prepared_sha256": digest}


def _native_cache_marker_matches(
    saved: Any, src: Path, native: Path,
    start_s: float | None, dur_s: float | None,
) -> bool:
    """Fail closed on v4, malformed markers or changed source/output bytes."""
    if (type(saved) is not dict or type(saved.get("version")) is not int
            or saved["version"] != _NATIVE_CACHE_VERSION):
        return False
    try:
        expected = _native_cache_marker(src, native, start_s, dur_s)
        return (type(saved) is dict and saved == expected
                and all(type(saved[key]) is type(value) for key, value in expected.items()))
    except (OSError, ValueError, TypeError, RuntimeError, KeyError):
        return False


def _native_cache_duration_ok(probe: dict[str, Any], dur_s: float | None) -> bool:
    value = probe.get("duration_ms")
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        return False
    return dur_s is None or abs(value - round(float(dur_s) * 1000)) <= 1000


def _native_cache_temp(path: Path, suffix: str) -> Path:
    candidate = path.with_name(path.name + "." + uuid.uuid4().hex + suffix)
    # Exclusive creation: never delete a temporary path belonging to a peer.
    with candidate.open("xb"):
        pass
    return candidate


def _native_cache_backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(path.name + "." + uuid.uuid4().hex + ".rollback")
    os.link(path, backup)
    return backup

def _ego_prepare_plan(clip: dict[str, Any]) -> dict[str, Any]:
    """Janela e caminhos que `prepare_clip` grava em disco.

    O acelerador usa este plano para reconhecer um cache já pronto sem
    reencode. Qualquer mudança de corte tem de passar por aqui.
    """
    clip_uid = clip["exported_clip_uid"]
    orig_start, orig_end = ego4d.clip_window_s(clip)
    # Preserve the selected source interval; arbitrary trimming changes the
    # advertised duration and the sensor alignment without source evidence.
    start_s, end_s = orig_start, orig_end
    parent_uid = str(clip.get("parent_video_uid") or clip_uid)
    needs_cut = bool(clip.get("needs_cut"))
    if needs_cut:
        media_uid = str(clip.get("media_uid") or parent_uid)
        media_offset = float(clip.get("media_time_offset_s") or 0.0)
        source_name = f"{media_uid}.mp4"
        norm_start = start_s - media_offset
    else:
        source_name = f"{clip_uid}.mp4"
        rel = start_s - orig_start
        norm_start = rel if rel > 0.02 else None
    return {
        "clip_uid": clip_uid,
        "parent_uid": parent_uid,
        "start_s": start_s,
        "end_s": end_s,
        "dur_s": end_s - start_s,
        "window_s": (start_s, end_s),
        "needs_cut": needs_cut,
        "source_name": source_name,
        "norm_start": norm_start,
        "imu_name": f"{parent_uid}_imu.csv",
        "native_name": f"{clip_uid}_native.mp4",
    }


def ego_clip_cache_state(
    clip_info: dict[str, Any], work_dir: Path,
) -> str:
    """`ready`, `partial`, `pending` ou `unresolved` para o reservatório Ego4D.

    `ready` exige a mesma fonte, o mesmo marcador de encode e o IMU que
    `prepare_clip` reaproveita. Não sonda o MP4: a campanha ainda valida a
    duração quando for usar o arquivo.
    """
    try:
        row, video = _ego_clip_inputs(clip_info)
        if row is None or video is None:
            return "unresolved"
        plan = _ego_prepare_plan(row)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return "unresolved"
    work = Path(work_dir)
    source = work / plan["source_name"]
    native = work / plan["native_name"]
    imu = work / plan["imu_name"]
    try:
        source_ok = source.is_file() and source.stat().st_size > 1024 * 1024
        imu_ok = ego4d._valid_imu_cache(imu)
        native_ok = False
        if source_ok and native.is_file() and native.stat().st_size > 1024 * 1024:
            marker = native.with_name(native.name + ".source.json")
            saved = json.loads(marker.read_text(encoding="utf-8"))
            native_ok = _native_cache_marker_matches(
                saved, source, native, plan["norm_start"], plan["dur_s"])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        source_ok = imu_ok = native_ok = False
    try:
        touched = source.exists() or native.exists() or imu.exists()
    except OSError:
        touched = False
    if source_ok and imu_ok and native_ok:
        return "ready"
    if touched:
        return "partial"
    return "pending"


def _try_prepare_imu_carve(
    clip_info: dict[str, Any],
    work_dir: Path,
    *,
    min_dur_s: float,
    max_dur_s: float,
    allow_download: bool,
    progress: Callable[[str, dict[str, Any]], None] | None,
    log: Callable[[str], None],
    emit: Callable[..., None],
    display_name: str,
) -> dict[str, Any] | None:
    """On IMU-gap prepare failure, retry the longest continuous subwindow."""
    from .imu_coverage import carve_continuous_window

    carved = carve_continuous_window(
        clip_info, work_dir, min_s=min_dur_s, max_s=max_dur_s)
    if carved is None:
        return None
    log(f"    [i] IMU com lacuna — tentando subjanela contínua "
        f"{carved.get('clip_uid')}")
    emit("clip_imu_carve", clip_uid=clip_info.get("clip_uid"),
         carved_uid=carved.get("clip_uid"), task_name=display_name)
    parent = carved.get("parent_video_uid")
    video = ego4d._cat().videos.get(parent) if parent else None
    if video is None:
        return None
    try:
        item = prepare_clip(
            carved, video, work_dir, progress=progress,
            allow_download=allow_download)
    except Exception:  # noqa: BLE001
        return None
    return {"item": item, "clip": carved}


def _normalize_ego_clip_for_prepare(clip: dict[str, Any]) -> dict[str, Any]:
    """Ensure list_clips / carved / ranked records all satisfy ``_ego_prepare_plan``."""
    out = dict(clip)
    uid = str(out.get("exported_clip_uid") or out.get("clip_uid") or "").strip()
    if not uid:
        raise RuntimeError("Clipe Ego4D sem clip_uid/exported_clip_uid")
    out["clip_uid"] = str(out.get("clip_uid") or uid)
    out["exported_clip_uid"] = uid
    window = out.get("window_s")
    if (out.get("parent_start_sec") is None or out.get("parent_end_sec") is None) and window:
        start, end = float(window[0]), float(window[1])
        out["parent_start_sec"] = str(start)
        out["parent_end_sec"] = str(end)
        out["window_s"] = (start, end)
        out.setdefault("dur_s", end - start)
    out.setdefault("source", "ego4d")
    out.setdefault("needs_cut", bool(out.get("needs_cut")))
    out.setdefault("media_time_offset_s", float(out.get("media_time_offset_s") or 0.0))
    return out


def prepare_clip(
    clip: dict[str, Any],
    video: dict[str, Any],
    work_dir: Path,
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
    allow_download: bool = True,
) -> dict[str, Any]:
    """Baixa clipe + IMU real, normaliza o vídeo e monta o sidecar. (sem upload)"""
    from . import content_provenance
    clip = _normalize_ego_clip_for_prepare(clip)
    selection_evidence = clip.get("selection_evidence")
    if selection_evidence is not None:
        selection_evidence = ego4d.revalidate_selection_evidence(clip)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    if not isinstance(clip.get("parent_video_uid"), str) or not clip["parent_video_uid"].strip():
        raise RuntimeError("Clipe Ego4D sem identidade do pai original")
    plan = _ego_prepare_plan(clip)
    clip_uid = plan["clip_uid"]
    window_s = plan["window_s"]
    parent_uid = plan["parent_uid"]
    if video.get("video_uid") and video["video_uid"] != parent_uid:
        raise RuntimeError("Metadados do vídeo não correspondem ao pai original do clipe Ego4D")
    dur_s = plan["dur_s"]
    dur_ms = int(round(dur_s * 1000))
    if not (MIN_DUR_MS <= dur_ms <= MAX_DUR_MS):
        raise RuntimeError(f"clipe {clip_uid} fora da janela de duração ({dur_ms}ms)")
    if not ego4d.imu_window_is_covered(video, window_s):
        raise RuntimeError(
            f"clipe {clip_uid} atravessa trecho sem cobertura contínua de IMU")

    def _progress(phase: str, **payload: Any) -> None:
        if progress:
            progress(phase, payload)

    # Valide o sensor ANTES de baixar/codificar gigabytes de vídeo. O catálogo
    # informa a cobertura geral, mas alguns aparelhos têm lacunas finas (por
    # exemplo, acelerômetro ausente por ~1 s) visíveis somente no CSV. Antes a
    # campanha gastava vários minutos no encode e só então descartava o clipe.
    _progress("imu_lookup")
    local_imu = work_dir / f"{parent_uid}_imu.csv"
    if not allow_download and not ego4d._valid_imu_cache(local_imu):
        raise RuntimeError(f"IMU local ausente ou inválida para {clip_uid}")
    imu_path = ego4d.download_imu(video, local_imu) if allow_download else local_imu
    if imu_path is None:
        raise RuntimeError(
            f"clipe {clip_uid} sem IMU real — não enviar (coerência sensor falharia)."
            f" Escolha um clipe com has_imu=true.")
    original_imu_binding = content_provenance.fingerprint(imu_path)
    _progress("imu_preflight")
    ego4d.build_imu_csv(
        imu_path, window_s, duration_ms=dur_ms, validate_only=True)
    _progress("imu_ready")

    _progress("video_lookup")
    source_video_path = work_dir / plan["source_name"]
    if not allow_download and not ego4d._valid_mp4_cache(source_video_path):
        raise RuntimeError(f"vídeo local ausente ou inválido para {clip_uid}")
    if plan["needs_cut"]:
        # Prefere o clip oficial CRF 18 quando ele contém a janela; o IMU
        # continua no relógio absoluto do vídeo canônico.
        if allow_download and not ego4d._valid_mp4_cache(source_video_path):
            source = dict(clip)
            source["needs_cut"] = False
            ego4d.download_clip(source, source_video_path)
    elif allow_download:
        ego4d.download_clip(clip, source_video_path)
    original_video_binding = content_provenance.fingerprint(source_video_path)
    _progress("encode")
    native = _normalize_video(
        source_video_path, work_dir,
        start_s=plan["norm_start"], dur_s=dur_s, stem=clip_uid)
    _progress("video_ready", bytes=native.stat().st_size)
    probe = probe_video(native)
    if not probe.get("duration_ms"):
        raise RuntimeError(
            f"clipe {clip_uid}: native sem duração após normalize "
            f"({native})")

    # Duração do ARQUIVO (probe) — o encoder nunca entrega N.000 s.
    dur_ms = int(probe["duration_ms"])
    fps = float(probe.get("fps") or 30.0)
    tolerance_ms = max(50, math.ceil(2000 / fps)) if math.isfinite(fps) and fps > 0 else 100
    if abs(dur_ms - round(dur_s * 1000)) > tolerance_ms:
        raise RuntimeError(
            f"clipe {clip_uid}: duração codificada divergente da janela original "
            f"({dur_ms}ms versus {round(dur_s * 1000)}ms)")
    _progress("sidecar")
    imu_diag: dict[str, Any] = {}
    imu_csv = ego4d.build_imu_csv(
        imu_path, window_s, duration_ms=dur_ms, stats=imu_diag)
    # 500 Hz (EgoImu.SAMPLING_PERIOD_US=2000) — o n_samples alimenta o
    # imuDiagnostics.sampleCount e precisa bater com as linhas do CSV.
    n_samples = max(
        1, int(dur_ms / 1000 * config.ANDROID_IMU_SAMPLE_RATE_HZ) + 1)
    frames_csv = _frames_csv(dur_ms, fps=(probe.get("fps") or 30.0))
    content_provenance.verify_input(original_imu_binding)
    content_provenance.verify_input(original_video_binding)
    lineage = content_provenance.prepare_content_provenance(
        source_video_path, imu_path, native, imu_csv, frames_csv,
        clip_uid=clip_uid, parent_video_uid=parent_uid,
        media_uid=str(clip.get("media_uid") or clip_uid), window_s=window_s,
        # The raw exported clip may already begin at a nonzero canonical
        # time. Record the actual declared source→window mapping used here.
        media_offset_s=window_s[0] - float(plan["norm_start"] or 0.0),
        normalization_start_s=plan["norm_start"], selection_evidence=selection_evidence)
    n_samples = lineage["derived_diagnostics"]["output"]["sample_count"]

    return {
        **lineage,
        # Runtime-only selection carrier keeps source location for fresh
        # validation. History serialization excludes this private structure.
        "_content_candidate": dict(clip),
        "task_name_authoritative": (selection_evidence or {}).get("task", {}).get("name"),
        "clip_uid": clip_uid,
        "video_path": str(native),
        "duration_ms": dur_ms,
        "device": video.get("device"),
        "scenario": " | ".join(ego4d.scenario_values(video)),
        "imu_real": bool(imu_path),
        # Preserve source provenance for duration/sensor checks.
        "imu_csv": imu_csv, "frames_csv": frames_csv,
        "imu_path": str(imu_path),
        "window_s": list(window_s),
        "n_samples": n_samples, "probe": probe,
        # Contadores do resample → imuDiagnostics forjado no zip (Minute 1.29).
        "imu_diagnostics": dict(imu_diag),
        "source": "ego4d",
        # Local audit/history only. Never relabel third-party footage as a
        # newly captured recording or claim the output grid as the native rate.
        "source_provenance": {
            **lineage["content_provenance"],
            "dataset": "ego4d", "recording_origin": "third_party_dataset",
            "parent_video_uid": parent_uid, "clip_uid": clip_uid,
            "parent_identity_verified": video.get("video_uid") == parent_uid,
            "window_s": list(window_s), "original_device": video.get("device"),
            "prepared_duration_ms": dur_ms,
            "imu": {
                "source_file": Path(imu_path).name,
                "processing": "resampled_measured_signals",
                "output_grid_hz": config.ANDROID_IMU_SAMPLE_RATE_HZ,
                "native_rate_hz": None,
            },
        },
        # Exclusivamente interno: nunca entra no log. Só é consumido após todos
        # os uploads deste item confirmarem sucesso.
        "_cleanup_paths": [
            str(source_video_path), str(native), str(imu_path),
        ],
    }


def prepare_nymeria_clip(
    clip: dict[str, Any],
    work_dir: Path,
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
    allow_download: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Extrai RGB+IMU reais do Aria VRS e monta item Minute-ready."""
    from . import content_provenance, nymeria_vrs, nymeria_library

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    def _progress(phase: str, **payload: Any) -> None:
        if progress:
            progress(phase, payload)

    if clip.get("acquisition_required") is True:
        if not allow_download:
            raise RuntimeError("Fonte Nymeria ainda não adquirida; modo somente cache impede download.")
        nymeria.revalidate_planned_candidate(clip)
        _progress("source_acquisition")
        def _acquisition_progress(value: Any) -> None:
            payload = value if isinstance(value, dict) else {"message": str(value)}
            _progress("source_acquisition", **payload)
        nymeria_library.ensure_sequence_for_campaign(
            clip, progress=_acquisition_progress, should_stop=should_stop)
        clip = nymeria.resolve_planned_candidate(clip)
        _progress("source_measured", measured=True)

    clip_uid = str(clip.get("clip_uid") or "")
    seq_dir = Path(str(clip.get("path") or ""))
    window = clip.get("window_s")
    if not clip_uid.startswith("nymeria:") or not seq_dir.is_dir():
        raise RuntimeError("identidade Nymeria inválida")
    if not window or len(window) != 2:
        raise RuntimeError("janela Nymeria inválida")
    start_s, end_s = float(window[0]), float(window[1])
    if not (math.isfinite(start_s) and math.isfinite(end_s) and end_s > start_s):
        raise RuntimeError("janela Nymeria inválida")
    dur_s = end_s - start_s
    dur_ms = int(round(dur_s * 1000))
    if not (MIN_DUR_MS <= dur_ms <= MAX_DUR_MS):
        raise RuntimeError(f"clipe {clip_uid} fora da janela de duração ({dur_ms}ms)")

    motion_vrs = seq_dir / "recording_head" / "data" / "motion.vrs"
    data_vrs = seq_dir / "recording_head" / "data" / "data.vrs"
    if not motion_vrs.is_file() or not data_vrs.is_file():
        raise RuntimeError(f"VRS Nymeria ausente em {seq_dir}")

    # Fresh action/window/source proof is required before any heavy decode.
    clip = nymeria.revalidate_candidate(clip)
    inputs = {"source_video": content_provenance.fingerprint(data_vrs),
              "source_imu": content_provenance.fingerprint(motion_vrs)}
    _progress("imu_lookup")
    t0_ns, t1_ns = nymeria.device_window_for_sequence(
        seq_dir, start_s=start_s, end_s=end_s)
    _progress("imu_preflight")
    samples = nymeria_vrs.read_imu_samples(
        motion_vrs, t0_ns=t0_ns, t1_ns=t1_ns, label="imu-right",
        stats=(read_stats := {}))
    imu_diag: dict[str, Any] = {}
    # Validate continuity before expensive RGB extract
    _ = nymeria_vrs.build_imu_csv_from_samples(
        samples, t0_ns=t0_ns, duration_ms=int(round((t1_ns - t0_ns) / 1e6)))
    _progress("imu_ready", samples=len(samples))

    stem = "nymeria_" + clip_uid.replace(":", "_").replace(".", "_")
    native = work_dir / f"{stem}_native.mp4"
    rgb_diag: dict[str, Any] = {}
    _progress("video_lookup")
    # Encode once from measured VRS frames. The generic normalizer forces
    # CFR30, so this path retains its envelope/codec and removes that rate.
    container_args = _native_container_args(native)[:-1]
    rate_index = container_args.index("-r")
    del container_args[rate_index:rate_index + 2]
    with _cpu_slots(ACCOUNT_ENCODE_WORKERS):
        nymeria_vrs.extract_rgb_mp4(
            data_vrs, native, t0_ns=t0_ns, t1_ns=t1_ns,
            output_args=[*_native_video_codec_args(nvenc=False), *container_args],
            stats=rgb_diag)
    _record_generated_media(native, root=work_dir, role="prepared_video")
    _progress("video_ready", bytes=native.stat().st_size)
    _progress("encode_ready", encoder="CPU", measured_pts=True)
    probe = probe_video(native)
    dur_ms = int(probe.get("duration_ms") or 0)
    if not (MIN_DUR_MS <= dur_ms <= MAX_DUR_MS):
        raise RuntimeError(
            f"clipe {clip_uid} fora da janela de duração ({dur_ms}ms)")
    _progress("sidecar")
    imu_csv = nymeria_vrs.build_imu_csv_from_samples(
        samples, t0_ns=int(rgb_diag["firstCaptureTimestampNs"]),
        duration_ms=dur_ms,
        source_dropped_timestamps_ns=read_stats["droppedRowTimestampsNs"],
        stats=imu_diag)
    n_samples = int(imu_diag["sampleCount"])
    frames_csv = build_frames_csv_from_video(
        native, duration_ms=dur_ms, require_measured_pts=True)
    for binding in inputs.values():
        content_provenance.verify_input(binding)
    inputs["prepared_video"] = content_provenance.fingerprint(native)
    diagnostics = {
        "schema": 1, "kind": "local_derived_vrs_observation",
        "source_clock": "aria_DEVICE_TIME_ns", "output_clock": "local_relative_csv_ns",
        "physical_provenance_verified": False, "native_equivalence_verified": False,
        "rgb": dict(rgb_diag), "imu": dict(imu_diag),
        "output": {"sample_count": n_samples,
                   "uniform_grid_step_ns": int(round(1e9 / config.ANDROID_IMU_SAMPLE_RATE_HZ)),
                   **content_provenance.text_binding(imu_csv)},
    }
    lineage = {
        "schema": 1, "dataset": "nymeria", "recording_origin": "third_party_dataset",
        "physical_provenance_verified": False, "receiver_derived_data_support_verified": False,
        "clip_uid": clip_uid, "parent_video_uid": clip.get("seq_id"),
        "media_uid": clip.get("seq_id"), "window_s": [start_s, end_s],
        "transform": {"kind": "measured_vrs_window_and_vfr_encoding",
                      "presentation_rotation_degrees_clockwise": 90,
                      "source_device_window_ns": [t0_ns, t1_ns],
                      "first_rgb_capture_ns": rgb_diag["firstCaptureTimestampNs"],
                      "imu": "measured_device_time_linear_resampling",
                      "frames_at_prepare": "file_extracted_measured_pts"},
        "assets": {**{role: content_provenance.public_binding(row)
                       for role, row in inputs.items()},
                   "imu_csv": content_provenance.text_binding(imu_csv),
                   "prepared_frames_csv": content_provenance.text_binding(frames_csv)},
        "selection_evidence": clip.get("selection_evidence"),
        "derived_diagnostics": diagnostics,
    }
    lineage["lineage_sha256"] = content_provenance.canonical_digest(lineage)
    return {
        "clip_uid": clip_uid,
        "video_path": str(native),
        "duration_ms": dur_ms,
        "device": "Project Aria",
        "scenario": str(clip.get("scenario") or clip.get("script") or ""),
        "imu_real": True,
        "imu_csv": imu_csv,
        "frames_csv": frames_csv,
        "n_samples": n_samples,
        "imu_diagnostics": dict(imu_diag),
        "rgb_diagnostics": dict(rgb_diag),
        "selection_evidence": clip.get("selection_evidence"),
        "task_name_authoritative": clip.get("task_name_authoritative"),
        "task_id": clip.get("task_id"),
        "registry_key": clip.get("registry_key"),
        "content_provenance": lineage,
        "derived_diagnostics": diagnostics,
        "_content_inputs": inputs,
        "_content_candidate": dict(clip),
        "_nymeria_resample_inputs": {
            "motion_vrs": inputs["source_imu"], "data_vrs": inputs["source_video"],
            "source_device_window_ns": [t0_ns, t1_ns],
            "origin_ns": int(rgb_diag["firstCaptureTimestampNs"]),
            "duration_ms": dur_ms, "imu_label": "imu-right",
            "sample_rate_hz": config.ANDROID_IMU_SAMPLE_RATE_HZ,
        },
        "probe": probe,
        "source": "nymeria",
        "window_s": [start_s, end_s],
        "seq_id": clip.get("seq_id"),
        "source_provenance": {
            **lineage,
            "dataset": "nymeria",
            "recording_origin": "third_party_dataset",
            "seq_id": clip.get("seq_id"),
            "window_s": [start_s, end_s],
            "source_time_domain": "DEVICE_TIME",
            "first_rgb_capture_ns": rgb_diag["firstCaptureTimestampNs"],
            "source_device_window_ns": [t0_ns, t1_ns],
        },
        "_cleanup_paths": [str(native)],
    }


def prepare_holoassist_clip(
    clip: dict[str, Any],
    work_dir: Path,
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
    allow_download: bool = True,
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

    if pitchshift.exists():
        source_video = pitchshift
    elif compressed.exists():
        source_video = compressed
    else:
        if not allow_download:
            raise RuntimeError(f"vídeo HoloAssist local ausente para {video_name}")
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
    if not allow_download:
        sensors = {name: sensor_dir / name for name in sensor_names}
        if any(not path.is_file() or path.stat().st_size == 0
               for path in sensors.values()):
            raise RuntimeError(f"sensores HoloAssist locais ausentes para {video_name}")
    elif progress:
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
                  recorded_at: str | None = None,
                  *,
                  limits: dict[str, int] | None = None,
                  ) -> tuple[str, str, str]:
    """Gera session_id/log_id/recorded_at NOVOS (identidade única por conta).

    `recorded_at` explícito é validado na janela da política (sem reescrita).
    Sem valor pré-agendado, a gravação termina `gap` (1.5min–1.5h) antes do
    upload — nunca "agora".
    """
    session_id = str(uuid.uuid4())
    backlog = device_profile.effective_backlog_cap_ms(limits)
    if recorded_at:
        recorded_at = device_profile.normalize_recorded_at(
            recorded_at, duration_s=duration_s, adjust=False,
            backlog_cap_ms=backlog)
        return session_id, f"{session_id}_0", recorded_at
    rng = random.Random(f"{email}|{session_id}")
    gap_s = rng.uniform(90.0, 5400.0)
    start = device_profile.recording_start_epoch(
        duration_s, gap_s=gap_s, backlog_cap_ms=backlog)
    recorded_at = device_profile.format_recorded_at(start)
    return session_id, f"{session_id}_0", recorded_at


def _chunk_plan(duration_ms: int, target_ms: int | None = None,
                limits: dict[str, int] | None = None,
                ) -> list[tuple[int, int]]:
    """Janelas (start_ms, dur_ms) respeitando os limites da política da sessão.

    Sem `limits`, cai nos defaults locais. O alvo padrão é o máximo da política:
    um vídeo dentro do intervalo vira um upload; só particiona acima do teto.
    """
    limits = dict(limits) if limits is not None else config.recording_limits()
    eff_min = max(1_000, int(limits.get("min_duration_ms") or MIN_DUR_MS))
    eff_max = max(eff_min, int(limits.get("max_duration_ms") or MAX_DUR_MS))
    if target_ms is None:
        target_ms = eff_max
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


def _chunk_video_signature(src: Path, start_s: float, dur_s: float, *, preserve_pts: bool = False) -> str:
    stat = src.stat()
    value = (
        f"{'vfr1' if preserve_pts else 'v1'}|{src.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
        f"{float(start_s):.3f}|{float(dur_s):.3f}"
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _chunk_video_path(
    src: Path,
    chunk_index: int,
    start_ms: int,
    duration_ms: int,
    *, preserve_pts: bool = False,
) -> Path:
    """Nome imutável: contas paralelas compartilham o mesmo corte pronto."""
    signature = _chunk_video_signature(
        src, start_ms / 1000.0, duration_ms / 1000.0, preserve_pts=preserve_pts)
    return src.with_name(
        f"{src.stem}_ch{int(chunk_index)}_{signature[:12]}{src.suffix}")


def _chunk_video_lock(dest: Path) -> threading.Lock:
    key = os.path.normcase(str(dest.resolve(strict=False)))
    with _CHUNK_VIDEO_LOCKS_GUARD:
        return _chunk_video_locks.setdefault(key, threading.Lock())


def _cut_video_chunk(src: Path, dest: Path, start_s: float, dur_s: float,
                     *, preserve_pts: bool = False) -> Path:
    """Corta uma vez e publica atomicamente para todas as contas do lote."""
    expected = _chunk_video_signature(src, start_s, dur_s, preserve_pts=preserve_pts)
    ready = dest.with_name(dest.name + ".chunk.ok")
    with _chunk_video_lock(dest):
        try:
            if (dest.is_file() and dest.stat().st_size > 0 and ready.is_file()
                    and ready.read_text(encoding="utf-8").strip() == expected):
                if preserve_pts:
                    _require_same_video_pts(src, dest, start_ns=round(start_s * 1e9),
                                            end_ns=round((start_s + dur_s) * 1e9))
                _record_generated_media(dest, root=dest.parent, role="delivery_chunk")
                return dest
        except OSError:
            pass
        ff = _ffmpeg_bin()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.stem + ".tmp.mp4")
        if tmp.exists():
            tmp.unlink()
        if preserve_pts:
            container = _measured_pts_container_args(tmp)
            filter_index = container.index("-vf") + 1
            container[filter_index] += (
                f",trim=start={start_s:.6f}:end={start_s + dur_s:.6f},setpts=PTS-STARTPTS")
            container[-1:-1] = _measured_packet_tail_args(
                src, start_ns=round(start_s * 1e9), end_ns=round((start_s + dur_s) * 1e9))
            with _cpu_slots(1):
                result = _ffmpeg_run([
                    *_ffmpeg_head(ff), "-i", str(src),
                    *_native_video_codec_args(nvenc=False), *container])
            if result.returncode != 0:
                tmp.unlink(missing_ok=True)
                raise UploadError("Falha no recorte Nymeria com PTS medidos.",
                                  transient=False, phase="prepare")
            try:
                _require_same_video_pts(src, tmp, start_ns=round(start_s * 1e9),
                                        end_ns=round((start_s + dur_s) * 1e9))
                tmp.replace(dest)
                ready.write_text(expected, encoding="utf-8")
                _record_generated_media(dest, root=dest.parent, role="delivery_chunk")
                return dest
            finally:
                tmp.unlink(missing_ok=True)
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
        _record_generated_media(dest, root=dest.parent, role="delivery_chunk")
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
        imu_diagnostics=(item.get("imu_diagnostics")
                         if isinstance(item.get("imu_diagnostics"), dict) else None),
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


def _valid_account_video(out: Path, base: Path, *, preserve_pts: bool = False) -> bool:
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
                != _ACCOUNT_VIDEO_ENCODE_VERSION + ("-vfr1" if preserve_pts else "")):
            return False
        if preserve_pts:
            _require_same_video_pts(base, out)
        return bool(probe_video(out).get("duration_ms"))
    except Exception:  # noqa: BLE001 — probe/stat falhou: trata como miss
        return False


def _per_account_video(base: Path, profile: DeviceProfile, *, preserve_pts: bool = False) -> Path:
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
        if _valid_account_video(out, base, preserve_pts=preserve_pts):
            _record_generated_media(out, root=base.parent, role="account_video")
            return out
        with _cpu_slots(1):
            # Pode ter ficado pronto enquanto esta conta aguardava o lock/slot.
            if _valid_account_video(out, base, preserve_pts=preserve_pts):
                _record_generated_media(out, root=base.parent, role="account_video")
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
            common_args = (_measured_pts_container_args(tmp) if preserve_pts
                           else _native_container_args(tmp))
            if preserve_pts:
                common_args[-1:-1] = _measured_packet_tail_args(base)
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
                if preserve_pts:
                    _require_same_video_pts(base, tmp)
                    if abs(int(probed["duration_ms"]) - int(probe_video(base)["duration_ms"])) > 1:
                        raise UploadError("Duração alterada durante encode Nymeria.",
                                          transient=False, phase="prepare")
                tmp.replace(out)
                _account_video_ok_path(out).write_text(
                    _ACCOUNT_VIDEO_ENCODE_VERSION + ("-vfr1" if preserve_pts else ""), encoding="utf-8")
                _record_generated_media(out, root=base.parent, role="account_video")
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
        if clip_info.get("source") == "nymeria":
            self._fut = self._pool.submit(
                prepare_nymeria_clip, clip_info, self.work_dir
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
        self._pool.shutdown(wait=True, cancel_futures=True)


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


@cleanup_operation
def _enforce_account_video_cache(work_dir: Path) -> tuple[int, int]:
    """Apaga variantes `_acc*.mp4` mais antigas se o cache passar do teto.

    Não mexe no `_native.mp4` (fonte do re-encode) nem no original Ego4D.
    Arquivos recém-gerados têm mtime novo e saem por último. Journals e
    reservas pendentes protegem as variantes mesmo acima do teto do cache.
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
    from .recovery import media_cleanup_protection
    protection = media_cleanup_protection()
    files.sort()  # mais antigo primeiro
    removed = freed = 0
    for _mtime, size, path in files:
        if total <= budget:
            break
        with _lock_for_account_video(path):
            cleanup = _delete_media_files([path], allowed_roots=(work_dir,), protection=protection)
            if cleanup["files"] != 1:
                continue
            # Keep the companion while a protected variant survives. The
            # enclosing lifecycle lease also covers this second deletion.
            _delete_media_files([_account_video_ok_path(path)], allowed_roots=(work_dir,),
                                protection=protection)
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


@cleanup_operation
def _delete_media_files(
    paths: list[Path],
    *,
    allowed_roots: tuple[Path, ...],
    protection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Remove somente arquivos validados dentro dos diretórios de mídia.

    A lista é interna, mas ainda assim cada caminho é resolvido e confinado.
    Falha de limpeza nunca transforma um upload confirmado em falha.
    """
    roots = tuple(root.resolve() for root in allowed_roots)
    if protection is None:
        from .recovery import media_cleanup_protection
        protection = media_cleanup_protection()
    protected_keys = {os.path.normcase(str(Path(p).resolve())) for p in protection['paths']}
    protected_hashes = protection['sha256']
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
        if os.path.normcase(str(resolved)) in protected_keys:
            skipped += 1
            continue
        if protected_hashes and resolved.is_file():
            try:
                if _native_cache_fingerprint(resolved, allow_empty=True)[1] in protected_hashes:
                    skipped += 1
                    continue
            except (OSError,RuntimeError,ValueError):
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
    elif clip_info.get("source") == "nymeria":
        clip_uid = str(clip_info.get("clip_uid") or "")
        stem = "nymeria_" + clip_uid.replace(":", "_").replace(".", "_")
        seq_dir = Path(str(clip_info.get("path") or ""))
        paths = [work / f"{stem}_native.mp4"]
        if seq_dir.is_absolute():
            paths.extend(seq_dir / "recording_head" / "data" / name
                         for name in ("data.vrs", "motion.vrs"))
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
    """Release only application-owned bytes after the batch is confirmed."""
    from .media_lifecycle import cleanup_managed_media, managed_media_paths
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
    result = cleanup_managed_media(
        filtered,
        allowed_roots=(Path(work_dir), holoassist.data_dir() / "recordings"),
        protected_paths=protected_paths or set(),
    )
    # A source/encode marker describes a present MP4. Remove it only after
    # that specific media was released; protected and foreign bytes keep it.
    companions = [companion for raw in result["removed_paths"]
                  for companion in _media_companions(Path(raw))]
    companion_result = _delete_media_files(
        companions, allowed_roots=(Path(work_dir), holoassist.data_dir() / "recordings"))
    result["files"] += companion_result["files"]
    result["bytes"] += companion_result["bytes"]
    result["errors"].extend(companion_result["errors"])
    if item.get("source") == "nymeria" and item.get("seq_id"):
        from . import nymeria_library
        candidate = item.get("_content_candidate") or {}
        seq_dir = Path(str(candidate.get("path") or ""))
        if seq_dir.is_absolute():
            released = nymeria_library.cleanup_sequence_sources(
                str(item["seq_id"]), root=seq_dir.parent,
                protected_paths=protected_paths or set())
            result["files"] += released["files"]
            result["bytes"] += released["bytes"]
            result["errors"].extend(released["errors"])
            candidates.extend(Path(row["path"]) for row in (item.get("_content_inputs") or {}).values()
                              if isinstance(row, dict) and isinstance(row.get("path"), str))
    roots = [Path(work_dir), holoassist.data_dir() / "recordings"]
    candidate = item.get("_content_candidate") or {}
    if item.get("source") == "nymeria" and isinstance(candidate.get("path"), str):
        seq_dir = Path(candidate["path"])
        if seq_dir.is_absolute():
            roots.append(seq_dir.parent)
    retained = managed_media_paths(candidates, allowed_roots=tuple(roots))
    result["retained_managed"] = len(retained)
    result["retained_bytes"] = sum(path.stat().st_size for path in retained)
    result["retained_names"] = [path.name for path in retained]
    result["protected"] = protected_count
    return result


def _cleanup_rejected_prepare(clip: dict[str, Any], work_dir: Path) -> dict[str, Any]:
    """Release an unconsumed candidate; journals still protect every binding."""
    paths = _expected_prefetch_paths(clip, work_dir)
    return _cleanup_uploaded_item({"source": clip.get("source"),
                                   "seq_id": clip.get("seq_id"),
                                   "_content_candidate": clip,
                                   "_cleanup_paths": [str(path) for path in paths]}, work_dir)


@cleanup_operation
def cleanup_media_cache(work_dir: Path | None = None, *, provider: str = "all") -> dict[str, Any]:
    """Limpa downloads/derivados, preservando catálogos e estado da campanha."""
    if provider not in {"all", "ego4d", "holoassist", "nymeria"}:
        raise ValueError("provedor inválido (ego4d|holoassist|nymeria|all)")
    work = Path(work_dir or config.MEDIA_DATA_DIR / "ego4d")
    from .recovery import media_cleanup_protection
    protection = media_cleanup_protection()  # Validate before deleting one file.
    candidates: list[Path] = []
    if work.is_dir():
        for path in work.iterdir():
            if not path.is_file() and not path.is_symlink():
                continue
            name = path.name.lower()
            belongs_to_holo = name.startswith("holoassist_")
            if provider != "all" and belongs_to_holo != (provider == "holoassist"):
                continue
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
    if provider in {"all", "holoassist"} and recordings.is_dir():
        candidates.extend(
            path for path in recordings.rglob("*")
            if path.is_file() or path.is_symlink()
        )
    # A filename/extension is not proof that a manually supplied file belongs
    # to this cache. Only a content-bound prepared-cache marker permits manual
    # cleanup; ambiguous legacy downloads/sensors remain for explicit review.
    owned = []
    for path in candidates:
        if path.suffix.lower() != '.mp4' or path.is_symlink():
            continue
        marker = path.with_name(path.name + '.source.json')
        try:
            saved = json.loads(marker.read_text(encoding='utf8'))
            if (type(saved) is not dict or type(saved.get('version')) is not int
                    or saved['version'] != _NATIVE_CACHE_VERSION
                    or not isinstance(saved.get('source_sha256'),str)
                    or not re.fullmatch('[0-9a-f]{64}',saved['source_sha256'])
                    or type(saved.get('prepared_size')) is not int
                    or saved['prepared_size'] != path.stat().st_size
                    or saved.get('prepared_sha256') != _native_cache_fingerprint(path)[1]):
                continue
        except (OSError,ValueError,RuntimeError):
            continue
        if os.path.normcase(str(path.resolve())) in {os.path.normcase(str(p)) for p in protection['paths']}:
            continue
        if saved['prepared_sha256'] in protection['sha256']:
            continue
        owned.extend([path,marker])
        # Delete only owned companions; arbitrary .part/tmp/CSV names do not
        # establish ownership or link a file to the admitted cache entry.
        ok = path.with_name(path.name + '.ok')
        if ok.is_file() and not ok.is_symlink():
            owned.append(ok)
    result = _delete_media_files(
        owned,
        allowed_roots=(work, recordings),
        protection=protection,
    )
    result['preserved_unowned'] = len(candidates)-len([p for p in owned if p.suffix.lower()=='.mp4'])
    return result


def _all_pending_uploads_succeeded(
    pending_accounts: list[AccountSpec],
    results: dict[str, dict[str, Any]],
) -> bool:
    """Só libera a mídia quando nenhuma conta enviada ficou pendente."""
    return bool(pending_accounts) and all(
        bool(results.get(account.email, {}).get("ok"))
        for account in pending_accounts
    )


def _cleanup_confirmed_account_media(
    item: dict[str, Any], account: AccountSpec, result: dict[str, Any],
    paths: list[str], work_dir: Path, *, protected_paths: set[Path],
) -> dict[str, Any] | None:
    """Release an account variant only after its durable receipt is complete."""
    if (result.get("ok") is not True or result.get("finalized") is not True
            or not isinstance(result.get("session_id"), str) or not paths):
        return None
    from . import recovery
    from .media_lifecycle import cleanup_managed_media
    groups = recovery._groups(include_reconciled=True)
    rows = next((rows for rows in groups if rows[0]["session_id"] == result["session_id"]
                 and rows[0]["account_email"] == account.email
                 and rows[0]["org_key"] == account.org_key), None)
    if (not rows or not recovery._complete_chunk_group(rows)
            or not all(journal_delivery_confirmed(row)
                       and row.get("campaign_reconciled") is True for row in rows)):
        return None
    from .campaign_evidence import publication_index, publication_registered
    if not publication_registered(rows, publication_index()):
        return None
    base = Path(item["video_path"])
    # Common native/chunk media serves the other accounts in this item. Only
    # account variants are released early; the batch releases shared media.
    candidates = [Path(path) for path in paths
                  if Path(path).parent == base.parent
                  and Path(path).name.startswith(base.stem + "_acc")]
    variant = Path(paths[0])
    if any(os.path.normcase(str(variant.resolve())) == os.path.normcase(str(consumer.resolve()))
           for consumer in protected_paths):
        # The next/current account can be cutting this same device variant
        # before it has published a journal. Keep its complete chunk family.
        return None
    guards = set(protected_paths)
    for path in candidates:
        if any(path.parent == consumer.parent
               and path.name.startswith(consumer.stem + "_ch")
               for consumer in protected_paths):
            guards.add(path)
    released = cleanup_managed_media(candidates, allowed_roots=(Path(work_dir),),
                                     protected_paths=guards)
    companions = [companion for raw in released["removed_paths"]
                  for companion in _media_companions(Path(raw))]
    extra = _delete_media_files(companions, allowed_roots=(Path(work_dir),))
    released["files"] += extra["files"]
    released["bytes"] += extra["bytes"]
    released["errors"].extend(extra["errors"])
    return released


# --- envio para uma conta -----------------------------------------------------

def _acknowledge_campaign_upload(session_id: str, rows: list[dict[str, Any]] | None = None) -> None:
    # Callers that already validated a complete group can avoid rescanning the
    # entire sidecar store. Standalone post-upload acknowledgments still take a
    # fresh validated listing.
    for source in list_sidecars() if rows is None else rows:
        row = dict(source)
        if row.get("session_id") == session_id:
            row["campaign_reconciled"] = True
            save_sidecar(row)


def _legacy_upload_context(session_id: str, email: str) -> dict[str, Any] | None:
    return _legacy_upload_contexts({(session_id, email)}).get((session_id, email))


def _legacy_upload_contexts(wanted: set[tuple[str, str]], journals: list[dict] | None = None) -> dict:
    """Read each history file once for a batch of legacy journals."""
    found = {}
    direct_matches = {}
    media_matches = {}
    journal_media = {}
    for row in journals or []:
        identity = (row.get("session_id"), row.get("account_email"))
        name = str(row.get("local_video_path") or "").replace("\\", "/").rsplit("/", 1)[-1]
        if identity in wanted and name and row.get("task_id"):
            journal_media.setdefault((name, row["task_id"], identity[1]), set()).add(identity)
    paths = {p for p in config.DATA_DIR.glob("campaign_*.json") if is_campaign_history_name(p.name)}
    # Older installations kept campaign history beside the media library.
    paths.update(p for p in config.MEDIA_DATA_DIR.glob("campaign_*.json") if is_campaign_history_name(p.name))
    for path in sorted(paths):
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(history, dict):
            continue
        for item in history.get("items") or []:
            if not isinstance(item, dict) or not isinstance(item.get("clip_uid"), str) or not item["clip_uid"]:
                continue
            for row in item.get("accounts") or []:
                if not isinstance(row, dict):
                    continue
                identity = (row.get("session_id"), row.get("email"))
                if not isinstance(identity[1], str):
                    continue
                key = item.get("registry_key") or (
                    f"minute|{item['task_id']}|{item['task_name']}"
                    if item.get("task_id") and item.get("task_name") else item.get("task_scenario"))
                if not isinstance(key, str) or not key:
                    continue
                context = {"registry_key": key, "clip_uid": item["clip_uid"],
                           "task_id": item.get("task_id"), "history_name": path.name}
                if identity in wanted:
                    direct_matches.setdefault(identity, {})[(key, item["clip_uid"])] = context
                name = str(item.get("video_path") or "").replace("\\", "/").rsplit("/", 1)[-1]
                for target in journal_media.get((name, item.get("task_id"), row.get("email")), ()):
                    media_matches.setdefault(target, {})[(key, item["clip_uid"])] = context
    # A filename alone is not proof. Require the same task and account and a
    # unique clip/registry mapping across all available histories.
    for identity, matches in direct_matches.items():
        if len(matches) == 1:
            found[identity] = next(iter(matches.values()))
    for identity, matches in media_matches.items():
        if identity not in direct_matches and len(matches) == 1:
            found[identity] = next(iter(matches.values()))
    return found


def _reconcile_uploads(rows: list[dict[str, Any]], account: AccountSpec,
                       item: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    groups: dict[str, list[dict[str, Any]]] = {}

    def owned(row):
        email = row.get("account_email")
        return (isinstance(email, str) and email.strip().casefold() == account.email.strip().casefold()
                and row.get("org_key") == account.org_key)

    for row in rows:
        sid = row.get("session_id")
        if not isinstance(sid, str) or not sid:
            if owned(row) and row.get("task_id") == task_id:
                return {"email": account.email, "ok": False, "finalized": False,
                        "error": "Registro de envio anterior com identidade inválida; preserve os arquivos para revisão."}
            continue
        # Keep all rows of a session, including malformed or foreign chunks.
        # A filtered subset must not become evidence of a complete receipt.
        groups.setdefault(sid, []).append(row)
    # Validate every owned session before any registry/ACK mutation. A later
    # corrupt session must not consume earlier valid receipts, and journals
    # for another clip must receive the same integrity check as this item.
    contexts: dict[str, Any] = {}
    lineages: dict[str, dict[str, Any]] = {}
    owned_groups = [(sid, chunks, first) for sid, chunks in groups.items()
                    if (first := next((row for row in chunks if owned(row)), None)) is not None]
    reset_checker = sent_registry.recovery_reset_checker() if owned_groups else None
    active_groups = [(sid, chunks, first) for sid, chunks, first in owned_groups
                     if not reset_checker(sid, "", "")]
    missing_contexts = {(sid, account.email) for sid, _chunks, first in active_groups
                        if not first.get("campaign_context")}
    legacy_contexts = (_legacy_upload_contexts(missing_contexts, rows)
                       if missing_contexts else {})
    for sid, chunks, first in active_groups:
        context = first.get("campaign_context") or legacy_contexts.get((sid, account.email))
        contexts[sid] = context
        lineage = context.get("content_provenance") if isinstance(context, dict) else None
        if lineage is not None:
            from .content_provenance import canonical_digest
            try:
                if (not isinstance(lineage, dict) or lineage.get("session_id") != sid
                        or lineage.get("task_id") != first.get("task_id")
                        or lineage.get("org_key") != account.org_key
                        or lineage.get("delivery_binding_sha256") != canonical_digest({
                            key: value for key, value in lineage.items()
                            if key != "delivery_binding_sha256"})):
                    raise ValueError
                lineages[sid] = json.loads(json.dumps(lineage, allow_nan=False))
            except (TypeError, ValueError, OverflowError) as exc:
                raise UploadError(
                    "Vínculo de conteúdo da retomada inválido; preserve os registros.",
                    transient=False, phase="recovery", review_required=True) from exc
    matched = None
    acknowledgments = []
    for sid, chunks, first in owned_groups:
        if sid not in contexts:
            continue
        context = contexts[sid]
        if (not isinstance(context, dict)
                or any(not isinstance(context.get(key), str) or not context[key]
                       for key in ("registry_key", "clip_uid"))):
            if first.get("task_id") == task_id:
                matched = {"email": account.email, "ok": False, "session_id": sid,
                           "error": "Envio anterior sem identificação do clipe; confira a sessão antes de reenviar."}
            continue
        same = context["clip_uid"] == item.get("clip_uid") and first.get("task_id") == task_id
        key = item.get("registry_key") if same else context["registry_key"]
        expected = first.get("expected_chunk_count", 1)
        indices = [row.get("chunk_index") for row in chunks]
        complete = (re.fullmatch(r"[A-Za-z0-9_-]{1,160}", sid) is not None
                    and type(expected) is int and expected > 0 and len(chunks) == expected
                    and all(type(index) is int and index >= 0 for index in indices)
                    and set(indices) == set(range(expected))
                    and all(owned(row) and journal_delivery_confirmed(row)
                            and type(row.get("expected_chunk_count", 1)) is int
                            and row.get("expected_chunk_count", 1) == expected
                            and row.get("task_id") == first.get("task_id")
                            and row.get("campaign_context") == first.get("campaign_context")
                            for row in chunks))
        if complete and reset_checker(
                sid, context["registry_key"], context.get("history_name", "")):
            continue
        if complete and all(row.get("campaign_reconciled") is True for row in chunks):
            continue
        if complete:
            acknowledgments.append((sid, key or context["registry_key"], context["clip_uid"], chunks))
        if same and (matched is None or not complete):
            matched = {"email": account.email, "ok": complete, "finalized": complete,
                       "org_key": account.org_key,
                       "session_id": sid, "recovered": complete,
                       "error": None if complete else "Envio anterior ainda pendente; nova sessão não criada."}
            if sid in lineages:
                # Keep the immutable planning descriptor and expose receipt
                # confirmation separately. Updating its flag would invalidate
                # its original digest or turn a plan into evidence of delivery.
                matched['content_provenance'] = lineages[sid]
                matched['content_receipt_confirmed']=complete
    # All owned sessions, including unrelated clips, have now been checked.
    # Only after that full pass may receipts update the sent index or journals.
    if acknowledgments:
        sent_registry.mark_sent_many([
            (scenario, clip_uid, account.email)
            for _sid, scenario, clip_uid, _chunks in acknowledgments])
        for sid, _scenario, _clip_uid, chunks in acknowledgments:
            _acknowledge_campaign_upload(sid, chunks)
    return matched


def _nymeria_resample_delivery(item: dict[str, Any], *, origin_ns: int | None = None,
                               duration_ms: int | None = None) -> tuple[str, dict[str, Any]]:
    """Measure one delivery interval from the original VRS sensor records."""
    from . import content_provenance, nymeria_vrs
    try:
        source = item["_nymeria_resample_inputs"]
        transform = item["content_provenance"]["transform"]
        first_rgb = int(transform["first_rgb_capture_ns"])
        if (source["motion_vrs"] != item["_content_inputs"]["source_imu"]
                or source["data_vrs"] != item["_content_inputs"]["source_video"]
                or source["source_device_window_ns"] != transform["source_device_window_ns"]
                or type(source["origin_ns"]) is not int or source["origin_ns"] != first_rgb
                or type(source["duration_ms"]) is not int or source["duration_ms"] != item["duration_ms"]
                or source["imu_label"] != "imu-right"
                or type(source["sample_rate_hz"]) is not int or source["sample_rate_hz"] != 500):
            raise ValueError(content_provenance.INTEGRITY_ERROR)
        origin = first_rgb if origin_ns is None else origin_ns
        duration = item["duration_ms"] if duration_ms is None else duration_ms
        if (type(origin) is not int or type(duration) is not int or duration <= 0
                or origin < first_rgb
                or origin + duration * 1_000_000 > first_rgb + item["duration_ms"] * 1_000_000 + 1000):
            raise ValueError(content_provenance.INTEGRITY_ERROR)
        read_stats, stats = {}, {}
        read_start, read_end = ((source["source_device_window_ns"])
                                if origin_ns is None and duration_ms is None
                                else (origin, origin + duration * 1_000_000))
        samples = nymeria_vrs.read_imu_samples(
            Path(source["motion_vrs"]["path"]), t0_ns=read_start, t1_ns=read_end,
            label=source["imu_label"], stats=read_stats)
        output = nymeria_vrs.build_imu_csv_from_samples(
            samples, t0_ns=origin, duration_ms=duration, sample_rate_hz=500,
            source_dropped_timestamps_ns=read_stats["droppedRowTimestampsNs"], stats=stats)
        content_provenance.verify_input(source["motion_vrs"])
        return output, stats
    except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
        raise UploadError("Fonte ou diagnóstico IMU Nymeria divergente; novo preparo necessário.",
                          transient=False, phase="prepare") from exc


@campaign_state_operation
def upload_to_account(item: dict[str, Any], account: AccountSpec,
                      task_id: str, timeout_blob: int,
                      evaluate: bool, finalize: bool,
                      on_progress: Callable[..., None] | None = None,
                      unique_video: bool = False,
                      session: Session | None = None,
                      session_cache: dict[str, Session] | None = None,
                      recorded_at: str | None = None,
                      recover_pending: bool = True,
                      **_legacy: Any,
                      ) -> dict[str, Any]:
    """Sobe um item JÁ PREPARADO para uma conta (o MP4 não é refeito).

    session_id é por conta (senão o Azure colide a chave). O arquivo de vídeo
    é o `_native.mp4` compartilhado, salvo `unique_video=True`.
    """
    result: dict[str, Any] = {"email": account.email, "ok": False}
    if item.get("source") in {"ego4d", "nymeria"} and item.get("imu_real") is not True:
        result.update(finalized=False, retryable=False,
                      error="Dataset sem sensores reais confirmados no preparo; envio bloqueado.")
        return result
    try:
        if item.get("source") == "nymeria":
            from . import content_provenance
            if not item.get("imu_csv") or not item.get("frames_csv"):
                raise UploadError(
                    "Nymeria sem CSV medido completo; envio bloqueado sem substituição sintética.",
                    transient=False, phase="prepare")
            try:
                result["source_provenance"] = content_provenance.revalidate_content_provenance(item)
                if (Path(item["video_path"]).resolve()
                        != Path(item["_content_inputs"]["prepared_video"]["path"]).resolve()):
                    raise ValueError(content_provenance.INTEGRITY_ERROR)
                candidate = dict(item["_content_candidate"])
                candidate["selection_evidence"] = item["content_provenance"]["selection_evidence"]
                result["selection_evidence"] = nymeria.revalidate_candidate(
                    candidate, task_name=item.get("task_name_authoritative"),
                    task_id=task_id, registry_key=item.get("registry_key"))["selection_evidence"]
                measured_csv, measured_stats = _nymeria_resample_delivery(item)
                if (measured_csv != item["imu_csv"]
                        or measured_stats != item.get("imu_diagnostics")
                        or measured_stats["sampleCount"] != item.get("n_samples")):
                    raise ValueError(content_provenance.INTEGRITY_ERROR)
            except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
                raise UploadError("Prova Nymeria ausente ou alterada; novo preparo necessário.",
                                  transient=False, phase="prepare") from exc
        if item.get("source") == "ego4d":
            from . import content_provenance
            try:
                if not item.get("imu_csv") or not item.get("frames_csv"):
                    raise ValueError("Ego4D sem CSV medido completo; envio bloqueado sem substituição sintética.")
                result["source_provenance"] = content_provenance.revalidate_content_provenance(item)
                if not isinstance(item.get("_content_candidate"), dict):
                    raise ValueError(content_provenance.INTEGRITY_ERROR)
                candidate = dict(item["_content_candidate"])
                candidate["selection_evidence"] = item["content_provenance"]["selection_evidence"]
                result["selection_evidence"] = ego4d.revalidate_selection_evidence(
                    candidate, task_name=item.get("task_name_authoritative"),
                    task_id=task_id, registry_key=item.get("registry_key"))
            except ValueError as exc:
                raise UploadError(str(exc), transient=False, phase="prepare") from exc
        if (org_policy.account_kind(account.email) == "crowtado"
                and account.org_key != config.ORG_KEY):
            raise AuthError(
                f"{account.email}: envio bloqueado; Crowtado exige a organização "
                f"do código {config.INVITE_CODE}. Inicie uma nova campanha."
            )
        sess = session
        if sess is None and session_cache is not None:
            sess = session_cache.get(account.email)
        if sess is None:
            sess = Session.from_email(account.email)
            # _live only means the Firebase token was refreshed. It does not
            # prove that Minute permits this account/organization to upload.
            sess.ensure_auth(org_key=account.org_key)
            if session_cache is not None:
                session_cache[account.email] = sess
        elif not getattr(sess, "_live", False):
            # Gate de org (quality-screen/userState + disabled) + version gate.
            sess.ensure_auth(org_key=account.org_key)
        else:
            sess.warmup()
        result["org_key"] = account.org_key
        profile = device_profile.get_profile(account.email)
        if recover_pending and not getattr(sess, "_moneymin_pending_pumped", False):
            recovered = pump_pending(
                sess, account_email=account.email, required_org_key=account.org_key,
                on_progress=on_progress,
            )
            sess._moneymin_pending_pumped = True
            if recovered:
                result["recovered_uploads"] = len(recovered)
        else:
            recovered = []
        journals = list_sidecars() if recover_pending else []
        # pump_pending returns journals that the next disk read usually also
        # contains. Remove only identical overlap between those two sources;
        # conflicting duplicates within persisted data must remain detectable.
        reconciliation_rows = [*journals, *(row for row in recovered if row not in journals)]
        reconciliation = _reconcile_uploads(reconciliation_rows, account, item, task_id) if recover_pending else None
        if reconciliation is not None:
            return reconciliation
        policy_limits = (
            sess.recording_policy.limits()
            if getattr(sess, "recording_policy", None) is not None
            else config.recording_limits())
        dur_s = item["duration_ms"] / 1000
        session_id, log_id, recorded_at = _new_identity(
            dur_s, account.email, recorded_at=recorded_at, limits=policy_limits)
        base_video = Path(item["video_path"])
        if unique_video:
            if on_progress:
                on_progress("encode", "start", 1)
            video_path = (_per_account_video(base_video, profile, preserve_pts=True)
                          if item.get("source") == "nymeria"
                          else _per_account_video(base_video, profile))
            if on_progress:
                on_progress("encode", "done", 1)
        else:
            video_path = base_video
        video_probe = item.get("probe") if not unique_video else None
        if not video_probe:
            video_probe = probe_video(video_path)
        imu_csv = item.get("imu_csv") or ""
        if unique_video and item.get("imu_path") and item.get("window_s"):
            imu_diag_live: dict[str, Any] = {}
            imu_csv = ego4d.build_imu_csv(
                item["imu_path"], tuple(item["window_s"]),
                duration_ms=item["duration_ms"],
                seed=f"{item['clip_uid']}|{account.email}",
                stats=imu_diag_live)
            item = dict(item)
            item["imu_diagnostics"] = imu_diag_live
        if not imu_csv:
            if item.get("source") in {"ego4d", "nymeria"}:
                raise UploadError(
                    "IMU real do dataset ausente; envio bloqueado sem substituição sintética.",
                    transient=False, phase="prepare")
            # Sem IMU real: gera a do APARELHO (sinal próprio por conta — nunca
            # a mesma IMU sintética para N contas).
            imu_csv = build_imu_csv(
                int(item["duration_ms"]),
                seed=f"moneymin.imu:{profile.device_id}")
        fps = video_probe.get("fps") or 30.0
        start_wall = device_profile.recorded_at_to_wall_ms(recorded_at)
        frames_offset = profile.uptime_ns_at(
            start_wall or int(time.time() * 1000))
        require_pts = item.get("source") in {"ego4d", "nymeria"}
        # frames.csv SEMPRE derivado do MP4 REAL (PTS/keyframes) + offset do
        # uptime Android do 1º frame. Nunca reusar o CSV preparado com relógio
        # em zero (desync: o metadata usa firstFrameSensorTimestampNs = uptime).
        try:
            frames_csv = build_frames_csv_from_video(
                video_path, duration_ms=int(item["duration_ms"]),
                fps=fps, gop=profile.frames_gop, offset_ns=frames_offset,
                **({"require_measured_pts": True} if require_pts else {}))
        except ValueError as exc:
            if not require_pts:
                raise
            raise UploadError(
                'Vídeo sem PTS medidos; preparo interrompido.',
                transient=False, phase='prepare') from exc
        plan = _chunk_plan(int(item["duration_ms"]), limits=policy_limits)
        chunk_imu_diagnostics: list[dict[str, Any]] | None = None
        if len(plan) > 1 and item.get("source") == "ego4d":
            # Classify the same source resampling events on each half-open
            # output interval. Resampling a chunk in isolation changes edge
            # holds/interpolation; copying the full item's counters repeats
            # events that do not belong to this ZIP.
            try:
                source_imu = item["_content_inputs"]["source_imu"]
                measured: dict[str, Any] = {}
                measured_csv = ego4d.build_imu_csv(
                    source_imu["path"], tuple(item["content_provenance"]["window_s"]),
                    duration_ms=int(item["duration_ms"]), stats=measured,
                    stats_windows_ms=[(start, start + duration) for start, duration in plan])
                content_provenance.verify_input(source_imu)
                if measured_csv != imu_csv:
                    raise ValueError(content_provenance.INTEGRITY_ERROR)
                chunk_imu_diagnostics = measured["windows"]
            except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
                raise UploadError(
                    "IMU ou diagnóstico do chunk divergente; novo preparo necessário.",
                    transient=False, phase="prepare") from exc
        chunk_paths: list[Path] = []
        chunk_zips: list[bytes] = []
        chunk_recorded: list[str] = []
        lineage_chunks: list[dict[str, Any]] = []
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
                if item.get("source") == "nymeria":
                    part = _chunk_video_path(video_path, index, start_ms, dur_ms, preserve_pts=True)
                    _cut_video_chunk(video_path, part, start_ms / 1000.0, dur_ms / 1000.0,
                                     preserve_pts=True)
                else:
                    part = _chunk_video_path(video_path, index, start_ms, dur_ms)
                    _cut_video_chunk(video_path, part, start_ms / 1000.0, dur_ms / 1000.0)
                part_imu, n_imu = _slice_imu_csv(imu_csv, start_ms, dur_ms)
                try:
                    part_frames = build_frames_csv_from_video(
                        part, duration_ms=dur_ms, fps=fps,
                        gop=profile.frames_gop,
                        offset_ns=profile.uptime_ns_at(
                            device_profile.recorded_at_to_wall_ms(rec_at)
                            or start_wall or int(time.time() * 1000)),
                        **({"require_measured_pts": True}
                           if require_pts else {}))
                except ValueError as exc:
                    if not require_pts:
                        raise
                    raise UploadError(
                        'Vídeo sem PTS medidos; preparo interrompido.',
                        transient=False, phase='prepare') from exc
            part_probe = probe_video(part) if len(plan) > 1 else video_probe
            chunk_item = dict(item)
            if len(plan) > 1 and item.get("source") == "nymeria":
                # A cut begins at its first captured RGB frame, which may be
                # later than a policy boundary. Resample this part at that
                # actual capture origin instead of shifting the whole CSV.
                from bisect import bisect_left
                source_pts = _measured_video_pts(video_path)
                first_index = bisect_left(source_pts, start_ms * 1_000_000)
                dur_ms = int(part_probe.get("duration_ms") or 0)
                minimum = max(1000, int(policy_limits.get("min_duration_ms") or MIN_DUR_MS))
                maximum = max(minimum, int(policy_limits.get("max_duration_ms") or MAX_DUR_MS))
                if not minimum <= dur_ms <= maximum:
                    raise UploadError("Duração medida do chunk Nymeria fora da política.",
                                      transient=False, phase="prepare")
                if first_index >= len(source_pts):
                    raise UploadError("Chunk Nymeria sem primeiro frame medido.", transient=False, phase="prepare")
                source_origin = item["_nymeria_resample_inputs"]["origin_ns"] + source_pts[first_index]
                part_imu, part_stats = _nymeria_resample_delivery(
                    item, origin_ns=source_origin, duration_ms=dur_ms)
                n_imu = int(part_stats["sampleCount"])
                chunk_item["imu_diagnostics"] = part_stats
                chunk_item["n_samples"] = n_imu
            if len(plan) > 1:
                chunk_item["n_samples"] = n_imu
                if chunk_imu_diagnostics is not None:
                    if chunk_imu_diagnostics[index]["sampleCount"] != n_imu:
                        raise UploadError(
                            "Diagnóstico IMU diverge das amostras do chunk; novo preparo necessário.",
                            transient=False, phase="prepare")
                    chunk_item["imu_diagnostics"] = chunk_imu_diagnostics[index]
            chunk_zips.append(_build_sidecar(
                chunk_item, session_id, log_id, rec_at, profile=profile,
                video_probe=part_probe, imu_csv=part_imu,
                frames_csv=part_frames, chunk_index=index,
                duration_ms=dur_ms))
            chunk_paths.append(part)
            chunk_recorded.append(rec_at)
            if item.get("source") in {"ego4d", "nymeria"}:
                lineage_chunks.append({"index": index, "start_ms": start_ms, "duration_ms": dur_ms,
                    "video_path": str(part), "imu_csv": part_imu, "frames_csv": part_frames,
                    "sidecar_bytes": chunk_zips[-1], "recorded_at": rec_at})
        result["session_id"] = session_id
        # The coordinator consumes this private carrier before persisting the
        # account result. It permits per-account eviction after the journals
        # and immutable receipt have been published, without guessing filenames.
        result["_delivery_media_paths"] = [str(video_path), *(str(path) for path in chunk_paths)]
        if item.get("source") in {"ego4d", "nymeria"}:
            try:
                result["source_provenance"] = content_provenance.bind_content_delivery(
                    item, session_id, task_id, account.org_key, lineage_chunks)
            except ValueError as exc:
                raise UploadError(str(exc), transient=False, phase="prepare") from exc
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
            suppress_per_chunk_catbear=True,
            on_progress=on_progress,
            campaign_context={"registry_key": item["registry_key"], "clip_uid": item["clip_uid"],
                              "task_id": task_id,
                              **({"history_name": item['_history_name']}
                                 if item.get('_history_name') else {}),
                              **({"content_provenance": result["source_provenance"]}
                                 if item.get("source") in {"ego4d", "nymeria"} else {})}
                              if item.get("registry_key") and item.get("clip_uid") else None,
            **({"expected_video_sha256": [row["video"]["sha256"]
                                           for row in result["source_provenance"]["chunks"]]}
               if item.get("source") in {"ego4d", "nymeria"} else {}),
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
        if isinstance(exc, AuthError):
            result["access_error"] = True
        result["restriction_confirmed"] = getattr(exc, "account_issue_code", None) == "restricted"
        result["retryable"] = (exc.retryable if isinstance(exc, UploadError) else
                               getattr(exc, "account_issue_code", None) in {
                                   "network", "timeout", "service", "rate_limit"})
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
            if is_campaign_history_name(p.name):
                paths.append(p)
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths


@campaign_state_operation
def run_campaign(
    config: CampaignConfig,
    log: CampaignLog | None = None,
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> CampaignLog:
    """Executa e persiste o ciclo completo, inclusive em falhas inesperadas."""
    if config.original_capture_plan is not None:
        from .original_capture import run_original_capture_campaign
        return run_original_capture_campaign(config, log, progress=progress, should_stop=should_stop)
    emails = [account.email.strip().casefold() for account in config.accounts]
    if not emails or any(not email for email in emails) or len(set(emails)) != len(emails):
        raise ValueError("Selecione contas válidas, sem duplicatas.")
    if not config.tasks:
        raise ValueError("Selecione ao menos uma categoria.")
    log = log or CampaignLog(started_at=_recorded_at_now(),
                             accounts=[a.email for a in config.accounts])
    log.start_request_id = config.start_request_id
    log.save()
    prefetch = _ClipPrefetch(Path(config.work_dir))
    try:
        return _run_campaign(config, log, progress=progress,
                             should_stop=should_stop, prefetch=prefetch)
    except Exception as exc:
        # The runner can restore confirmed progress after an observer fails.
        # Keep the original exception/type and immutable attempt; this private
        # reference is never inferred from server JSON or exposed in the API.
        try:
            exc._moneymin_campaign_log = log
        except Exception:
            # A custom exception may forbid private attributes. Recording
            # progress is best effort and must preserve the original failure.
            pass
        try:
            with log._save_lock:
                log.status = "error"
                log.issues.append({"kind": "campaign_error", "error": f"{type(exc).__name__}: {exc}"})
                log.save()
        except OSError as save_error:
            exc.add_note(f"Não foi possível salvar a falha da campanha: {save_error}")
        raise
    finally:
        prefetch.shutdown()


@ego4d.selection_boundary
def automatic_candidates(tsk: TaskSpec, config: CampaignConfig, *, catalog_only: bool = False) -> list[dict[str, Any]]:
    """Resolve the candidate pool once; a reviewed pool is reused verbatim."""
    if config.candidate_plan is not None:
        if tsk.task_id not in config.candidate_plan:
            raise ValueError("categoria ausente do plano confirmado")
        return [dict(clip) for clip in config.candidate_plan[tsk.task_id]]
    dataset_provider = normalize_dataset_provider(config.dataset_provider)
    content_mode = normalize_content_mode(config.content_mode)
    work_dir = Path(config.work_dir)
    shorts: list[dict[str, Any]] = []
    if dataset_provider in ("all", "ambos", "ego4d"):
        if tsk.task_name and task_matching.rule_for(tsk.task_name):
            # A mesma seleção usada pela tela inclui o índice
            # portátil mesmo quando há narrações locais parciais.
            shorts = list(_compatible_task_clips(
                tsk.task_name, "ego4d", min_dur_s=tsk.min_dur_s,
                max_dur_s=tsk.max_dur_s))
            # Best of v1.0.73/v2.0.2: merge accelerator-ready Ego4D so
            # already-normalized cache is not left unused by the rank pool.
            if content_mode != "dataset":
                shorts = _with_cached_expansion(
                    shorts, tsk.task_name, min_dur_s=tsk.min_dur_s,
                    max_dur_s=tsk.max_dur_s, work_dir=work_dir,
                    include_disabled=True, **({"catalog_only": True} if catalog_only else {}))
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
    if dataset_provider in ("all", "ambos", "nymeria"):
        try:
            nymeria_clips = nymeria.automatic_candidates(
                task_name=tsk.task_name, task_id=tsk.task_id,
                registry_key=tsk.registry_key,
                min_dur_s=tsk.min_dur_s, max_dur_s=tsk.max_dur_s,
                include_planned=content_mode != "cache",
                **({"catalog_only": True} if catalog_only else {}))
        except Exception:
            nymeria_clips = []
        # Nymeria windows carry current action and measured VRS evidence.
        if dataset_provider == "nymeria":
            shorts = list(nymeria_clips)
        else:
            shorts = [*nymeria_clips, *shorts]
    if content_mode == "cache":
        cached = _catalog_clip_cached_hint if catalog_only else _clip_is_cached
        shorts = [clip for clip in shorts if cached(clip, work_dir)]
    elif not catalog_only:
        from .imu_coverage import refine_candidates
        # refine_candidates is Ego4D-IMU-CSV specific; keep Nymeria intact.
        ego: list[dict[str, Any]] = []
        other: list[dict[str, Any]] = []
        for clip in shorts:
            uid = str(clip.get("clip_uid") or "")
            if clip.get("source") == "nymeria" or uid.startswith("nymeria:"):
                other.append(clip)
            else:
                ego.append(clip)
        ego = refine_candidates(ego, work_dir, tsk.min_dur_s, tsk.max_dur_s)
        shorts = [*ego, *other]
    task_name = tsk.task_name
    if catalog_only:
        return [dict(clip, requires_measured_validation=True,
                     capacity_kind="estimate_before_preparation")
                for clip in shorts if _catalog_queue_accepts(clip, task_name)]
    return [clip for clip in shorts if _prepare_queue_accepts(clip, task_name, fresh=False)]


def _candidate_sent_emails(registry_key: str, clip: dict) -> set[str]:
    return set().union(*(sent_registry.sent_emails(registry_key, uid)
                         for uid in [clip["clip_uid"], *clip.get("dedup_clip_uids", [])]))


def _candidate_reserved_emails(config: CampaignConfig, clip: dict) -> set[str]:
    return set().union(*(set(config.recovery_exclusions.get(uid, []))
                         for uid in [clip["clip_uid"], *clip.get("dedup_clip_uids", [])]))


def _batch_footage_overlaps(previous: dict, candidate: dict) -> bool:
    """Match original footage, independently of category and encoded variants."""
    if str(previous.get("source") or "ego4d") != str(candidate.get("source") or "ego4d"):
        return False
    if previous.get("source") == "nymeria":
        def device_window(row: dict) -> tuple[int, int] | None:
            values = row.get("device_window_ns") or row.get("planned_device_window_ns")
            if (row.get("source_clock_domain") == "aria_DEVICE_TIME_ns"
                    and isinstance(values, (list, tuple)) and len(values) == 2
                    and all(type(value) is int and value >= 0 for value in values)
                    and values[1] > values[0]):
                return values[0], values[1]
            return None
        old_window, new_window = device_window(previous), device_window(candidate)
        if old_window and new_window:
            if previous.get("parent_video_uid") != candidate.get("parent_video_uid"):
                return False
            overlap = min(old_window[1], new_window[1]) - max(old_window[0], new_window[0])
            return overlap * 5 >= min(old_window[1] - old_window[0],
                                     new_window[1] - new_window[0]) * 3
        if (previous.get("acquisition_required") is True
                or candidate.get("acquisition_required") is True):
            # Annotation-relative and SDK-relative clocks cannot be compared.
            old_ids = {previous.get("clip_uid"), *previous.get("dedup_clip_uids", [])} - {None, ""}
            new_ids = {candidate.get("clip_uid"), *candidate.get("dedup_clip_uids", [])} - {None, ""}
            return bool(old_ids & new_ids)
    if (previous.get("parent_video_uid") and candidate.get("parent_video_uid")
            and _clip_window(previous) is not None and _clip_window(candidate) is not None):
        # Distinct cuts can share an old catalog alias. Canonical source
        # windows distinguish them and tolerate small shared boundary padding.
        return _same_action_already_covered([previous], candidate)
    old_ids = {previous.get("clip_uid"), *previous.get("dedup_clip_uids", [])} - {None, ""}
    new_ids = {candidate.get("clip_uid"), *candidate.get("dedup_clip_uids", [])} - {None, ""}
    return bool(old_ids & new_ids)


def _run_campaign(
    config: CampaignConfig,
    log: CampaignLog | None = None,
    *,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    prefetch: _ClipPrefetch,
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
      sobrar nenhum clipe novo de um cenário (100% enviado), informa esgotamento;
      limpar o histórico exige um reset explícito.
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
    - `config.cleanup_after_upload`: baixa e prepara somente o item em uso,
      envia para suas contas e libera os arquivos gerenciados após confirmação.
      Um envio pendente impede a aquisição de outro item. Prefetch de mídia e
      retenção de cache ficam disponíveis somente quando a limpeza é desligada.
    """
    sends = {"ok": 0, "failed": 0, "skipped": 0}

    def _emit(kind: str, **payload: Any) -> None:
        if kind == "account_done":
            key = "skipped" if payload.get("skipped") else (
                "ok" if payload.get("ok") else "failed")
            sends[key] += 1
        elif kind == "campaign_stopped":
            with log._save_lock:
                log.status = "stopped"
        elif (kind in ("task_error", "task_empty", "task_exhausted", "task_shortfall", "goal_shortfall")
              or (kind == "clip_prepare_done" and not payload.get("ok"))):
            with log._save_lock:
                log.issues.append({"kind": kind, **payload})
                log.save()
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
    content_mode = normalize_content_mode(config.content_mode)

    log = log or CampaignLog(started_at=_recorded_at_now(),
                             accounts=[a.email for a in config.accounts])
    work_dir = Path(config.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    sessions: dict[str, Session] = {}
    banned: set[str] = set()
    rejected_imu: set[str] = set()
    account_seconds: dict[str, float] = {}
    batch_footage: dict[str, list[dict[str, Any]]] = {}

    def _batch_reserved_emails(candidate: dict) -> set[str]:
        return {email for email, previous in batch_footage.items()
                if any(_batch_footage_overlaps(clip, candidate) for clip in previous)}

    def _reserve_batch_footage(email: str, candidate: dict) -> dict:
        reservation = {key: candidate[key] for key in (
            "clip_uid", "source", "parent_video_uid", "window_s", "dedup_clip_uids",
            "device_window_ns", "planned_device_window_ns", "source_clock_domain",
            "acquisition_required")
            if key in candidate}
        batch_footage.setdefault(email, []).append(reservation)
        return reservation

    until_exhausted = config.run_until_exhausted
    quota_s = 0.0 if until_exhausted else max(0.0, float(config.target_hours_per_account or 0) * 3600.0)
    n_tasks = max(1, len(config.tasks))
    per_task_cap_s = (quota_s / n_tasks) * 1.3 if quota_s else 0.0
    _emit("campaign_start", accounts=[a.email for a in config.accounts],
          tasks=[t.task_name or t.scenario for t in config.tasks],
          dataset=dataset_provider, content_mode=content_mode,
          cleanup_after_upload=bool(config.cleanup_after_upload))

    # Fica True somente depois que um vídeo foi realmente enviado. O intervalo
    # é consumido uma vez antes do próximo candidato útil; falhas de prepare e
    # clipes pulados não podem criar uma segunda espera.
    delay_pending = False
    last_dur_s = 0.0  # duração do último clipe enviado (p/ delay_mode="clip")
    last_batch_started_at: float | None = None
    ordered_tasks = _maybe_shuffle(list(config.tasks))
    # First distribute across categories, then use the remaining reviewed content
    # without a per-category ceiling when another category cannot fill its share.
    schedule = [(task, per_task_cap_s) for task in ordered_tasks]
    if quota_s and len(ordered_tasks) > 1:
        schedule += [(task, quota_s) for task in ordered_tasks if not task.clip_uids]
    for tsk, per_task_cap_s in schedule:
        if quota_s and all(a.email in banned or account_seconds.get(a.email, 0) >= quota_s
                           for a in config.accounts):
            break
        if should_stop and should_stop():
            _log("  [!] campanha interrompida pelo usuário")
            _emit("campaign_stopped", reason="parada pelo usuário")
            break
        display_name = tsk.task_label or tsk.task_name or tsk.scenario
        registry_key = tsk.registry_key
        selection_limit = "até esgotar o conteúdo" if until_exhausted else f"n={tsk.count}"
        _log(f"\n=== categoria={display_name} ({selection_limit}) ===")
        _emit("task_start", scenario=tsk.scenario, task_name=display_name,
              count=tsk.count, run_until_exhausted=until_exhausted)
        automatic_selection = not tsk.clip_uids
        try:
            if tsk.clip_uids:
                # Seleção explícita continua sujeita às mesmas regras da
                # automática; config antiga/manual não pode furar o matching.
                clips = []
                ego_compatible: dict[str, dict[str, Any]] | None = None
                if (dataset_provider in ("all", "ambos", "ego4d") and tsk.task_name
                        and task_matching.rule_for(tsk.task_name)):
                    compatible_ego = list(_compatible_task_clips(
                        tsk.task_name, "ego4d", min_dur_s=tsk.min_dur_s,
                        max_dur_s=tsk.max_dur_s))
                    ego_compatible = {
                        candidate["clip_uid"]: candidate
                        for candidate in compatible_ego
                        if tsk.min_dur_s <= float(candidate.get("dur_s") or 0)
                        <= tsk.max_dur_s
                    }
                for uid in tsk.clip_uids:
                    if uid.startswith(("nymeria:", "nymeria-planned:")):
                        if dataset_provider not in {"all", "ambos", "nymeria"}:
                            _log(f"  [!] clipe {uid} pertence ao Nymeria — pulando")
                            continue
                        compatible = {
                            candidate["clip_uid"]: candidate
                            for candidate in nymeria.automatic_candidates(
                                task_name=tsk.task_name, task_id=tsk.task_id,
                                registry_key=tsk.registry_key,
                                min_dur_s=tsk.min_dur_s,
                                max_dur_s=tsk.max_dur_s,
                                include_planned=content_mode != "cache",
                            )
                        }
                        nymeria_clip = compatible.get(uid)
                        if nymeria_clip is None:
                            _log(
                                f"  [!] clipe {uid} incompatível com "
                                f"'{display_name}' — pulando"
                            )
                            continue
                        clips.append(nymeria_clip)
                        continue
                    if uid.startswith("holoassist:"):
                        if dataset_provider in {"ego4d", "nymeria", "ambos"}:
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
                    if dataset_provider == "nymeria":
                        _log(f"  [!] clipe {uid} não pertence ao Nymeria — pulando")
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
                shorts = automatic_candidates(tsk, config)
                all_clips = shorts
                fresh = []
                new_recipient_counts = {}
                used_parents = set()
                for candidate in all_clips:
                    if candidate["clip_uid"] in rejected_imu:
                        continue
                    reserved_for = (_candidate_reserved_emails(config, candidate)
                                    | _batch_reserved_emails(candidate))
                    unavailable = _candidate_sent_emails(registry_key, candidate) | reserved_for
                    if sent_registry.is_sent_to_all(registry_key, candidate["clip_uid"], emails) or all(email in unavailable for email in emails):
                        used_parents.add(parent_key(candidate))
                    else:
                        fresh.append(candidate)
                        new_recipient_counts[candidate["clip_uid"]] = sum(email not in unavailable for email in emails)
                skipped_n = len(all_clips) - len(fresh)
                if skipped_n:
                    _log(f"  (pulando {skipped_n} clipe(s) já enviados, reservados ou reprovados nos sensores)")
                if not fresh and all_clips:
                    _log(f"  [i] catálogo elegível esgotado para '{display_name}'; histórico preservado")
                    _emit("task_exhausted", scenario=tsk.scenario, task_name=display_name,
                          eligible=len(all_clips), rejected_imu=sum(c["clip_uid"] in rejected_imu for c in all_clips))
                    continue
                hours = sum(float(c.get("dur_s") or 0) for c in fresh) / 3600
                merged_n = sum(1 for c in fresh if c.get("needs_cut"))
                _log(f"  pool: {len(shorts)} trechos puros → {len(fresh)} "
                     f"sessões ({merged_n} cortes do vídeo-pai, {hours:.1f}h)")
                clips = _prefer_cached_clips(diverse_order(ego4d.prefer_long_clips(
                    fresh, shuffle=config.shuffle_schedule,
                    prefer_parent_cuts=True), used_parents=used_parents),
                    work_dir, prioritize=content_mode != "dataset")
                # Prefer content that serves more accounts, keeping cache/diversity
                # ordering within each group. Partially sent clips remain fallback.
                clips.sort(key=lambda clip: -new_recipient_counts[clip["clip_uid"]])
                _emit("content_pool", task_name=display_name, clips=len(clips),
                      **diversity_summary(clips))
        except Exception as exc:  # noqa: BLE001 — uma categoria não mata as demais
            error = f"{type(exc).__name__}: {exc}"
            _log(f"  [!] falha ao selecionar clipes: {error}")
            _emit("task_error", scenario=tsk.scenario, task_name=display_name,
                  error=error)
            continue
        if tsk.clip_uids:
            clips = _maybe_shuffle(clips)
            if content_mode == "cache":
                clips = [clip for clip in clips if _clip_is_cached(clip, work_dir)]
            if config.cleanup_after_upload:
                clips = [
                    {**clip, "_cache_ready_at_selection": True}
                    if _clip_is_cached(clip, work_dir) else clip
                    for clip in clips
                ]
        if not clips:
            _log(f"  [!] nenhum clipe encontrado p/ cenário '{tsk.scenario}'")
            _emit("task_empty", scenario=tsk.scenario, task_name=display_name,
                  min_dur_s=tsk.min_dur_s, max_dur_s=tsk.max_dur_s,
                  dataset=dataset_provider)
            continue
        task_accounts = _maybe_shuffle(list(config.accounts))
        task_sends: dict[str, int] = {}
        task_seconds: dict[str, float] = {}
        for clip_info in clips:
            if clip_info["clip_uid"] in rejected_imu:
                continue
            if len(banned) == len(config.accounts):
                break
            if quota_s and all(
                    a.email in banned or account_seconds.get(a.email, 0) >= quota_s
                    or task_seconds.get(a.email, 0) >= per_task_cap_s
                    for a in config.accounts):
                break
            if automatic_selection and not quota_s and not until_exhausted and all(
                    a.email in banned or task_sends.get(a.email, 0) >= tsk.count
                    for a in config.accounts):
                break
            if should_stop and should_stop():
                _log("  [!] campanha interrompida pelo usuário")
                _emit("campaign_stopped", reason="parada pelo usuário")
                break
            # anti-desperdício: se TODAS as contas da campanha já receberam este
            # clipe (seleção explícita do wizard ou corrida entre campanhas),
            # pula ANTES do intervalo e de baixar/reencodar.
            sent_to = _candidate_sent_emails(registry_key, clip_info)
            batch_reserved = _batch_reserved_emails(clip_info)
            reserved = _candidate_reserved_emails(config, clip_info) | batch_reserved
            eligible_accounts = [a for a in config.accounts if a.email not in banned
                                 and (not quota_s or (account_seconds.get(a.email, 0) < quota_s
                                      and task_seconds.get(a.email, 0) < per_task_cap_s))
                                 and (until_exhausted or quota_s or not automatic_selection
                                      or task_sends.get(a.email, 0) < tsk.count)]
            if not eligible_accounts or all(a.email in sent_to | reserved for a in eligible_accounts):
                _log(f"  clipe {clip_info['clip_uid'][:12]} já enviado ou reservado por pendência "
                     f"em todas as contas — pulando (sem baixar/reencodar)")
                # account_done mantém o progresso da UI consistente; o item nem
                # chega a existir (nada a registrar no log da campanha)
                for account in eligible_accounts:
                    _emit("account_done", clip_uid=clip_info["clip_uid"],
                          task=tsk.scenario, email=account.email, ok=account.email not in reserved,
                          skipped=True, reason=("source_reserved_in_campaign" if account.email in batch_reserved
                                               else "pending_recovery" if account.email in reserved else "already_sent"))
                continue
            if content_mode == "cache" and not _clip_is_cached(clip_info, work_dir):
                _emit("clip_prepare_done", clip_uid=clip_info["clip_uid"],
                      task_name=display_name, ok=False,
                      error="clipe saiu do cache antes do preparo")
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
                        if should_stop and should_stop():
                            raise RuntimeError("preparo interrompido pelo usuário")
                        _emit(
                            "clip_prepare_progress",
                            clip_uid=_clip_uid,
                            phase=phase,
                            **payload,
                        )

                if item is None and clip_info.get("source") == "holoassist":
                    item = prepare_holoassist_clip(
                        clip_info, work_dir, progress=_prepare_progress,
                        allow_download=content_mode != "cache",
                    )
                if item is None and clip_info.get("source") == "nymeria":
                    item = prepare_nymeria_clip(
                        clip_info, work_dir, progress=_prepare_progress,
                        allow_download=content_mode != "cache",
                        should_stop=should_stop,
                    )
                if item is None:
                    clip, video = _ego_clip_inputs(clip_info)
                    if clip is None or video is None:
                        raise RuntimeError("clipe ou vídeo pai ausente no manifest")
                    item = prepare_clip(
                        clip, video, work_dir, progress=_prepare_progress,
                        allow_download=content_mode != "cache")
                # A duração real pode diferir das anotações ou de um cache antigo.
                # Nunca envie um MP4 que ultrapasse o teto escolhido.
                prepared_duration = float(item["duration_ms"])
                if not math.isfinite(prepared_duration) or prepared_duration <= 0:
                    raise ValueError("vídeo preparado sem duração válida")
                # Encoding/cache validation already allows up to one second
                # of container rounding; reject genuinely shortened footage.
                if prepared_duration + 1000 < tsk.min_dur_s * 1000:
                    raise ValueError(
                        f"vídeo preparado abaixo da duração mínima de {tsk.min_dur_s:g}s"
                    )
                if prepared_duration > tsk.max_dur_s * 1000:
                    raise ValueError(
                        f"vídeo preparado excede a duração máxima de {tsk.max_dur_s:g}s"
                    )
            except Exception as exc:  # noqa: BLE001 — pula o clipe, segue a campanha
                if should_stop and should_stop():
                    _emit("campaign_stopped", reason="parada durante aquisição ou preparo")
                    break
                imu_fail = any(reason in str(exc).lower() for reason in (
                        "cobertura imu insuficiente", "sem amostras válidas de imu",
                        "sem cobertura contínua de imu", "atravessa trecho sem cobertura",
                        "sem imu real"))
                carved_item = None
                if imu_fail and not clip_info.get("imu_carved_from"):
                    def _carve_progress(
                        phase: str,
                        payload: dict[str, Any],
                        _clip_uid: str = str(clip_info["clip_uid"]),
                    ) -> None:
                        _emit("clip_prepare_progress", clip_uid=_clip_uid,
                              phase=phase, **payload)

                    carved_item = _try_prepare_imu_carve(
                        clip_info, work_dir,
                        min_dur_s=float(tsk.min_dur_s),
                        max_dur_s=float(tsk.max_dur_s),
                        allow_download=content_mode != "cache",
                        progress=_carve_progress,
                        log=_log,
                        emit=_emit,
                        display_name=display_name,
                    )
                if carved_item is None:
                    if imu_fail:
                        rejected_imu.add(clip_info["clip_uid"])
                    error = f"{type(exc).__name__}: {exc}"
                    _log(f"    [!] prepare falhou: {error}")
                    _emit("clip_prepare_done", clip_uid=clip_info["clip_uid"],
                          task_name=display_name, ok=False, error=error)
                    if config.cleanup_after_upload:
                        cleanup = _cleanup_rejected_prepare(clip_info, work_dir)
                        _emit("storage_cleanup", clip_uid=clip_info["clip_uid"],
                              files=cleanup["files"], bytes=cleanup["bytes"],
                              errors=cleanup["errors"], protected=cleanup["protected"])
                        # A rejected sensor window is safe to skip after its
                        # owned, unused files are released. Acquisition/encode
                        # failures may hold partial downloads: stop this cycle.
                        if (not imu_fail or cleanup["errors"]
                                or cleanup.get("retained_managed", 0)):
                            raise RuntimeError(
                                "Preparo interrompido; nenhum outro vídeo foi adquirido. " + error) from exc
                    continue
                item = carved_item["item"]
                clip_info = carved_item["clip"]
                prepared_duration = float(item["duration_ms"])
                if (not math.isfinite(prepared_duration) or prepared_duration <= 0
                        or prepared_duration + 1000 < tsk.min_dur_s * 1000
                        or prepared_duration > tsk.max_dur_s * 1000):
                    rejected_imu.add(clip_info["clip_uid"])
                    _log("    [!] carve fora da janela de duração")
                    _emit("clip_prepare_done", clip_uid=clip_info["clip_uid"],
                          task_name=display_name, ok=False,
                          error="carve fora da duração")
                    if config.cleanup_after_upload:
                        _cleanup_uploaded_item(item, work_dir)
                        raise RuntimeError(
                            "Recorte preparado fora da duração; nenhum outro vídeo foi adquirido.")
                    continue
            # Acquisition resolves catalog-only Nymeria identities against the
            # measured SDK clock before dedup, reservations or delivery.
            if clip_info.get("acquisition_required") is True:
                resolved = item.get("_content_candidate")
                if not isinstance(resolved, dict) or resolved.get("acquisition_required") is True:
                    raise ValueError("Fonte Nymeria adquirida sem candidato medido confirmado.")
                clip_info = dict(resolved)
            # IMU carving can produce a different proven source interval.
            # Account eligibility follows the footage that will actually go out.
            batch_reserved = _batch_reserved_emails(clip_info)
            reserved = _candidate_reserved_emails(config, clip_info) | batch_reserved
            sent_to = _candidate_sent_emails(registry_key, clip_info)
            _emit("clip_ready", clip_uid=clip_info["clip_uid"],
                  duration_ms=item["duration_ms"], imu_real=item["imu_real"],
                  source_provenance=item.get("source_provenance"))
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
            elif until_exhausted:
                should_prefetch = any(a.email not in banned for a in task_accounts)
            else:
                available_accounts = [a for a in task_accounts
                                      if a.email not in banned and a.email not in sent_to | reserved
                                      and task_sends.get(a.email, 0) < tsk.count]
                current_recipients = {a.email for a in (
                    available_accounts if config.share_clips else available_accounts[:1])}
                should_prefetch = any(
                    a.email not in banned
                    and task_sends.get(a.email, 0) + int(a.email in current_recipients) < tsk.count
                    for a in task_accounts)
            if (should_prefetch and content_mode != "cache"
                    and not config.cleanup_after_upload):
                _prefetch_following(
                    prefetch,
                    clips,
                    clip_info["clip_uid"],
                    [account.email for account in config.accounts],
                    registry_key,
                )
            item["clip_uid"] = clip_info["clip_uid"]
            item["task_id"] = tsk.task_id
            item["task_name"] = display_name
            item["task_name_authoritative"] = tsk.task_name
            item["task_scenario"] = tsk.scenario
            item["registry_key"] = registry_key
            item["dedup_clip_uids"] = list(clip_info.get("dedup_clip_uids") or [])
            item['_history_name'] = log._path.name if log._path is not None else None
            item["accounts"] = []
            pending_accounts: list[AccountSpec] = []
            account_results: dict[str, dict[str, Any]] = {}
            persisted_item: dict[str, Any] | None = None

            def _persist_item_results() -> None:
                nonlocal persisted_item
                with log._save_lock:
                    item["accounts"] = [account_results[a.email] for a in config.accounts
                                        if a.email in account_results]
                    if persisted_item is None:
                        persisted_item = {k: v for k, v in item.items()
                                          if k not in ("imu_csv", "frames_csv", "probe", "_cleanup_paths", "_history_name")}
                        log.add_item(persisted_item)
                    else:
                        persisted_item["accounts"] = item["accounts"]
                    log.save()
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
                elif automatic_selection and not until_exhausted and task_sends.get(account.email, 0) >= tsk.count:
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
                if account.email in batch_reserved:
                    account_results[account.email] = {"email": account.email, "org_key": account.org_key,
                        "ok": False, "skipped": True, "reason": "source_reserved_in_campaign"}
                    _emit("account_done", clip_uid=clip_info["clip_uid"], task=tsk.scenario,
                          email=account.email, ok=False, skipped=True, reason="source_reserved_in_campaign")
                    continue
                if account.email in reserved:
                    _log(f"      -> {account.email} (clipe reservado por envio anterior pendente)")
                    account_results[account.email] = {"email": account.email, "org_key": account.org_key,
                        "ok": False, "skipped": True, "reason": "pending_recovery"}
                    _emit("account_done", clip_uid=clip_info["clip_uid"], task=tsk.scenario,
                          email=account.email, ok=False, skipped=True, reason="pending_recovery")
                    continue
                # dedup por conta: quem já recebeu este clipe é pulado
                if account.email in _candidate_sent_emails(registry_key, clip_info):
                    _log(f"      -> {account.email} (já recebeu este clipe — pulando)")
                    skip_res = {"email": account.email, "org_key": account.org_key,
                                "ok": True, "skipped": True, "reason": "already_sent"}
                    account_results[account.email] = skip_res
                    # account_done mantém o progresso da UI consistente
                    _emit("account_done", clip_uid=clip_info["clip_uid"],
                          task=tsk.scenario, email=account.email, ok=True,
                          skipped=True, reason="already_sent")
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
            if config.unique_video and not config.cleanup_after_upload:
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
                    recover_pending=not bool(config.recovery_exclusions),
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
                    # The uploaded video already has a receipt. Retry only its
                    # unavailable evaluation/finalization, never the send. Keep
                    # one bounded recovery pass before the storage guard.
                    sid = last_result.get("session_id")
                    if (config.cleanup_after_upload and config.evaluate and config.finalize
                            and sid and account.email in sessions
                            and not (should_stop and should_stop())):
                        try:
                            owned = [row for row in list_sidecars()
                                     if row.get("session_id") == sid]
                            if owned and all(
                                    row.get("account_email") == account.email
                                    and row.get("org_key") == account.org_key
                                    and row.get("task_id") == tsk.task_id
                                    and row.get("evaluation_http_status") != 429
                                    and "(HTTP 429)" not in str(row.get("error") or "")
                                    and is_pending_evaluation(row) for row in owned):
                                _emit("account_evaluation_recovery", email=account.email)
                                def recovery_progress(phase: str, state: str, attempt: int,
                                                      **details: Any) -> None:
                                    _emit("account_progress", clip_uid=clip_info["clip_uid"],
                                          task=tsk.scenario, email=account.email,
                                          phase=phase, state=state, attempt=attempt, **details)
                                pump_pending(sessions[account.email],
                                    account_email=account.email, required_org_key=account.org_key,
                                    session_ids={sid}, on_progress=recovery_progress)
                                rows = [row for row in list_sidecars()
                                        if row.get("session_id") == sid]
                                recovered = _reconcile_uploads(rows, account, item, tsk.task_id)
                                if recovered and recovered.get("ok"):
                                    return {**last_result, **recovered, "evaluation_recovered": True}
                        except Exception as exc:
                            # Preserve the receipt and let the normal terminal
                            # guard report the unresolved delivery. No new SID.
                            _log(f"  recuperação da avaliação preservada ({type(exc).__name__})")
                    if last_result.get("restriction_confirmed") or _is_disabled_error(last_result.get("error")):
                        # Restrição confirmada não deve provocar novas tentativas.
                        return last_result
                    # Não recrie uma sessão cujo envio pode ter sido aceito.
                    # A recuperação desse estado pertence ao journal persistido.
                    if (last_result.get("retryable") is not True
                            or last_result.get("session_id") or last_result.get("uploads")):
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
                delivery_media = acc_res.pop("_delivery_media_paths", [])
                if (acc_res.get("ok") and not acc_res.get("skipped")
                        and acc_res.get("finalized") is not True):
                    acc_res = {**acc_res, "ok": False,
                               "error": acc_res.get("error") or
                               "Finalização não confirmada; envio preservado para revisão."}
                # Known pre-effect failures/skips have not consumed this
                # footage. A receipt or an uncertain remote result stays
                # reserved across the remaining tasks in this batch.
                if (acc_res.get("skipped") or (not acc_res.get("ok")
                        and "retryable" in acc_res and not acc_res.get("session_id")
                        and not acc_res.get("uploads"))):
                    reservation = batch_reservations.pop(account.email, None)
                    if reservation is not None:
                        batch_footage[account.email].remove(reservation)
                results[account.email] = acc_res
                # Persiste antes de atualizar o índice de deduplicação. Uma falha
                # no índice não deve apagar do histórico um envio concluído.
                record_error: Exception | None = None
                try:
                    _persist_item_results()
                except Exception as exc:
                    record_error = exc
                ok = acc_res.get("ok")
                if ok and not acc_res.get("skipped") and acc_res.get("finalized") is True:
                    try:
                        identities = (sent_registry.delivery_clip_uids(item)
                                      if item.get("source") == "nymeria" else [clip_uid])
                        if len(identities) > 1:
                            sent_registry.mark_sent_many([
                                (sent_key, identity, account.email) for identity in identities])
                        else:
                            sent_registry.mark_sent(sent_key, clip_uid, account.email)
                        if acc_res.get("session_id"):
                            _acknowledge_campaign_upload(acc_res["session_id"])
                    except Exception as exc:
                        record_error = record_error or exc
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
                      task=task_scenario, registry_key=sent_key, email=account.email, ok=ok,
                      finalized=acc_res.get("finalized"),
                      evaluate=ev, error=acc_res.get("error"),
                      skipped=bool(acc_res.get("skipped")),
                      session_id=acc_res.get("session_id"),
                      credited_seconds=(duration_s if ok and not acc_res.get("skipped")
                                        and acc_res.get("finalized") is True else 0.0))
                if not ok and (acc_res.get("restriction_confirmed") or _is_disabled_error(acc_res.get("error"))):
                    banned.add(account.email)
                    sessions.pop(account.email, None)
                    acc_res["excluded_from_campaign"] = True
                    _emit("account_excluded", email=account.email)
                elif (not ok and not config.require_all_accounts
                      and acc_res.get("access_error") is True and not acc_res.get("session_id")
                      and not acc_res.get("uploads")):
                    # A classified failure before any receipt may leave this
                    # campaign without archiving/banning the user's account.
                    banned.add(account.email)
                    sessions.pop(account.email, None)
                    acc_res["excluded_from_campaign"] = True
                    _emit("account_deferred", email=account.email, error=acc_res.get("error"))
                if record_error is not None:
                    raise record_error
                if config.cleanup_after_upload and config.unique_video and delivery_media:
                    # Queued accounts can share a persisted device identity.
                    # Protect their exact variant before they publish journals.
                    consumers = {Path(item["video_path"])}
                    for other in pending_accounts:
                        if other.email not in results:
                            consumers.add(_account_video_path(
                                Path(item["video_path"]), device_profile.get_profile(other.email)))
                    try:
                        released = _cleanup_confirmed_account_media(
                            item, account, acc_res, delivery_media, work_dir,
                            protected_paths=consumers)
                    except (OSError, ValueError, RuntimeError) as exc:
                        _log(f"  armazenamento: variante preservada para revisão ({type(exc).__name__})")
                    else:
                        if released and released["files"]:
                            _emit("storage_cleanup", clip_uid=clip_uid, email=account.email,
                                  files=released["files"], bytes=released["bytes"],
                                  errors=released["errors"], protected=released["skipped"])

            batch_started_at = time.monotonic() if pending_accounts else None
            batch_reservations: dict[str, dict] = {}
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
                    persistence_error: Exception | None = None
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
                        # só a parada solicitada interrompe as demais contas.
                        for future in [f for f in futures if f.done()]:
                            account = futures.pop(future)
                            try:
                                _record_account(account, future.result())
                            except Exception as exc:
                                # Pare novos envios, mas recolha resultados de
                                # workers já ativos antes de encerrar o histórico.
                                persistence_error = persistence_error or exc
                                accepting = False
                                queued.clear()
                        if should_stop and should_stop():
                            accepting = False
                        while accepting and queued and len(futures) < workers:
                            # A espera de gravação pode terminar depois que a
                            # janela fechou; não inicie PUT fora do horário.
                            if (config.active_hours
                                    and _window_remaining_s(config.active_hours) > 0):
                                break
                            if queued[0][0] > time.time():
                                break
                            if _stagger_launch(launched) or (should_stop and should_stop()):
                                accepting = False
                                break
                            _ready_at, idx, account, recorded_at = queued.pop(0)
                            batch_reservations[account.email] = _reserve_batch_footage(account.email, clip_info)
                            future = pool.submit(
                                _send_account_with_recovery, idx, account, recorded_at)
                            futures[future] = account
                            launched += 1
                        if not futures:
                            if not accepting or not queued:
                                break
                            if config.active_hours and _window_remaining_s(config.active_hours) > 0:
                                if _wait_for_window(config.active_hours, should_stop, _emit):
                                    accepting = False
                                continue
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
                    if persistence_error is not None:
                        _stop_warm()
                        raise persistence_error

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
                _persist_item_results()
            if should_stop and should_stop():
                if account_results:
                    _emit("item_done", clip_uid=clip_info["clip_uid"],
                          task=tsk.scenario, partial=True)
                _log("  [!] campanha interrompida pelo usuário")
                _emit("campaign_stopped", reason="parada pelo usuário")
                _stop_warm()
                break
            retained_accounts = [a for a in pending_accounts
                                 if not account_results.get(a.email, {}).get("excluded_from_campaign")]
            all_pending_succeeded = not retained_accounts or _all_pending_uploads_succeeded(
                retained_accounts, account_results)
            # Não espera tarefa de fundo: todas as variantes usadas por contas
            # bem-sucedidas já terminaram; o prefetch do próximo segue ativo.
            _stop_warm()
            failed_accounts = [
                result for result in account_results.values()
                if not result.get("ok") and not result.get("skipped")
                and (not result.get("excluded_from_campaign")
                     or result.get("session_id") or result.get("uploads"))
            ]
            if (config.require_all_accounts or config.cleanup_after_upload) and failed_accounts:
                details = "; ".join(
                    f"{result.get('email')}: {result.get('error') or 'erro desconhecido'}"
                    for result in failed_accounts
                )
                _emit("item_incomplete", clip_uid=clip_info["clip_uid"],
                      task=tsk.scenario,
                      accounts=[r.get("email") for r in failed_accounts])
                raise RuntimeError(
                    "lote incompleto após todas as tentativas; "
                    "a campanha preservou a mídia e não adquiriu outro vídeo. "
                    "Contas pendentes: " + details
                )
            if pending_accounts:
                if reserved and not config.cleanup_after_upload:
                    _log("  armazenamento: mídia preservada para a recuperação do envio anterior")
                elif config.cleanup_after_upload and all_pending_succeeded:
                    from . import recovery
                    recovery.reconcile_confirmed(refresh=False)
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
                        retained_managed=cleanup.get("retained_managed", 0),
                        retained_bytes=cleanup.get("retained_bytes", 0),
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
                    if cleanup["errors"] or cleanup.get("retained_managed", 0):
                        _emit("item_incomplete", clip_uid=clip_info["clip_uid"],
                              task=tsk.scenario, reason="media_retained_for_recovery",
                              retained_managed=cleanup.get("retained_managed", 0),
                              retained_bytes=cleanup.get("retained_bytes", 0))
                        raise RuntimeError(
                            "Mídia reservada para envio anterior ou limpeza incompleta; "
                            "a campanha não adquiriu outro vídeo. Resolva a pendência e retome.")
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
            elif config.cleanup_after_upload:
                cleanup = _cleanup_uploaded_item(item, work_dir)
                if cleanup["errors"] or cleanup.get("retained_managed", 0):
                    raise RuntimeError(
                        "Mídia reservada para envio anterior ou limpeza incompleta; "
                        "a campanha não adquiriu outro vídeo. Resolva a pendência e retome.")
            # (log: sem os blobs/csv brutos — grandes; identity fica por conta)

        if log.status != "stopped" and not quota_s and not until_exhausted and automatic_selection:
            remaining = {a.email: tsk.count - task_sends.get(a.email, 0)
                         for a in config.accounts if task_sends.get(a.email, 0) < tsk.count}
            if remaining:
                _emit("task_shortfall", task_name=display_name, task_id=tsk.task_id,
                      requested_per_account=tsk.count, remaining_sends=remaining)
        if len(banned) == len(config.accounts):
            break
        if quota_s and all(account_seconds.get(a.email, 0) >= quota_s
                           for a in config.accounts):
            _log(f"  [i] meta de {quota_s / 3600:.1f}h por conta atingida")
            break

    if log.status != "stopped":
        if quota_s:
            remaining = {a.email: max(0.0, quota_s - account_seconds.get(a.email, 0))
                         for a in config.accounts if account_seconds.get(a.email, 0) < quota_s}
            if remaining:
                _emit("goal_shortfall", target_seconds=quota_s, remaining_seconds=remaining)
        log.status = ("error" if not sends["ok"] and not sends["skipped"] else
                      "partial" if log.issues or sends["failed"] else "done")
    log_path = log.save()
    _log(f"\nlog salvo: {log_path.name}")
    if log.status != "stopped":
        # Só o nome do arquivo vai para a UI — nada de caminhos absolutos.
        _emit("campaign_done", log_path=log_path.name, status=log.status,
              ok_sends=sends["ok"], failed_sends=sends["failed"],
              skipped_sends=sends["skipped"], issues=len(log.issues),
              shortfall_accounts=sum(account_seconds.get(a.email, 0) < quota_s
                                     for a in config.accounts) if quota_s else 0,
              preparation_failures=sum(issue.get("kind") == "clip_prepare_done"
                                       for issue in log.issues))
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
    if not isinstance(files, list) or not isinstance(uf, list):
        raise RuntimeError("Minute devolveu uma lista de arquivos ilegível")
    status = "processing"
    quality = None
    if files and not uf:
        quality = files[0].get("quality") if isinstance(files[0], dict) else None
        status = "preview_ready"
    elif uf:
        states = [
            str(item.get("previewStatus") or "pending").strip().lower()
            if isinstance(item, dict) else "pending"
            for item in uf
        ]
        if any(value == "unavailable" for value in states):
            status = "unprocessed:unavailable"
        elif any(value not in {"pending", "processing"} for value in states):
            status = "unprocessed:" + next(
                value for value in states
                if value not in {"pending", "processing"})
        else:
            status = "processing"
    preview_states = [
        str(item.get("previewStatus") or "pending").strip().lower()
        if isinstance(item, dict) else "pending"
        for item in uf
    ]
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
def _rank_seed_bytes_cached(raw: bytes | None) -> dict[str, tuple[dict[str, Any], ...]] | None:
    """Carrega IDs e janelas; o arquivo não contém mídia, segredo ou narração."""
    try:
        payload = json.loads(gzip.decompress(raw)) if raw is not None else {}
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


def _load_rank_seed() -> dict[str, tuple[dict[str, Any], ...]] | None:
    return _rank_seed_bytes_cached(ego4d._selection_source(_rank_seed_path())[0])


_load_rank_seed.cache_clear = _rank_seed_bytes_cached.cache_clear


@ego4d.selection_boundary
def _rank_cache_stamp() -> tuple[tuple[str, int, str], ...]:
    """Content of the exact bound parser inputs; once per operation boundary."""
    ego4d._selection_rank_seed(_rank_seed_path())
    files = (
        config.MEDIA_DATA_DIR / "ego4d" / "ego4d.json",
        config.MEDIA_DATA_DIR / "ego4d" / "clips.csv",
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
        source = ego4d._selection_source(path)
        stamp.append((relative, source[1], source[2]))
    # Frozen modules live in PYZ; their __file__ paths need not exist. Bind the
    # actual rules and aliases as well, so an installed update cannot reuse an
    # index built with an older catalogue merely because both sources are missing.
    rules = ego4d._selection_rules_binding()
    stamp.append(("task-rules-semantic", rules["bytes"], rules["sha256"]))
    # Logic changes outside the declared rules also invalidate duration caches.
    stamp.append(("ranked-union", 12, ego4d._SELECTION_VERSION))
    return tuple(stamp)


def _rank_stamp_clear() -> None:
    # The stamp itself is no longer memoized without its content-bound key.
    pass


_rank_cache_stamp.cache_clear = _rank_stamp_clear


@ego4d.selection_boundary
def _rank_evidenced(buckets):
    return {name: tuple(ego4d.attach_selection_evidence(clip, name) for clip in clips)
            for name, clips in buckets.items()}


def _clip_window(clip: dict[str, Any]) -> tuple[float, float] | None:
    if (clip.get("source") == "nymeria"
            and clip.get("source_clock_domain") == "aria_DEVICE_TIME_ns"):
        canonical = clip.get("device_window_ns") or clip.get("planned_device_window_ns")
        if (isinstance(canonical, (list, tuple)) and len(canonical) == 2
                and all(type(value) is int and value >= 0 for value in canonical)
                and canonical[1] > canonical[0]):
            return canonical[0] / 1e9, canonical[1] / 1e9
        return None
    window = clip.get("window_s")
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        return None
    try:
        start, end = float(window[0]), float(window[1])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        return None
    return start, end


def _same_action_already_covered(
    kept: list[dict[str, Any]], clip: dict[str, Any],
) -> bool:
    """O corte oficial não entra de novo quando um trecho narrado já cobre a ação."""
    parent = str(clip.get("parent_video_uid") or "")
    window = _clip_window(clip)
    if not parent or window is None:
        return False
    start, end = window
    span = end - start
    for other in kept:
        if str(other.get("parent_video_uid") or "") != parent:
            continue
        other_window = _clip_window(other)
        if other_window is None:
            continue
        overlap = min(end, other_window[1]) - max(start, other_window[0])
        if overlap >= min(span, other_window[1] - other_window[0]) * 0.6:
            return True
    return False


def _union_ranked_clips(
    spans: dict[str, list[dict[str, Any]] | tuple[dict[str, Any], ...]],
    official: dict[str, list[dict[str, Any]] | tuple[dict[str, Any], ...]],
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Junta cortes narrados e clipes oficiais que passam na mesma prova de ação.

    Os dois lados já foram aceitos por `score_action`. O cenário sozinho não
    entra. O trecho narrado permanece primeiro.
    """
    names = set(spans) | set(official)
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for name in names:
        kept: list[dict[str, Any]] = []
        seen: set[str] = set()
        by_parent: dict[str, list[dict[str, Any]]] = {}

        def add(clip: dict[str, Any], *, allow_overlap: bool) -> None:
            try:
                duration = float(clip.get("dur_s") or 0)
            except (TypeError, ValueError):
                return
            if not min_dur_s <= duration <= max_dur_s:
                return
            uid = str(clip.get("clip_uid") or "")
            if not uid or uid in seen:
                return
            parent = str(clip.get("parent_video_uid") or "")
            overlapping = [other for other in by_parent.get(parent, ())
                           if _same_action_already_covered([other], clip)]
            aliases = set(clip.get("dedup_clip_uids") or ())
            for other in overlapping:
                # Alternate catalog IDs must not turn previously sent material
                # into fresh content when the catalog is expanded.
                aliases.add(str(other["clip_uid"]))
                aliases.update(other.get("dedup_clip_uids") or ())
                other["dedup_clip_uids"] = sorted(
                    (set(other.get("dedup_clip_uids") or ())
                     | set(clip.get("dedup_clip_uids") or ()) | {uid})
                    - {str(other["clip_uid"])})
            if not allow_overlap and overlapping:
                # A short narrated core must not erase a much longer verified
                # candidate. Only discard when the candidate itself is mostly
                # covered; aliases above still prevent resending its old core.
                window = _clip_window(clip)
                covered = []
                if window is not None:
                    for other in by_parent.get(parent, ()):
                        other_window = _clip_window(other)
                        if other_window is not None:
                            a = max(window[0], other_window[0])
                            b = min(window[1], other_window[1])
                            if b > a:
                                covered.append((a, b))
                    merged_end = window[0]
                    seconds = 0.0
                    for a, b in sorted(covered):
                        seconds += max(0.0, b - max(a, merged_end))
                        merged_end = max(merged_end, b)
                    if seconds < (window[1] - window[0]) * 0.6:
                        overlapping = []
                if overlapping:
                    return
            seen.add(uid)
            item = dict(clip)
            if aliases:
                item["dedup_clip_uids"] = sorted(aliases - {uid})
            kept.append(item)
            if parent:
                by_parent.setdefault(parent, []).append(item)

        for clip in spans.get(name, ()):
            add(clip, allow_overlap=True)
        def number(value: Any) -> float:
            try:
                value = float(value or 0)
                return value if math.isfinite(value) else 0
            except (TypeError, ValueError):
                return 0

        extras = list(official.get(name, ()))
        extras.sort(key=lambda clip: (
            -number(clip.get("match_score")),
            -number(clip.get("dur_s")),
            str(clip.get("clip_uid") or ""),
        ))
        for clip in extras:
            add(clip, allow_overlap=False)
        result[name] = tuple(kept)
    return result


def _link_rank_history(
    current: dict[str, tuple[dict[str, Any], ...]],
    previous: dict[str, tuple[dict[str, Any], ...]],
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Old IDs are provenance, not permission to retain rejected old content."""
    old_by_task: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for name, clips in previous.items():
        task = task_matching.canonical_task_name(name)
        parents = old_by_task.setdefault(task, {})
        for clip in clips:
            parent = str(clip.get("parent_video_uid") or "")
            parents.setdefault(parent, {})[str(clip.get("clip_uid") or "")] = clip
    linked = {}
    for name, clips in current.items():
        parents = old_by_task.get(task_matching.canonical_task_name(name), {})
        result = []
        for candidate in clips:
            clip = dict(candidate)
            uid = str(clip.get("clip_uid") or "")
            aliases = set(clip.get("dedup_clip_uids") or ())
            for old_uid, old in parents.get(str(clip.get("parent_video_uid") or ""), {}).items():
                if uid == old_uid or _same_action_already_covered([old], clip):
                    aliases.add(old_uid)
                    aliases.update(old.get("dedup_clip_uids") or ())
            aliases.discard(uid)
            if aliases:
                clip["dedup_clip_uids"] = sorted(aliases)
            result.append(clip)
        linked[name] = tuple(result)
    return linked


def _narration_file_present(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _local_narration_catalog() -> bool:
    """A biblioteca ativa tem narração. O índice portátil não a substitui."""
    return ego4d.has_timed_narrations() or _narration_file_present(
        config.MEDIA_DATA_DIR / "ego4d" / "clip_narrations.json")


def _stamp_hides_present_narration(stamp) -> bool:
    """Cache gravado com narração ausente não vale depois que o arquivo existe."""
    present = {
        "clip_narrations.json": _narration_file_present(
            config.MEDIA_DATA_DIR / "ego4d" / "clip_narrations.json"),
        "timed_narrations.jsonl": _narration_file_present(ego4d.timed_narrations_path()),
    }
    for item in stamp:
        if not item:
            continue
        name = str(item[0]).replace("\\", "/").rsplit("/", 1)[-1]
        if not present.get(name):
            continue
        size = item[1] if len(item) > 1 else 0
        digest = str(item[2]) if len(item) > 2 else ""
        if size == 0 or digest == "missing":
            return True
    return False


def _prepare_queue_accepts(clip: dict[str, Any], task_name: str | None, *,
                           fresh: bool = True) -> bool:
    """Trecho sem prova na biblioteca atual não entra na fila de preparo."""
    source = str(clip.get("source") or "")
    uid = str(clip.get("clip_uid") or "")
    if source == "nymeria" or uid.startswith("nymeria:"):
        try:
            if clip.get("acquisition_required") is True:
                nymeria.revalidate_planned_candidate(clip, task_name=task_name)
            else:
                nymeria.revalidate_candidate(clip, task_name=task_name, fresh=fresh)
        except (OSError, ValueError, RuntimeError, ImportError):
            return False
        return True
    if source == "holoassist":
        return True
    if not isinstance(clip.get("selection_evidence"), dict):
        return True
    # The portable seed can suggest a source, but queue admission must match
    # the same current evidence gate used by preparation on every installation.
    try:
        ego4d.revalidate_selection_evidence(clip, task_name=task_name, fresh=fresh)
    except ValueError:
        return False
    return True


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
    # Keep the portable seed's alternatives available (different duration
    # requests can need them), while linking their overlapping identities.
    combined = _union_ranked_clips(
        {name: [*buckets.get(name, ()), *seed.get(name, ())]
         for name in buckets.keys() | seed.keys() | task_matching.TASK_RULES.keys()}, {},
        min_dur_s=min_dur_s, max_dur_s=max_dur_s)
    result: dict[str, tuple[dict[str, Any], ...]] = {}
    for name, candidates in combined.items():
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in candidates:
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


@ego4d.selection_boundary
def _load_rank_cache(
    path: Path | None = None,
) -> dict[str, tuple[dict[str, Any], ...]] | None:
    path = path or _rank_cache_path()
    if not path.exists():
        return None
    try:
        # Legacy pickle caches are discarded, never executed during migration.
        payload = json.loads(path.read_bytes())
        if payload.get("schema") != 3:
            return None
        stamp, buckets = payload["stamp"], payload["buckets"]
        stamp = tuple(tuple(item) for item in stamp)
        narration_scan = payload.get("narration_scan") is True
    except Exception:  # noqa: BLE001 — cache corrompido = recompute
        return None
    if _local_narration_catalog() and not narration_scan:
        return None
    if _stamp_hides_present_narration(stamp):
        return None
    if stamp != _rank_cache_stamp() or not isinstance(buckets, dict):
        return None
    if any(not isinstance(name, str) or not isinstance(items, list)
           or any(not isinstance(item, dict) for item in items)
           for name, items in buckets.items()):
        return None
    return {name: tuple(items) for name, items in buckets.items()}


@ego4d.selection_boundary
def _save_rank_cache(
    buckets: dict[str, tuple[dict[str, Any], ...]],
    path: Path | None = None,
    *,
    narration_scan: bool | None = None,
) -> None:
    # Quem grava o cache desta biblioteca narrada precisa poder relê-lo.
    # narration_scan=False continua sendo o índice só portátil, recusado
    # quando as narrações existem.
    if narration_scan is None:
        narration_scan = _local_narration_catalog()
    try:
        payload = json.dumps({"schema": 3, "narration_scan": narration_scan,
                              "stamp": _rank_cache_stamp(),
                              "buckets": _rank_evidenced(buckets)},
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        path = path or _rank_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with tmp.open("xb") as handle:
                handle.write(payload)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError:
        pass


@lru_cache(maxsize=1)
def _ranked_pools_snapshot(stamp) -> dict[str, tuple[dict[str, Any], ...]]:
    cached = _load_rank_cache()
    if cached is not None:
        return _rank_evidenced(_merge_rank_seed(cached))
    if _local_narration_catalog():
        spans = (ego4d.rank_all_task_spans(min_dur_s=60, max_dur_s=1800)
                 if ego4d.has_timed_narrations() else {})
        official = task_matching.rank_all_tasks(_task_candidates())
        evidenced = (
            ego4d.narration_evidence_clips(min_dur_s=60, max_dur_s=1800)
            if ego4d.has_timed_narrations() else {})
        buckets = _union_ranked_clips(
            _union_ranked_clips(spans, official), evidenced)
        result = _rank_evidenced(_merge_rank_seed(buckets))
        _save_rank_cache(result, narration_scan=True)
        return result
    seed = _load_rank_seed()
    if seed and any(seed.values()):
        # Sem narração local, o índice portátil é o piso. Com narração, ele
        # só completa tarefas que a biblioteca não cobre.
        result = _rank_evidenced(_merge_rank_seed({}))
        _save_rank_cache(result, narration_scan=False)
        return result
    spans = ego4d.rank_all_task_spans(min_dur_s=60, max_dur_s=1800) if ego4d.has_timed_narrations() else {}
    official = task_matching.rank_all_tasks(_task_candidates())
    evidenced = (
        ego4d.narration_evidence_clips(min_dur_s=60, max_dur_s=1800) if ego4d.has_timed_narrations() else {})
    buckets = _union_ranked_clips(
        _union_ranked_clips(spans, official), evidenced)
    result = _rank_evidenced(_merge_rank_seed(buckets))
    _save_rank_cache(result, narration_scan=_local_narration_catalog())
    return result


@ego4d.selection_boundary
def _ranked_pools_cached() -> dict[str, tuple[dict[str, Any], ...]]:
    return _ranked_pools_snapshot(_rank_cache_stamp())


_ranked_pools_cached.cache_clear = _ranked_pools_snapshot.cache_clear


_RANK_INPUT_SIGNATURE = None


def _refresh_rank_inputs() -> None:
    """Called under _RANK_LOCK; new metadata must replace in-memory rankings."""
    global _RANK_INPUT_SIGNATURE
    paths = (
        config.MEDIA_DATA_DIR / "ego4d" / "ego4d.json",
        config.MEDIA_DATA_DIR / "ego4d" / "clips.csv",
        config.MEDIA_DATA_DIR / "ego4d" / "clip_narrations.json",
        ego4d.timed_narrations_path(), _rank_seed_path(),
    )
    signature = []
    for path in paths:
        source = ego4d._selection_source(path)
        signature.append((str(path), source[1], source[2]))
    signature = tuple(signature)
    if signature == _RANK_INPUT_SIGNATURE:
        return
    _task_candidates.cache_clear()
    _rank_cache_stamp.cache_clear()
    _load_rank_seed.cache_clear()
    _ranked_pools_cached.cache_clear()
    _duration_ranked_pools.cache_clear()
    _RANK_INPUT_SIGNATURE = signature


@ego4d.selection_boundary
def _ranked_pools() -> dict[str, tuple[dict[str, Any], ...]]:
    """Todas as tasks de uma vez. Cache em disco para o GET /api/tasks não congelar a UI."""
    with _RANK_LOCK:
        ego4d.ensure_task_annotations()
        _refresh_rank_inputs()
        return _ranked_pools_cached()


@lru_cache(maxsize=8)
def _duration_ranked_snapshot(stamp, min_dur_s: float, max_dur_s: float):
    """Recheck indexed source windows once per content-bound duration range."""
    cache_key = hashlib.sha256(
        f"{float(min_dur_s):.6f}|{float(max_dur_s):.6f}".encode("ascii")
    ).hexdigest()[:16]
    path = config.DATA_DIR / f"task_rank_cache_{cache_key}.pkl"
    cached = _load_rank_cache(path)
    if cached is not None:
        return _rank_evidenced(_merge_rank_seed(
            cached, min_dur_s=min_dur_s, max_dur_s=max_dur_s))
    full = _ranked_pools_cached()
    result = {
        name: tuple(clip for clip in rows
                    if min_dur_s <= float(clip.get("dur_s") or 0) <= max_dur_s)
        for name, rows in full.items()
    }
    if ego4d.has_timed_narrations():
        windows = _duration_window_candidates(full)
        if windows:
            recuts = ego4d.revalidate_task_windows(
                windows, min_dur_s=min_dur_s, max_dur_s=max_dur_s)
            result = _union_ranked_clips(
                result, recuts, min_dur_s=min_dur_s, max_dur_s=max_dur_s)
            result = _link_rank_history(result, full)
    result = _rank_evidenced(result)
    _save_rank_cache(result, path, narration_scan=_local_narration_catalog())
    return result


def _duration_window_candidates(buckets) -> dict[str, list[dict[str, Any]]]:
    """Union indexed footage before requesting new verified segmentation.

    Adjacent five-minute scene windows can support a longer requested take.
    Every union is revalidated against current annotations and sensor coverage;
    a gap in the original source windows remains a gap here.
    """
    ranges: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for name, clips in buckets.items():
        name = task_matching.canonical_task_name(name)
        for clip in clips:
            parent = str(clip.get("parent_video_uid") or "")
            window = _clip_window(clip)
            if parent and window is not None:
                ranges.setdefault((name, parent), []).append(window)
    candidates: dict[str, list[dict[str, Any]]] = {}
    for (name, parent), windows in ranges.items():
        merged: list[tuple[float, float]] = []
        for start, end in sorted(windows):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for start, end in merged:
            for relative_start, relative_end in ego4d.split_parent_windows(end - start):
                candidates.setdefault(name, []).append({
                    "parent_video_uid": parent,
                    "window_s": (start + relative_start, start + relative_end),
                })
    return candidates


@ego4d.selection_boundary
def _duration_ranked_pools(min_dur_s: float, max_dur_s: float):
    return _duration_ranked_snapshot(_rank_cache_stamp(), min_dur_s, max_dur_s)


_duration_ranked_pools.cache_clear = _duration_ranked_snapshot.cache_clear


def _nymeria_windows(task_name: str, min_dur_s: float,
                     max_dur_s: float, *, catalog_only: bool = False) -> tuple[dict[str, Any], ...]:
    """Nymeria owns a cache bound to exact current roots/media/annotations."""
    try:
        options = {"catalog_only": True} if catalog_only else {}
        clips = nymeria.automatic_candidates(task_name=task_name,
            min_dur_s=min_dur_s, max_dur_s=max_dur_s, include_planned=True, **options)
    except Exception:
        return ()
    return tuple(dict(clip) for clip in clips)


_nymeria_windows.cache_clear = nymeria.clear_caches


def _compatible_task_clips(
    task_name: str,
    dataset_provider: str = "all",
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
    catalog_only: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Combina fontes compatíveis sem reinterpretar categorias.

    HoloAssist só participa das duas tarefas de móveis explicitamente
    mapeadas; ausência do índice local não afeta o catálogo Ego4D.
    """
    provider = normalize_dataset_provider(dataset_provider)
    ego_clips: tuple[dict[str, Any], ...] = ()
    task_name = task_matching.canonical_task_name(task_name)
    if provider in ("all", "ambos", "ego4d"):
        ego4d.ensure_task_annotations()
        if ((min_dur_s, max_dur_s) != (60, 1800)
                and ego4d.has_timed_narrations()):
            with _RANK_LOCK:
                _refresh_rank_inputs()
                pools = _duration_ranked_pools(min_dur_s, max_dur_s)
        else:
            pools = _ranked_pools()
        if task_name in pools:
            ego_clips = pools[task_name]
        else:
            ego_clips = tuple(task_matching.ranked_clips(task_name, _task_candidates()))
        ego_clips = tuple(c for c in ego_clips
                          if min_dur_s <= float(c.get("dur_s") or 0) <= max_dur_s)
    if provider == "ego4d":
        return ego_clips
    if provider == "nymeria":
        return _nymeria_windows(task_name, min_dur_s, max_dur_s, catalog_only=catalog_only)
    holo_clips: tuple[dict[str, Any], ...] = ()
    if provider in ("all", "holoassist"):
        try:
            holo_clips = tuple(holoassist.list_clips(
                task_name, min_dur_s=min_dur_s, max_dur_s=max_dur_s))
        except FileNotFoundError:
            holo_clips = ()
    nymeria_clips = (_nymeria_windows(task_name, min_dur_s, max_dur_s, catalog_only=catalog_only)
                     if provider in ("all", "ambos") else ())
    if provider == "ambos":
        return (*nymeria_clips, *ego_clips)
    return (*holo_clips, *nymeria_clips, *ego_clips)


def _with_cached_expansion(
    clips: list[dict[str, Any]],
    task_name: str,
    *,
    min_dur_s: float,
    max_dur_s: float,
    work_dir: Path | None = None,
    include_disabled: bool = False,
    catalog_only: bool = False,
) -> list[dict[str, Any]]:
    """Acrescenta cenário já gravado pelo acelerador, sem buscar mídia nova.

    Restaurado de v1.0.73/v2.0.2: o rank narrado sozinho deixava clipes
    `_native` prontos fora do pool. Melhor aproveitamento do Ego4D em disco.
    """
    from .ego_accelerator import ready_scenario_clips

    merged = list(clips)
    seen = {str(clip.get("clip_uid") or "") for clip in merged}
    options = {"catalog_only": True} if catalog_only else {}
    for extra in ready_scenario_clips(
            task_name, min_dur_s=min_dur_s, max_dur_s=max_dur_s,
            work_dir=work_dir, allow_disabled=include_disabled, **options):
        uid = str(extra.get("clip_uid") or "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        merged.append(extra)
    return merged


def _clip_is_cached(clip: dict[str, Any], work_dir: Path) -> bool:
    if str(clip.get("source") or "") == "holoassist":
        from .holo_accelerator import clip_ready
        return clip_ready(clip, work_dir)
    if str(clip.get("source") or "") == "nymeria" or str(
            clip.get("clip_uid") or "").startswith("nymeria:"):
        stem = "nymeria_" + str(clip.get("clip_uid") or "").replace(
            ":", "_").replace(".", "_")
        return (Path(work_dir) / f"{stem}_native.mp4").is_file()
    return ego_clip_cache_state(clip, work_dir) == "ready"


def _catalog_clip_cached_hint(clip: dict[str, Any], work_dir: Path) -> bool:
    """Read inventory/encode metadata only; this never admits a media send.

    Category counts do not need to hash a parent MP4 for each of its windows.
    The actual cache and source bytes are checked by prepare/upload as before.
    A same-size replacement can therefore retain this hint until that gate.
    """
    try:
        work = Path(work_dir)
        source_name = str(clip.get("source") or "ego4d")
        if source_name == "nymeria":
            if clip.get("acquisition_required") is True:
                return False
            uid = str(clip.get("clip_uid") or "")
            if not uid.startswith("nymeria:"):
                return False
            stem = "nymeria_" + uid.replace(":", "_").replace(".", "_")
            seq_dir = Path(str(clip.get("path") or ""))
            paths = (work / f"{stem}_native.mp4",
                     seq_dir / "recording_head/data/data.vrs",
                     seq_dir / "recording_head/data/motion.vrs")
            return all(path.is_file() and path.stat().st_size > 0 for path in paths)
        if source_name == "holoassist":
            from .holo_accelerator import native_path, source_path, sensors_ready
            source, native = source_path(clip), native_path(clip, work)
            if not sensors_ready(clip):
                return False
            start_s = dur_s = None
        else:
            # Ranked candidates and official rows carry source coordinates.
            # Do not resolve incomplete inventory through sync_meta here.
            row = dict(clip)
            uid = str(row.get("exported_clip_uid") or row.get("clip_uid") or "")
            if not uid:
                return False
            window = row.get("window_s")
            if isinstance(window, (list, tuple)) and len(window) == 2:
                row.update(parent_start_sec=window[0], parent_end_sec=window[1])
            elif "parent_start_sec" not in row or "parent_end_sec" not in row:
                return False
            row["exported_clip_uid"] = uid
            plan = _ego_prepare_plan(row)
            source, native = work / plan["source_name"], work / plan["native_name"]
            imu = work / plan["imu_name"]
            if not imu.is_file() or imu.stat().st_size <= 128:
                return False
            start_s, dur_s = plan["norm_start"], plan["dur_s"]
        if not source.is_file() or not native.is_file():
            return False
        source_stat, native_stat = source.stat(), native.stat()
        if min(source_stat.st_size, native_stat.st_size) <= 1024 * 1024:
            return False
        marker = native.with_name(native.name + ".source.json")
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            return False
        expected = {"version": _NATIVE_CACHE_VERSION,
                    "source_size": source_stat.st_size,
                    "source_mtime_ns": source_stat.st_mtime_ns,
                    "prepared_size": native_stat.st_size,
                    "start_s": None if start_s is None else round(float(start_s), 6),
                    "dur_s": None if dur_s is None else round(float(dur_s), 6),
                    "width": 1440, "height": 1080, "fps": 30}
        return (all(saved.get(key) == value and type(saved.get(key)) is type(value)
                    for key, value in expected.items())
                and all(isinstance(saved.get(key), str)
                        and re.fullmatch(r"[0-9a-f]{64}", saved[key])
                        for key in ("source_sha256", "prepared_sha256")))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def _catalog_queue_accepts(clip: dict[str, Any], task_name: str) -> bool:
    """Check current catalog declarations without repeating Nymeria planning.

    These rows were produced by the provider's read-only catalog operation.
    They are estimates, never a substitute for the fresh preparation gate.
    """
    if clip.get("source") != "nymeria":
        return _prepare_queue_accepts(clip, task_name, fresh=False)
    try:
        name = task_matching.canonical_task_name(task_name)
        carrier = clip["selection_evidence"]
        task = carrier["task"]
        if (carrier.get("schema") != 1 or carrier.get("dataset") != "nymeria"
                or task.get("name") != name or clip.get("task_name_authoritative") != name
                or task_matching.rule_for(name) is None):
            return False
        planned = clip.get("acquisition_required") is True
        algorithm = nymeria._PLANNED_ALGORITHM if planned else nymeria._ALGORITHM
        window = _clip_window(clip)
        duration = float(clip["dur_s"])
        if (carrier.get("algorithm") != algorithm or window is None
                or not math.isfinite(duration) or duration <= 0
                or abs((window[1] - window[0]) - duration) > 1e-6):
            return False
        bounds = carrier["duration_bounds_s"]
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                or not float(bounds[0]) <= duration <= float(bounds[1])):
            return False
        if planned:
            device_window = clip.get("planned_device_window_ns")
            return (carrier.get("sensor_coverage") == "unmeasured"
                    and clip.get("selection_ready") is False
                    and device_window == clip.get("device_window_ns")
                    and device_window == carrier.get("planned_device_window_ns")
                    and clip.get("clip_uid") ==
                    f"nymeria-planned:{clip['seq_id']}:{device_window[0]}:{device_window[1]}")
        return str(clip.get("clip_uid") or "").startswith("nymeria:")
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _prefer_cached_clips(
    clips: list[dict[str, Any]], work_dir: Path, *,
    prioritize: bool = True,
) -> list[dict[str, Any]]:
    """Usa mídia pronta primeiro; orçamento de pre-cache não limita a campanha."""
    ordered = list(clips)
    ready: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    in_original_order: list[dict[str, Any]] = []
    for clip in ordered:
        if _clip_is_cached(clip, work_dir):
            marked = {**clip, "_cache_ready_at_selection": True}
            ready.append(marked)
            in_original_order.append(marked)
        else:
            later.append(clip)
            in_original_order.append(clip)
    return ready + later if prioritize else in_original_order


@campaign_state_operation
def warm_task_catalog() -> None:
    """Load the prepared catalog before the first category request."""
    _ranked_pools()


@ego4d.selection_boundary
def available_tasks(email: str, org_key: str, *, min_dur_s: float = 60,
                    max_dur_s: float = 1800,
                    include_unavailable: bool = False,
                    dataset_provider: str = "all",
                    content_mode: str = "both",
                    session: Session | None = None,
                    remote_tasks: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Devolve as tasks do Minute que têm mídia elegível com IMU real.

    Cada item traz clip_count e dur_range_s já calculados para a faixa pedida.
    Com include_unavailable, tarefas compatíveis apenas fora da faixa pedida
    também são devolvidas para a UI explicar por que estão desabilitadas.
    `session` reusa a Session já autenticada (o GET /api/tasks não pode
    refreshar o Firebase duas vezes na mesma conta).
    """
    if remote_tasks is None:
        sess = session
        if sess is None:
            sess = Session.from_email(email)
            sess.ensure_auth(org_key=org_key)
        elif not getattr(sess, "_live", False):
            sess.ensure_auth(org_key=org_key)
        tasks = sess.all_tasks(org_key)
    else:
        tasks = remote_tasks
    tasks = validate_task_catalog(tasks)
    mode = normalize_content_mode(content_mode)
    ready_by_uid: dict[str, bool] = {}
    eligibility: dict[tuple[str, str], bool] = {}

    def queue_accepts(clip: dict[str, Any], name: str) -> bool:
        # Both duration pools share the request's immutable parser inputs.
        # Include the whole candidate/evidence and task, never only its UID.
        key = (task_matching.canonical_task_name(name), ego4d._selection_digest(clip))
        if key not in eligibility:
            eligibility[key] = _catalog_queue_accepts(clip, name)
        return eligibility[key]

    def cache_ready(clip: dict[str, Any]) -> bool:
        uid = str(clip.get("clip_uid") or "")
        if uid not in ready_by_uid:
            ready_by_uid[uid] = _catalog_clip_cached_hint(
                clip, config.MEDIA_DATA_DIR / "ego4d")
        return ready_by_uid[uid]

    out = []
    for t in tasks:
        name = (t.get("name") or "").strip()
        rule = task_matching.rule_for(name)
        if not rule:
            if include_unavailable:
                out.append({
                    "id": t.get("id"), "name": name, "name_pt": name,
                    "description": str(t.get("description") or ""),
                    "clip_count": 0, "clip_sources": {}, "overall_clip_count": 0,
                    "available_for_duration": False, "mapping_supported": False,
                    "unavailable_reason": "Esta tarefa ainda não tem uma regra de seleção no QMoney.",
                })
            continue
        all_clips = [c for c in _compatible_task_clips(name, dataset_provider, catalog_only=True)
                     if 60 <= c["dur_s"] <= 1800]
        clips = list(_compatible_task_clips(
            name, dataset_provider, min_dur_s=min_dur_s, max_dur_s=max_dur_s, catalog_only=True))
        if (mode != "dataset"
                and normalize_dataset_provider(dataset_provider) in ("all", "ambos", "ego4d")):
            all_clips = _with_cached_expansion(
                all_clips, name, min_dur_s=60, max_dur_s=1800,
                include_disabled=True, catalog_only=True)
            clips = _with_cached_expansion(
                clips, name, min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                include_disabled=True, catalog_only=True)
        if mode == "cache":
            all_clips = [clip for clip in all_clips if cache_ready(clip)]
            clips = [clip for clip in clips if cache_ready(clip)]
        all_clips = [clip for clip in all_clips if queue_accepts(clip, name)]
        clips = [clip for clip in clips if queue_accepts(clip, name)]
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
                "name_pt": TASK_NAME_PT.get(name, TASK_NAME_PT.get(
                    task_matching.canonical_task_name(name), name)),
                "boosted": name in BOOSTED_TASKS,
                "category_slug": category_slug,
                "category_label": CATEGORY_PT.get(
                    category_slug, str(category.get("label") or "Outras")),
                "clip_count": len(clips),
                **diversity_summary(clips),
                "clip_sources": source_counts,
                "dur_range_s": ((min(c["dur_s"] for c in clips),
                                 max(c["dur_s"] for c in clips)) if clips else None),
                "overall_clip_count": len(all_clips),
                "overall_dur_range_s": ((min(c["dur_s"] for c in all_clips),
                                         max(c["dur_s"] for c in all_clips))
                                        if all_clips else None),
                "available_for_duration": bool(clips),
                "mapping_supported": True,
                "requires_measured_validation": True,
                "unavailable_reason": ("" if clips else
                    "Há conteúdo no catálogo, mas nenhum trecho nesta faixa de duração."
                    if all_clips else
                    "As anotações Ego4D ainda não estão disponíveis. Configure o acesso em Integrações e atualize as categorias."
                    if normalize_dataset_provider(dataset_provider) == "ego4d"
                    and not _local_narration_catalog() else
                    "Nenhum trecho atende à atividade e aos sensores no provedor escolhido."),
                "match_confidence": rule.confidence,
                "match_scenarios": list(rule.primary),
            })
    return out
