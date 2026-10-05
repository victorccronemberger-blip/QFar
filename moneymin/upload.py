"""
upload.py — Réplica do fluxo de upload do app Minute (com.bakerdata.minute).

Pipeline de 6 etapas (espelha o fluxo nativo do app Android):
  1. POST  /api/v1/uploads                                  -> registra o chunk
  2. POST  /api/v1/storage/sas/blobs                        -> SAS URLs do Azure
  3. PUT   <blob_url>  (x-ms-blob-type: BlockBlob)          -> bytes direto no Azure
  4. PATCH /api/v1/uploads/{id}/complete                    -> confirma conclusão
  5. POST  /api/v1/uploads/{id}/evaluate  (opcional)         -> verifica qualidade
  6. POST  /api/v1/organizations/{org}/sessions/{sid}/finalize -> confirma sessão

Comportamento nativo adicional (observado no bundle do app e na spec):
  - PATCH /api/v1/uploads/{id}/fail          -> marca o upload como falho (error_message)
  - GET   /api/v1/uploads/{upload_id}        -> consulta status (upload_status)
  - suppress_per_chunk_catbear=True + network_type no PATCH complete (APK 1.29.0)
  - retries limitados nas etapas compatíveis; CREATE sem recibo não é repetido
  - retry-late: após exaurir retries o chunk fica pendente p/ tentativa futura
  - loss record: arquivo local sumiu -> registra perda em vez de abortar
  - sidecar persistente (data/sidecars/<session_id>.json) + fila de retomada

A API nunca toca nos bytes do MP4 — eles vão direto pro Azure Blob Storage.
O registro é criado antes da emissão das URLs SAS nos novos envios, conforme
driveCreate do APK 1.29.0 (S22).
Jornais legados conservam a política de ordem para retomar a etapa pendente.
Cada arquivo _N.mp4 é um chunk de uma mesma sessionId.

Usa `Session` (refresh automático de token) e `config` (URLs/chaves centralizadas).
Somente stdlib.

Exemplo (chunk único):
    from moneymin.minute_api import Session
    from moneymin.upload import upload_session

    sess = Session.from_email("seu@email.com")
    result = upload_session(sess, "data/videos/clip.mp4", org_key="sua_org_key",
                           task_id="uuid-da-task")
    print(result.upload_id)

Exemplo (multi-chunk):
    result = upload_session(sess, ["chunk0.mp4", "chunk1.mp4"], org_key="...",
                           task_id="...")

Exemplo (fila com retomada):
    from moneymin.upload import enqueue_upload, pump_pending

    enqueue_upload(sess, "data/videos/clip.mp4", org_key="...", task_id="...")
    pump_pending(sess)   # processa sidecars pendentes (retry-late / loss)
"""
from __future__ import annotations

import io
import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.parse
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, transport
from .atomic_io import JsonStateError, decode_json_state, load_json_state, save_bytes, save_json
from .capture_import import CaptureDescriptor, CaptureImportError, inspect_original_capture
from .device_profile import (
    BACKLOG_CAP_MS,
    DeviceProfile,
    format_recorded_at,
    recorded_at_to_wall_ms,
    recording_start_epoch,
)
from .sidecar import (
    build_metadata_json,
    build_sidecar_zip,
    ffmpeg_bin,
    probe_video,
)
from .upload_types import (
    STATE_COMPLETING,
    STATE_CREATING,
    STATE_DONE,
    STATE_FAILED,
    STATE_LOSS,
    STATE_QUARANTINE,
    STATE_RETRY_LATE,
    STATE_TRANSPORT,
    TRANSIENT_STATES,
    ChunkResult,
    UploadError,
    UploadResult,
    is_pending_finalization,
    journal_delivery_confirmed,
    journal_flags_valid,
)

__all__ = [
    "ChunkResult",
    "STATE_COMPLETING",
    "STATE_CREATING",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_LOSS",
    "STATE_QUARANTINE",
    "STATE_RETRY_LATE",
    "STATE_TRANSPORT",
    "TRANSIENT_STATES",
    "UploadError",
    "UploadResult",
    "enqueue_upload",
    "pump_pending",
    "trim_video",
    "upload_session",
    "upload_video",
]

UPLOAD_MIN_DURATION_MS = 60_000
UPLOAD_MAX_DURATION_MS = 1_800_000


# --- Metadados de dispositivo/plataforma/video/rede (mimica app nativo) ---------

def default_device_meta(profile: DeviceProfile | None = None) -> dict[str, Any]:
    """Metadados de dispositivo que o app nativo envia em meta.device.

    Formato curto Android (getDeviceUploadMeta): APENAS {"model": Build.MODEL}.
    NÃO envia systemName nem systemVersion no POST /uploads.
    Com perfil: o Build.MODEL Samsung daquele aparelho, não o default global.
    """
    if profile is not None:
        return profile.upload_device_meta()
    return {
        "model": config.NATIVE_DEVICE_MODEL,
    }


def default_platform_meta(profile: DeviceProfile | None = None) -> dict[str, Any]:
    """Metadados de plataforma que o app nativo envia em meta.platform.

    Formato curto Android (getDeviceUploadMeta): APENAS {"os": "android"}.
    NÃO envia version no POST /uploads.
    """
    if profile is not None:
        return profile.upload_platform_meta()
    return {
        "os": config.NATIVE_PLATFORM_OS,
    }


def _iso_now() -> str:
    return format_recorded_at(time.time())


def _normalize_recorded_at_sequence(
    values: list[str],
    durations_ms: list[int],
    *,
    now: float | None = None,
) -> list[str]:
    """Canoniza horários explícitos sem destruir a distância entre chunks.

    O chamador já usou esses mesmos horários para montar os sidecars. Portanto
    não podemos prender cada chunk à duração total da sessão: isso colapsa os
    últimos horários e faz o POST divergir de ``metadata.createdAt``. Valores
    explícitos são validados como uma sequência e apenas reformatados.
    """
    if len(values) != len(durations_ms):
        raise UploadError(
            "recorded_at precisa ter exatamente um horário por chunk",
            transient=False,
        )
    if not values:
        return []
    wall_ms: list[int] = []
    for value in values:
        parsed = recorded_at_to_wall_ms(str(value))
        if parsed is None:
            raise UploadError(
                f"recorded_at inválido: {value!r}", transient=False)
        wall_ms.append(parsed)
    if any(current <= previous
           for previous, current in zip(wall_ms, wall_ms[1:], strict=False)):
        raise UploadError(
            "recorded_at dos chunks precisa ser estritamente crescente",
            transient=False,
        )
    now_ms = int((time.time() if now is None else float(now)) * 1000)
    backlog_cap_ms = int(
        config.recording_limits().get("backlog_cap_ms") or BACKLOG_CAP_MS)
    if wall_ms[0] < now_ms - backlog_cap_ms:
        raise UploadError(
            "recorded_at está fora do backlog de gravação do Minute",
            transient=False,
        )
    last_end_ms = max(
        started + max(0, int(duration))
        for started, duration in zip(wall_ms, durations_ms, strict=True)
    )
    # Pequena tolerância só para diferença de relógio entre captura e chamada.
    if last_end_ms > now_ms + 5_000:
        raise UploadError(
            "recorded_at indica chunk que ainda não terminou",
            transient=False,
        )
    return [format_recorded_at(value / 1000.0) for value in wall_ms]


def _recorded_at_sequence_from_base(
    base: str,
    durations_ms: list[int],
    *,
    now: float | None = None,
) -> list[str]:
    """Expande o início da sessão em inícios contínuos de cada chunk."""
    base_ms = recorded_at_to_wall_ms(base)
    if base_ms is None:
        raise UploadError(f"recorded_at inválido: {base!r}", transient=False)
    cursor = base_ms
    values: list[str] = []
    for duration in durations_ms:
        values.append(format_recorded_at(cursor / 1000.0))
        cursor += max(0, int(duration))
    return _normalize_recorded_at_sequence(values, durations_ms, now=now)


def default_video_meta() -> dict[str, Any]:
    """Metadados de vídeo que o app nativo envia em meta.video.

    Shape Android: {height, path, rotationDeg, width} (path = {logId}.mp4).
    """
    return {
        "height": 1080,
        "path": "",
        "rotationDeg": 0,
        "width": 1440,
    }


def default_network_meta() -> dict[str, Any]:
    """Metadados de rede que o app nativo envia em meta.network."""
    return {
        "type": "wifi",
        "carrier": None,
    }


# --- HTTP para o Azure Blob (PUT direto, fora da minute-api) -------------------

def _put_blob(blob_url: str, file_bytes: bytes, content_type: str = "video/mp4",
              timeout: int = 300, resumable: bool | None = None,
              on_progress: Callable[[int, int, float], None] | None = None) -> int:
    """Faz PUT dos bytes no Azure Blob Storage. Devolve o status HTTP.

    O transporte (e o fingerprint de rede) é do `transport.py`: com curl_cffi
    o MP4 e o sidecar sobem em blocos de 4MB + Put Block List, como o
    AzureBlockUploader/OkHttp do app Android. Sem curl_cffi, PUT BlockBlob
    único. Sucesso é 201 Created (o OkHttp do app aceita qualquer 2xx).

    Levanta UploadError se o Azure recusar (status != 201) ou em erro de rede.
    """
    try:
        if on_progress is not None:
            return transport.put_blob(blob_url, file_bytes,
                                      content_type=content_type, timeout=timeout,
                                      resumable=resumable, on_progress=on_progress)
        return transport.put_blob(blob_url, file_bytes,
                                  content_type=content_type, timeout=timeout,
                                  resumable=resumable)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise _http_upload_error("PUT Blob", exc.code, body) from exc
    except UploadError:
        raise
    except Exception as exc:
        status = _status_from_exception(exc)
        if status is not None:
            raise _http_upload_error("PUT Blob", status, str(exc)) from exc
        raise UploadError(
            f"PUT Blob falhou (erro de rede): {exc}",
            transient=True, phase="transport",
        ) from exc


def _put_blob_file(blob_url: str, file_path: str | Path,
                   content_type: str = "video/mp4",
                   timeout: int = 300,
                   on_progress: Callable[[int, int, float], None] | None = None,
                   *, expected_sha256: str | None = None) -> int:
    """PUT do vídeo a partir do disco, sem uma cópia integral na RAM."""
    try:
        if expected_sha256 is not None:
            return transport.put_blob_file(blob_url, file_path,
                                           content_type=content_type, timeout=timeout,
                                           on_progress=on_progress, expected_sha256=expected_sha256)
        return transport.put_blob_file(blob_url, file_path,
                                       content_type=content_type,
                                       timeout=timeout,
                                       on_progress=on_progress)
    except transport.TransportIntegrityError:
        raise UploadError("O conteúdo local mudou; o recibo e os arquivos foram preservados para revisão.",
                          transient=False, phase="transport", review_required=True) from None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise _http_upload_error("PUT Blob", exc.code, body) from exc
    except UploadError:
        raise
    except Exception as exc:
        status = _status_from_exception(exc)
        if status is not None:
            raise _http_upload_error("PUT Blob", status, str(exc)) from exc
        raise UploadError(
            f"PUT Blob falhou (erro de rede): {exc}",
            transient=True, phase="transport",
        ) from exc


def _probe_duration_ms(video_path: str | Path) -> int:
    """Duração em ms: ffprobe, senão `ffmpeg -i` (imageio), senão PyAV."""
    return int(probe_video(video_path).get("duration_ms") or 0)


# --- Normalização de vídeo (full-range -> limited-range) -----------------------

def _video_pix_fmt(video_path: str | Path) -> tuple[str | None, str | None]:
    """pix_fmt / color_range via probe unificado (ffprobe ou ffmpeg -i)."""
    info = probe_video(video_path)
    return info.get("pix_fmt"), info.get("color_range")


def _video_handler(video_path: str | Path) -> str | None:
    """handler_name do stream de vídeo (ex.: 'VideoHandler')."""
    return probe_video(video_path).get("handler_name")


def _run_ffmpeg(cmd: list[str], timeout: int = 3600):
    """Roda ffmpeg sem stdin (evita pausa) e sem console no Windows."""
    import os
    import subprocess
    kwargs: dict[str, Any] = {
        "capture_output": True, "text": True, "timeout": timeout,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, **kwargs)


def normalize_video(video_path: str | Path, out_dir: str | Path | None = None,
                    preset: str = "veryfast") -> Path:
    """Reencoda o vídeo para o formato EGO nativo do app Android (1440x1080 4:3).

    O app grava a ultra-wide em 1440x1080 (4:3); datasets/YouTube vêm em
    16:9 (1920x1080). O Catbear pontua melhor (clarity=great) o que replica a
    gravação Android: proporção 4:3, yuv420p limited-range, H.264 High@4.2
    (MediaRecorder setVideoEncodingProfileLevel(8, 8192)), handler
    VideoHandler/SoundHandler e áudio AAC 48 kHz estéreo 256 kbps.

    Conversão: escala para cobrir 1440x1080 (force_original_aspect_ratio=increase)
    e corta o excesso (crop) para 4:3 — conteúdo central preservado.

    Devolve o caminho pronto para upload (o próprio arquivo se já for
    compatível, ou um arquivo `_yuv420p.mp4` ao lado).
    Levanta UploadError se precisar reencodar e ffmpeg faltar/falhar.
    """
    video_path = Path(video_path)
    pix_fmt, _color_range = _video_pix_fmt(video_path)
    handler = _video_handler(video_path) or ""
    w, h = _video_dims(video_path)
    native_43 = abs((w / h) - (4.0 / 3.0)) < 0.02
    needs = (pix_fmt == "yuvj420p" or "videohandler" not in handler.lower()
             or not native_43 or w != 1440)
    if not needs:
        return video_path

    out_dir = Path(out_dir) if out_dir else video_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{video_path.stem}_yuv420p.mp4"
    if out.exists():
        out.unlink()
    cmd = [
        ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-i", str(video_path),
        "-c:v", "libx264", "-preset", preset,
        "-b:v", "8000k", "-maxrate", "8000k", "-bufsize", "16000k",
        "-profile:v", "high", "-level:v", "4.2",
        "-g", "30", "-keyint_min", "30",
        "-vf", ("scale=1440:1080:force_original_aspect_ratio=increase,"
                "crop=1440:1080,"
                "scale=in_range=full:out_range=tv,format=yuv420p"),
        "-r", "30",
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-metadata:s:v:0", "handler_name=VideoHandler",
        "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "256k",
        "-metadata:s:a:0", "handler_name=SoundHandler",
        "-movflags", "+faststart", "-f", "mp4", str(out),
    ]
    try:
        res = _run_ffmpeg(cmd)
    except FileNotFoundError as exc:
        raise UploadError(
            "vídeo precisa de reencode para formato Android (1440x1080 yuv420p), "
            "mas o FFmpeg do QMoney não está disponível; use Reparar instalação "
            "na aba Integrações") from exc
    if res.returncode != 0:
        raise UploadError(
            f"falha ao normalizar vídeo para formato Android: {res.stderr.strip()[:300]}")
    return out


def _video_dims(video_path: str | Path) -> tuple[int, int]:
    """Resolução (width, height) do stream de vídeo."""
    info = probe_video(video_path)
    return int(info.get("width") or 0), int(info.get("height") or 0)


def trim_video(
    video_path: str | Path,
    cut_first: float = 30.0,
    cut_last: float = 30.0,
    auto: bool = True,
    out_dir: str | Path | None = None,
    margin: float = 1.0,
) -> str:
    """Corta intro/outro de vídeos (30s início + 30s fim por padrão).

    Padrão: remove 30 segundos do início e 30 segundos do fim — prático,
    rápido e seguro para vídeos de YouTube que sempre têm intro/outro.

    Devolve o caminho do vídeo cortado (o próprio arquivo se não precisou corte,
    ou um arquivo _trimmed.mp4 ao lado).
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise UploadError(f"vídeo não encontrado: {video_path}")

    duration_ms = _probe_duration_ms(video_path)
    if not duration_ms:
        raise UploadError(f"não foi possível ler a duração de {video_path}")
    duration = duration_ms / 1000.0

    start = cut_first
    end = duration - cut_last

    # Se o vídeo for muito curto, não cortar
    if end <= start:
        return str(video_path)

    # Cortar
    out_dir = Path(out_dir) if out_dir else video_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{video_path.stem}_trimmed.mp4"
    if out.exists():
        out.unlink()

    cmd = [
        ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-ss", str(start),
        "-i", str(video_path),
        "-t", str(end - start),
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", "8000k", "-maxrate", "8000k", "-bufsize", "16000k",
        "-profile:v", "high", "-level:v", "4.2",
        "-g", "30", "-keyint_min", "30",
        "-vf", ("scale=1440:1080:force_original_aspect_ratio=increase,"
                "crop=1440:1080,"
                "scale=in_range=full:out_range=tv,format=yuv420p"),
        "-r", "30",
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-metadata:s:v:0", "handler_name=VideoHandler",
        "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "256k",
        "-metadata:s:a:0", "handler_name=SoundHandler",
        "-movflags", "+faststart", "-f", "mp4",
        str(out),
    ]
    try:
        res = _run_ffmpeg(cmd)
    except FileNotFoundError as exc:
        raise UploadError("ffmpeg não está instalado") from exc
    if res.returncode != 0:
        raise UploadError(f"falha ao cortar vídeo: {res.stderr.strip()[:300]}")

    return str(out)


# --- Retry com backoff (mimica o auto-retry do app) ----------------------------

def _session_request(
    session: Any,
    method: str,
    path: str,
    body: Any = None,
) -> tuple[int, str, dict[str, str]]:
    """Usa headers de resposta quando a sessão oferece a API detalhada."""
    detailed = getattr(session, "request_detailed", None)
    if callable(detailed):
        response = detailed(method, path, body)
        if hasattr(response, "status"):
            return (
                int(response.status), str(response.text),
                {str(k): str(v) for k, v in dict(response.headers).items()},
            )
        if isinstance(response, tuple) and len(response) == 3:
            status, text, headers = response
            return int(status), str(text), {
                str(k): str(v) for k, v in dict(headers or {}).items()}
    status, text = session.request(method, path, body)
    return int(status), str(text), {}


def _header(headers: dict[str, str], name: str) -> str | None:
    wanted = name.casefold()
    return next((value for key, value in headers.items()
                 if key.casefold() == wanted), None)


def _error_detail(text: str) -> str:
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return text[:500]
    if isinstance(body, dict):
        for key in ("detail", "message", "error", "code"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value[:500]
            if isinstance(value, dict):
                for nested in ("detail", "message", "code", "reason"):
                    nested_value = value.get(nested)
                    if isinstance(nested_value, str) and nested_value:
                        return nested_value[:500]
    return text[:500]


def _http_upload_error(
    phase: str,
    status: int,
    text: str,
    headers: dict[str, str] | None = None,
) -> UploadError:
    response_headers = headers or {}
    blocked = _header(response_headers, "X-Blocked-Reason")
    # Create/SAS responses can contain signed URLs. These are temporary
    # credentials and must not become result.error or persisted journal text.
    private_response = phase in ("SAS", "POST /uploads")
    detail = ("o serviço não concluiu esta etapa"
              if private_response else _error_detail(text))
    message = f"{phase} falhou ({status}): {detail}"
    if blocked:
        displayed_reason = (blocked if not private_response or re.fullmatch(
            r"[a-z][a-z0-9_-]{0,63}", blocked) else "não especificado")
        message += f" [bloqueio: {displayed_reason}]"
    return UploadError(
        message,
        status_code=status,
        transient=(status == -1 or status in (408, 429) or status >= 500),
        blocked_reason=blocked,
        phase=phase,
    )


def _status_from_exception(exc: BaseException) -> int | None:
    raw = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    match = re.search(r"\((\d{3})\)", str(exc))
    return int(match.group(1)) if match else None

def _with_retry(
    fn: Callable[[], Any],
    *,
    max_retries: int = 3,
    retry_backoff: float = 1.5,
    retry_delay: float = 1.0,
    label: str = "etapa",
) -> tuple[Any, int]:
    """Executa `fn` com auto-retry em falhas transientes.

    O app nativo usa "auto-retry" para falhas de create/sas, transport e sidecar
    (strings de log: `[upload] transient ... -> auto-retry`). Aqui replicamos com
    backoff exponencial: 1s, 1.5s, 2.25s... Devemos exaurir os retries.
    Retorna (resultado, tentativas_usadas).
    Levanta a última UploadError depois de `max_retries` tentativas.
    """
    attempt = 0
    delay = retry_delay
    last: UploadError | None = None
    while attempt < max_retries:
        attempt += 1
        try:
            return fn(), attempt
        except UploadError as exc:
            last = exc
            if not exc.retryable or attempt >= max_retries:
                break
            time.sleep(delay * random.uniform(0.80, 1.20))
            delay *= retry_backoff
    assert last is not None
    # Preserve a contagem mesmo quando a atribuição do retorno de _with_retry
    # não acontece. Todos os caminhos de falha conseguem então registrar a
    # quantidade correta sem depender de uma variável local ainda inexistente.
    last.attempts = attempt
    raise last


# --- Etapa extra nativa: marcar upload como falho ------------------------------

def fail_upload(session: Any, upload_id: str, error_message: str) -> dict[str, Any]:
    """PATCH /api/v1/uploads/{id}/fail — marca o upload como falho no backend.

    O app nativo usa isso quando o upload falha após o registro (`_failUpload`).
    O backend guarda `error_message` (max 500 chars) no registro.
    Retorna o UploadOut parseado. Levanta UploadError se o backend recusar.
    """
    status, text = session.request(
        "PATCH", f"/api/v1/uploads/{upload_id}/fail",
        {"error_message": error_message[:500]},
    )
    if status != 200:
        raise UploadError(f"PATCH /fail falhou ({status}).", status_code=status,
                          transient=(status == -1 or status in (408, 429) or status >= 500), phase="fail")
    try:
        parsed = decode_json_state(text)
    except JsonStateError:
        raise UploadError("Resposta de sinalização de falha inválida; recibo preservado para revisão.",
                          status_code=status, transient=False, phase="fail", review_required=True) from None
    try:
        _validate_known_receipt(parsed, upload_id, "fail")
    except UploadError as exc:
        exc.status_code = status
        raise
    return parsed


def delete_upload(session: Any, upload_id: str) -> bool:
    """DELETE /api/v1/uploads/{upload_id} — remove o upload do backend.

    Usado pela política perfect-only: se o evaluate reprovar, o registro de
    upload é apagado (rows + referência). Sucesso = 204 No Content.
    Retorna True se deletado.
    """
    status, text = session.request("DELETE", f"/api/v1/uploads/{upload_id}")
    if status == 204:
        return True
    raise UploadError(f"DELETE do upload falhou ({status}).", status_code=status,
                      transient=(status == -1 or status in (408, 429) or status >= 500), phase="delete-upload")


def delete_session(session: Any, org_key: str, session_id: str) -> bool:
    """DELETE /api/v1/organizations/{org}/sessions/{sid} — remove a sessão.

    Apaga a sessão de gravação (rows + blobs do Azure) para o caller.
    Sucesso = 204 No Content. Retorna True se deletada.
    """
    status, text = session.request(
        "DELETE", f"/api/v1/organizations/{org_key}/sessions/{session_id}",
    )
    if status == 204:
        return True
    raise UploadError(
        f"DELETE da sessão falhou ({status}).", status_code=status,
        transient=(status == -1 or status in (408, 429) or status >= 500), phase="delete-session")


def _validate_known_receipt(data: dict[str, Any], upload_id: str, phase: str) -> None:
    """An optional legacy ID must still refer to the requested receipt.

    Empty legacy acknowledgements remain supported. This does not implement
    the complete native UploadOut schema or prove remote acceptance.
    """
    if any(data[key] != upload_id or not isinstance(data[key], str)
           for key in ("id", "uploadId", "upload_id") if key in data):
        raise UploadError("Resposta referente a outro recibo ou com identidade inválida; "
                          "envio preservado para revisão.", transient=False,
                          phase=phase, review_required=True)


def _validate_native_upload_out(data: dict[str, Any], phase: str) -> None:
    """UploadOut as declared in APK 1.28; status is an unrestricted string.

    Preserve finite extra metadata for review. blob_path belongs to meta and,
    when present, must be a string; an absent field differs from JSON null.
    A structurally valid response alone does not prove physical capture quality.
    """
    meta = data.get("meta")
    if (not isinstance(data.get("id"), str)
            or not isinstance(data.get("status"), str)
            or not isinstance(meta, dict)
            or ("blob_path" in meta and not isinstance(meta["blob_path"], str))):
        raise UploadError("Resposta do upload incompleta ou com tipos inválidos; "
                          "recibo preservado para revisão.", transient=False,
                          phase=phase, review_required=True)


def get_upload(session: Any, upload_id: str) -> dict[str, Any]:
    """GET /api/v1/uploads/{upload_id} — consulta o status do upload.

    O app nativo consulta o registro para saber o estado (`upload_status`).
    Devolve o dict com campos camelCase (uploadId, sessionId, logId, status,
    durationMs, recordedAt, ...). Levanta UploadError se não achar (404).
    """
    status, text = session.request("GET", f"/api/v1/uploads/{upload_id}")
    if status != 200:
        raise _http_upload_error("GET /uploads", status, text, {})
    try:
        parsed = decode_json_state(text)
    except JsonStateError:
        raise UploadError("Resposta de consulta do upload inválida; envio preservado para revisão.",
                          transient=False, phase="query", review_required=True) from None
    _validate_known_receipt(parsed, upload_id, "query")
    return parsed


def _require_completion_flags(suppress_per_chunk_catbear: bool,
                              session_complete: bool = False) -> None:
    if type(suppress_per_chunk_catbear) is not bool or type(session_complete) is not bool:
        raise UploadError("Política de conclusão inválida; use valores booleanos.",
                          transient=False, phase="preflight")


def _journal_flag(journal: dict[str, Any], key: str) -> bool:
    """Absence is the legacy False policy; never coerce an existing value."""
    value = journal.get(key, False)
    if type(value) is not bool:
        raise UploadError("Política do registro de envio inválida; preserve o arquivo para revisão.",
                          transient=False, phase="recovery")
    return value


def _journal_recorded_at(journal: dict[str, Any]) -> str:
    value = journal.get("recorded_at")
    if not isinstance(value, str) or not value.strip() or recorded_at_to_wall_ms(value) is None:
        raise UploadError("Horário original do envio ausente ou inválido; preserve o arquivo para revisão.",
                          transient=False, phase="recovery")
    return value


def _journal_has_remote_receipt(journal: dict[str, Any]) -> bool:
    upload_id = journal.get("upload_id", "")
    if upload_id is not None and not isinstance(upload_id, str):
        raise UploadError("Recibo de envio inválido; preserve o arquivo para revisão.",
                          transient=False, phase="recovery")
    return (bool(upload_id and upload_id.strip()) or journal.get("state") == STATE_DONE
            or journal.get("phase") == "done" or journal.get("finalized") is True)


def _existing_receipt_error() -> UploadError:
    return UploadError("Este envio já possui um recibo ou estado de conclusão; use Retomada "
                       "para as etapas compatíveis ou preserve o registro para revisão. "
                       "Um novo envio não substituirá o recibo existente.",
                       transient=False, phase="recovery")


def _journal_creation_uncertain(journal: dict[str, Any]) -> bool:
    """No receipt does not prove that a previously attempted CREATE failed.

    Legacy queued journals precede any remote creation. Other legacy phases
    lack a durable request marker, so cannot authorize another CREATE safely.
    Known receipts retain their existing completion/finalization handling.
    """
    upload_id = journal.get("upload_id")
    if isinstance(upload_id, str) and upload_id.strip():
        return False
    if journal.get("phase") in {"create_in_flight", "registered"}:
        return True
    if "create_attempted" in journal:
        return journal["create_attempted"] is not False
    return journal.get("phase") != "queued"


def _uncertain_creation_error() -> UploadError:
    return UploadError("A criação anterior pode ter sido aceita sem um recibo local. "
                       "O envio foi preservado para revisão; uma nova criação foi bloqueada.",
                       transient=False, phase="recovery")


def _upload_owner(session: Any, profile: DeviceProfile | None) -> str:
    """Use the session's declared owner, never infer auth from device metadata."""
    value = getattr(session, "email", None)
    owner = value.strip().casefold() if isinstance(value, str) else ""
    profile_value = getattr(profile, "email", None) if profile is not None else None
    profile_owner = (profile_value.strip().casefold()
                     if isinstance(profile_value, str) else "")
    if owner and profile_owner and owner != profile_owner:
        raise UploadError("O perfil de dispositivo difere da conta da sessão; o envio foi bloqueado.",
                          transient=False, phase="preflight")
    return owner


def _validate_journal_owner(journal: dict[str, Any], session_owner: str) -> None:
    value = journal.get("account_email")
    if value is None:
        return  # Legacy unknown ownership is preserved, not retrospectively inferred.
    if not isinstance(value, str):
        raise UploadError("Dono do registro de envio inválido; preserve o arquivo para revisão.",
                          transient=False, phase="recovery")
    owner = value.strip().casefold()
    if owner and (not session_owner or owner != session_owner):
        raise UploadError("A conta da sessão não corresponde ao dono do registro de envio; "
                          "o arquivo foi preservado para revisão.",
                          transient=False, phase="recovery")


def complete_upload(
    session: Any,
    upload_id: str,
    size_bytes: int,
    *,
    suppress_per_chunk_catbear: bool = True,
    session_complete: bool = False,
    network_type: str = "wifi",
    _native_response_schema: bool = False,
) -> dict[str, Any]:
    """Confirma um blob já enviado; operação reutilizável após reinício.

    No APK 1.29.0, ``_completeUpload`` envia sempre ``suppress_per_chunk_catbear``
    e ``network_type`` (wifi|cellular); ``session_complete`` só no último chunk.
    O QMoney usa ``complete -> finalize`` em etapas separadas. Sem prova de
    aceitação remota pelo Catbear.
    """
    _require_completion_flags(suppress_per_chunk_catbear, session_complete)
    if type(_native_response_schema) is not bool:
        raise UploadError("Contrato de resposta inválido; use um valor booleano.",
                          transient=False, phase="preflight")
    body: dict[str, Any] = {"size_bytes": int(size_bytes)}
    # Helper Android 1.29.0 (decompiled L357749–357760).
    if suppress_per_chunk_catbear or session_complete:
        body["suppress_per_chunk_catbear"] = True
    if network_type:
        body["network_type"] = str(network_type)
    if session_complete:
        body["session_complete"] = True
    status, text, response_headers = _session_request(
        session, "PATCH", f"/api/v1/uploads/{upload_id}/complete", body)
    if status == 409:
        try:
            current = get_upload(session, upload_id)
        except UploadError as exc:
            # A failed/ambiguous GET cannot prove that the conflicting Complete
            # failed. Retry only a transient lookup; otherwise keep the receipt
            # for review, without PATCH /fail or repeating transport.
            raise UploadError("Confirmação conflitante sem consulta confiável; "
                              "envio preservado para revisão.", status_code=exc.status_code,
                              transient=exc.retryable, phase="complete",
                              review_required=not exc.retryable) from None
        current_status = str(
            current.get("status") or current.get("upload_status") or ""
        ).casefold()
        if current_status in {"completed", "complete", "done"}:
            if _native_response_schema:
                _validate_native_upload_out(current, "complete")
            return current
    if status not in (200, 204):
        raise _http_upload_error(
            "PATCH /complete", status, text, response_headers)
    try:
        parsed = decode_json_state(text) if text.strip() else {}
    except JsonStateError:
        raise UploadError("Resposta de conclusão do upload inválida; recibo preservado para revisão.",
                          transient=False, phase="complete", review_required=True) from None
    _validate_known_receipt(parsed, upload_id, "complete")
    if _native_response_schema:
        _validate_native_upload_out(parsed, "complete")
    return parsed


# --- Sidecar persistente + fila (mimica recording.saveBody / pumpUploads) ------

def sidecars_dir() -> Path:
    """Retomadas pertencem à instalação, não à biblioteca compartilhável."""
    from .upload_storage import journal_directory
    return journal_directory()


MAX_CRASH_RESUMES = 3


def _sidecar_filename(session_id: str, chunk_index: int = 0) -> str:
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", session_id) or type(chunk_index) is not int or chunk_index < 0:
        raise UploadError("identificador de retomada inválido")
    suffix = "" if chunk_index == 0 else f"__{chunk_index}"
    return f"{session_id}{suffix}.json"


def _sidecar_path(session_id: str, chunk_index: int = 0) -> Path:
    filename = _sidecar_filename(session_id, chunk_index)
    return sidecars_dir() / filename


def _sidecar_archive_path(session_id: str, chunk_index: int = 0) -> Path:
    return _sidecar_path(session_id, chunk_index).with_suffix(".data.zip")


def _remove_sidecar_archive(session_id: str, chunk_index: int = 0) -> None:
    """Remove somente o ZIP temporário depois da entrega confirmada."""
    try:
        _sidecar_archive_path(session_id, chunk_index).unlink(missing_ok=True)
    except OSError:
        pass


def _sidecar_resume_payload(item: dict[str, Any]) -> bytes:
    """Bind the original archive to a known uploaded receipt before effects.

    This is an integrity/identity check, not proof of physical sensor origin.
    Missing historical bindings require review rather than generating new data.
    """
    error = UploadError("ZIP original ou vínculo de retomada inválido; preserve os arquivos para revisão.",
                        transient=False, phase="recovery")
    sid, index = item.get("session_id"), item.get("chunk_index", 0)
    _sidecar_filename(sid, index)
    _response_upload_id({"id": item.get("upload_id")})
    _journal_recorded_at(item)
    size, duration = item.get("size_bytes"), item.get("duration_ms")
    archive_size, archive_hash = item.get("sidecar_size_bytes"), item.get("sidecar_sha256")
    owner, org = item.get("account_email"), item.get("org_key")
    if (item.get("transport_artifact") != "sidecar"
            or item.get("conflict_action") != "complete"
            or _journal_flag(item, "register_first") is not True
            or type(size) is not int or size <= 0
            or type(duration) is not int or duration <= 0
            or type(archive_size) is not int or archive_size <= 0
            or not isinstance(archive_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", archive_hash)
            or not isinstance(owner, str) or not owner.strip()
            or not isinstance(org, str) or not org.strip()
            or item.get("log_id") != f"{sid}_{index}"
            or item.get("filename") != f"{sid}_{index}.mp4"):
        raise error
    name = item.get("sidecar_data_path")
    expected = _sidecar_archive_path(sid, index).resolve()
    try:
        if not isinstance(name, str) or Path(name).resolve() != expected:
            raise error
        payload = expected.read_bytes()
    except (OSError, ValueError, RuntimeError):
        raise error from None
    if len(payload) != archive_size or hashlib.sha256(payload).hexdigest() != archive_hash:
        raise error
    _validate_sidecar_zip(payload, log_id=f"{sid}_{index}", duration_ms=duration)
    return payload


def save_sidecar(sidecar: dict[str, Any]) -> Path:
    """Persiste o estado de um upload em `data/sidecars/<session_id>.json`."""
    if not isinstance(sidecar, dict):
        raise UploadError("identificador de retomada inválido")
    candidate = dict(sidecar)
    sid = candidate.get("session_id", candidate.get("sessionId"))
    if "sessionId" in candidate and candidate["sessionId"] != sid:
        raise UploadError("identificador de retomada inválido")
    # Missing indices belong to the legacy zero-chunk format. Present values
    # must retain their exact type: coercion can replace a different journal.
    chunk_index = candidate.get("chunk_index", 0)
    try:
        json.dumps(candidate, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise UploadError("O novo estado de envio não é um objeto JSON válido; o arquivo anterior foi preservado.",
                          transient=False, phase="recovery") from None
    path = _sidecar_path(sid, chunk_index)
    from .operation_lease import operation_lease, OperationLeaseError
    from .media_lifecycle import media_state_lease
    try:
        with media_state_lease(wait=True), operation_lease(path.with_suffix('.write.lock')):
            previous = load_sidecar(sid, chunk_index)
            if previous is not None and (any(
                    previous.get(key) is not None and previous.get(key) != candidate.get(key)
                    for key in ("account_email", "org_key", "task_id", "expected_chunk_count", "campaign_context"))
                    or (previous.get('create_attempted') is True and candidate.get('create_attempted') is False)
                    or (isinstance(previous.get('upload_id'),str) and previous['upload_id'].strip()
                        and previous['upload_id'] != candidate.get('upload_id'))):
                raise UploadError("A identidade do registro de envio existente não corresponde ao novo estado; preserve o arquivo para revisão.",
                                  transient=False, phase="recovery")
            candidate["session_id"] = sid
            save_json(path, candidate)
    except OperationLeaseError:
        raise UploadError('O registro de envio já está em uso; preserve a retomada.',transient=True,phase='recovery') from None
    return path


def _read_sidecar_file(path: Path) -> dict[str, Any] | None:
    """One authoritative decoder and filename contract for every journal reader."""
    data = load_json_state(path, None)
    if data is None:
        return None
    if (not isinstance(data, dict)
            or _sidecar_filename(data.get("session_id"), data.get("chunk_index", 0)) != path.name):
        raise ValueError("invalid journal identity")
    return data


def load_sidecar(session_id: str, chunk_index: int = 0) -> dict[str, Any] | None:
    """Lê um recibo; somente a ausência real retorna None."""
    path = _sidecar_path(session_id, chunk_index)
    try:
        data = _read_sidecar_file(path)
        if data is None:
            return None
        if (not isinstance(data, dict)
                or _sidecar_filename(data.get("session_id"), data.get("chunk_index", 0)) != path.name
                or data.get("session_id") != session_id or data.get("chunk_index", 0) != chunk_index):
            raise ValueError("invalid journal identity")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError, UploadError):
        raise UploadError("Não foi possível validar o registro de envio; preserve o arquivo para revisão.",
                          transient=False, phase="recovery") from None
    return data


def list_sidecars(state: str | None = None) -> list[dict[str, Any]]:
    """Valida todos os journals antes de retornar o filtro de estado."""
    from .media_lifecycle import media_state_lease
    out: list[dict[str, Any]] = []
    try:
        # Read one coherent generation while writers/cleanup hold the same
        # short local barrier. No network work occurs under this lease.
        with media_state_lease(wait=True):
            # iterdir reports directory I/O failures; glob can silently omit them.
            paths = sorted(path for path in sidecars_dir().iterdir() if path.name.lower().endswith(".json"))
            for path in paths:
                data = _read_sidecar_file(path)
                if not isinstance(data, dict):
                    raise ValueError("invalid journal format")
                expected_name = _sidecar_filename(data.get("session_id"), data.get("chunk_index", 0))
                if path.name != expected_name:
                    raise ValueError("invalid journal identity")
                if state is None or data.get("state") == state:
                    out.append(data)
    except (OSError, UnicodeError, ValueError, UploadError):
        # Corrupt or unknown receipts must never become proof of no prior upload.
        # Keep diagnostics independent of filenames, payloads and parser errors.
        raise UploadError("Não foi possível validar todos os registros de envio; preserve os arquivos para revisão.",
                          transient=False, phase="recovery") from None
    return out


def _csv_span_ns(
    payload: bytes,
    timestamp_column: str,
    required_columns: tuple[str, ...],
) -> tuple[int, int, int]:
    """Valida um CSV temporal e devolve primeira/última marca e nº de amostras."""
    try:
        lines = payload.decode("utf-8-sig").splitlines()
    except UnicodeError as exc:
        raise UploadError("sidecar contém CSV fora de UTF-8", transient=False) from exc
    if len(lines) < 3:
        raise UploadError("sidecar contém CSV sem amostras suficientes", transient=False)
    header = [value.strip() for value in lines[0].split(",")]
    if len(set(header)) != len(header):
        raise UploadError("sidecar CSV com colunas duplicadas", transient=False)
    missing = [column for column in required_columns if column not in header]
    if missing:
        raise UploadError(
            "sidecar CSV sem coluna(s): " + ", ".join(missing),
            transient=False,
        )
    index = header.index(timestamp_column)
    try:
        stamps = []
        for line in lines[1:]:
            cells = line.split(",")
            if len(cells) != len(header) or not re.fullmatch(r"[0-9]+", cells[index]):
                raise ValueError
            stamp = int(cells[index])
            if stamp > (1 << 63) - 1 or (stamps and stamp <= stamps[-1]):
                raise ValueError
            stamps.append(stamp)
        first, last = stamps[0], stamps[-1]
    except (IndexError, TypeError, ValueError) as exc:
        raise UploadError("sidecar CSV com timestamp inválido", transient=False) from exc
    if last <= first:
        raise UploadError("sidecar CSV sem relógio crescente", transient=False)
    return first, last, len(lines) - 1


def _validate_sidecar_zip(
    payload: bytes,
    *,
    log_id: str,
    duration_ms: int,
) -> dict[str, Any]:
    """Falha antes da rede se ZIP, metadata, IMU e frames forem incoerentes."""
    if not payload:
        raise UploadError("sidecar obrigatório está vazio", transient=False)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            names = {info.filename for info in infos}
            required = {
                f"{log_id}.metadata.json",
                f"{log_id}.imu.csv",
                f"{log_id}.frames.csv",
            }
            if not required.issubset(names):
                missing = ", ".join(sorted(required - names))
                raise UploadError(
                    f"sidecar incompleto; faltando: {missing}", transient=False)
            if len(names) != len(infos):
                raise UploadError(
                    "sidecar contém membros duplicados", transient=False)
            if any("/" in name or "\\" in name or name.startswith(".")
                   for name in names):
                raise UploadError(
                    "sidecar contém caminhos inesperados", transient=False)
            if sum(info.file_size for info in infos) > 512 * 1024 * 1024:
                raise UploadError("sidecar descompactado excede 512 MiB", transient=False)
            metadata = decode_json_state(archive.read(f"{log_id}.metadata.json"))
            imu = archive.read(f"{log_id}.imu.csv")
            frames = archive.read(f"{log_id}.frames.csv")
    except UploadError:
        raise
    except JsonStateError:
        raise UploadError(
            "metadata.json do sidecar inválido ou ambíguo; arquivo preservado",
            transient=False) from None
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeError, RuntimeError, json.JSONDecodeError) as exc:
        raise UploadError(f"sidecar inválido: {exc}", transient=False) from exc
    if not isinstance(metadata, dict):
        raise UploadError("metadata.json do sidecar não é objeto", transient=False)
    if str(metadata.get("logId") or metadata.get("id") or "") != log_id:
        raise UploadError("sidecar pertence a outro log_id", transient=False)
    declared = metadata.get("durationMs")
    if type(declared) is not int or declared <= 0:
        raise UploadError(
            "durationMs do sidecar deve ser inteiro positivo", transient=False)
    tolerance_ms = max(500, int(duration_ms * 0.01))
    if abs(declared - duration_ms) > tolerance_ms:
        raise UploadError(
            f"duração do sidecar diverge do vídeo ({declared} vs {duration_ms} ms)",
            transient=False,
        )
    imu_start, imu_end, _imu_count = _csv_span_ns(
        imu, "t", ("t", "ax", "ay", "az", "wx", "wy", "wz"))
    frame_start, frame_end, _frame_count = _csv_span_ns(
        frames, "ptsNs", ("i", "ptsNs", "dtNs", "tNs", "key"))
    expected_ns = duration_ms * 1_000_000
    tolerance_ns = max(500_000_000, int(expected_ns * 0.02))
    for kind, actual in (
        ("IMU", imu_end - imu_start),
        ("frames", frame_end - frame_start),
    ):
        if abs(actual - expected_ns) > tolerance_ns:
            raise UploadError(
                f"janela de {kind} diverge do vídeo "
                f"({actual / 1e9:.3f}s vs {duration_ms / 1000:.3f}s)",
                transient=False,
            )
    # Checklist local equivalente ao evaluate (monotonicidade, keyframes,
    # schema do metadata, duração) — QA antes da rede.
    from .validate import summarize, validate_sidecar_zip
    _summary = summarize(
        validate_sidecar_zip(payload, log_id=log_id, duration_ms=duration_ms))
    if _summary["counts"].get("fail", 0):
        details = "; ".join(
            f"{item['id']}: {item['detail']}"
            for item in _summary["failures"])
        raise UploadError(
            f"sidecar reprovado no checklist local: {details}",
            transient=False,
        )
    return metadata


def _chunk_sidecar(chunk: ChunkResult, session_id: str, org_key: str,
                   task_id: str | None, recorded_at: str, filename: str,
                   state: str, attempts: int, error: str | None) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "org_key": org_key,
        "task_id": task_id,
        "chunk_index": chunk.chunk_index,
        "log_id": chunk.log_id,
        "filename": filename,
        "blob_path": chunk.blob_path,
        "sidecar_blob_path": chunk.sidecar_blob_path,
        "upload_id": chunk.upload_id,
        "size_bytes": chunk.size_bytes,
        "duration_ms": chunk.duration_ms,
        "recorded_at": recorded_at,
        "state": state,
        "attempts": attempts,
        "error": error,
        "updated_at": _iso_now(),
    }


def _response_upload_id(data: Any, *, status: int | None = None) -> str:
    """Validate an ID without coercing an invalid response into a URL segment."""
    error = UploadError(
        "Resposta de registro do upload inválida ou sem identificador válido.",
        status_code=status, transient=False, phase="create",
    )
    if not isinstance(data, dict):
        raise error
    values = [data[key] for key in ("id", "uploadId", "upload_id")
              if key in data and data[key] is not None]
    if (not values or any(not isinstance(value, str) or not value
                         or value != value.strip() or value in (".", "..")
                         or any(char.isspace() or ord(char) < 32 or ord(char) == 127
                         or char in "/\\?#%" for char in value)
                         for value in values)
            or any(value != values[0] for value in values[1:])):
        raise error
    return values[0]


def _validated_sas_urls(data: Any, filenames: list[str], *,
                        _native_response_schema: bool = False) -> tuple[dict[str, str], dict[str, str]]:
    """Require all requested artifacts before starting either PUT operation."""
    error = UploadError(
        "Resposta SAS inválida ou incompleta para os arquivos solicitados.",
        status_code=200, transient=False, phase="sas",
    )
    if not isinstance(data, dict):
        raise error
    entries = data.get("signed_urls")
    if not isinstance(entries, list) or not entries:
        raise error
    expected = set(filenames)
    urls: dict[str, str] = {}
    paths: dict[str, str] = {}
    resources: set[tuple[str, str, int, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise error
        filename, url = entry.get("filename"), entry.get("blob_url")
        # APK 1.28 declares expires_at as a required z.string, without a date
        # refinement. Do not infer temporal validity from this structural gate.
        if _native_response_schema and not isinstance(entry.get("expires_at"), str):
            raise error
        if (not isinstance(filename, str) or filename not in expected or filename in urls
                or not isinstance(url, str) or not url or "\\" in url
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)):
            raise error
        try:
            parsed = urllib.parse.urlsplit(url)
            valid = (parsed.scheme == "https" and parsed.hostname
                     and parsed.path and parsed.path != "/"
                     and parsed.username is None and parsed.password is None
                     and not parsed.fragment and parsed.port != 0)
        except ValueError:
            raise error from None
        if not valid:
            raise error
        resource = (parsed.scheme, parsed.hostname, parsed.port or 443, parsed.path)
        if resource in resources:
            raise error
        resources.add(resource)
        urls[filename] = url
        paths[filename] = parsed.path.lstrip("/")
    if set(urls) != expected:
        raise error
    return urls, paths


def _conflict_upload(text: str) -> tuple[dict[str, Any], str]:
    """Read the canonical conflict contract, keeping driver decisions private.

    APK 1.28 requires root upload_id/status and optional string detail. Finite,
    unambiguous JSON and safe, consistent ID aliases are stronger local guards.
    A failed receipt remains an identity to review, never a delivery receipt.
    """
    try:
        body = decode_json_state(text)
    except JsonStateError:
        raise UploadError(
            "POST /uploads conflitante sem JSON válido.",
            status_code=409, transient=False, phase="create",
        ) from None

    status = body.get("status")
    valid_shape = (
        isinstance(body.get("upload_id"), str)
        and isinstance(status, str) and status in ("initiated", "uploaded", "failed")
        and ("detail" not in body or isinstance(body["detail"], str))
    )
    if not valid_shape:
        # Native safeParse succeeds before consulting this exact fallback.
        if body.get("detail") == "Session has been deleted":
            raise UploadError(
                "POST /uploads: a sessão foi removida pelo servidor",
                status_code=409, transient=False, phase="create",
            )
        raise UploadError(
            "POST /uploads conflitante com resposta inválida; preserve o registro para revisão.",
            status_code=409, transient=False, phase="create",
        )

    upload_id = _response_upload_id(body, status=409)
    action = {"initiated": "reuse-and-upload", "uploaded": "complete", "failed": "dead-end"}[status]
    # Unknown fields cannot inject a transition or supply an alternative ID.
    return {"id": upload_id, "status": status}, action


# --- Fluxo de um chunk individual (etapas 1-4) --------------------------------

def _upload_single_chunk(
    session: Any,
    video_path: Path,
    org_key: str,
    session_id: str,
    chunk_index: int,
    task_id: str | None,
    content_type: str,
    timeout_blob: int,
    recorded_at: str,
    device_meta: dict[str, Any] | None,
    platform_meta: dict[str, Any] | None,
    video_meta: dict[str, Any] | None,
    network_meta: dict[str, Any] | None,
    max_retries: int = 3,
    retry_backoff: float = 1.5,
    suppress_per_chunk_catbear: bool = True,
    session_complete: bool = False,
    fail_on_error: bool = True,
    sidecar: bool = True,
    sidecar_data: bytes | None = None,
    ego_meta: dict[str, Any] | None = None,
    register_first: bool = True,
    on_progress: Callable[..., None] | None = None,
    profile: DeviceProfile | None = None,
    checkpoint: Callable[..., None] | None = None,
    _resume_sidecar_row: dict[str, Any] | None = None,
    _native_response_schema: bool = True,
    _original_capture: CaptureDescriptor | None = None,
    _expected_video_sha256: str | None = None,
) -> ChunkResult:
    """Executa registro -> SAS -> PUT Blob -> PATCH /complete.

    Novos envios usam a ordem observada em driveCreate do APK 1.29.0:
    POST /uploads -> SAS -> transporte -> complete. False é preservado para
    retomadas explicitamente identificadas com a ordem legada.

    Espelha o app nativo:
      - auto-retry com backoff nas etapas transientes (create/sas, transport);
      - PATCH complete com supressão True nos novos envios; False explícito
        mantém a omissão legada. `session_complete` só é enviado quando esse
        parâmetro é True; não há equivalência demonstrada entre o desktop e
        os estados Android accepted && saveGated (U04 permanece aberto);
      - se o registro foi criado e a confirmação falhar, marca `PATCH /fail`
        (fail_on_error) — comportamento `_failUpload` do app;
      - se o arquivo local sumir, devolve um ChunkResult em estado `loss`
        (loss record) em vez de abortar a sessão.
      - (sidecar=True) gera e sobe o `.data.zip` nativo junto com o MP4
        (imu.csv, frames.csv, metadata.json) e envia meta ego
        (source/timebase/cameras/codecActuals) — requisito do evaluate.

    Retorna o ChunkResult com os IDs e metadados. Política de ordem não booleana
    conserva o contrato de retorno STATE_FAILED e comunica somente o diagnóstico
    ao callback checkpoint, antes de probe/HTTP. Esse callback é código do
    chamador e pode produzir efeitos locais. Flags de conclusão não booleanas
    levantam UploadError antes de qualquer callback.
    """
    _require_completion_flags(suppress_per_chunk_catbear, session_complete)
    if _expected_video_sha256 is not None:
        _verify_video_content_digest(video_path, _expected_video_sha256)
    if type(_native_response_schema) is not bool:
        raise UploadError("Contrato de resposta inválido; use um valor booleano.",
                          transient=False, phase="preflight")
    resume_row = _resume_sidecar_row
    if resume_row is not None:
        _native_response_schema = _journal_flag(resume_row, "native_response_schema")
        _validate_journal_owner(resume_row, _upload_owner(session, profile))
        if resume_row.get("video_content_sha256") is not None:
            persisted_digest = resume_row["video_content_sha256"]
            if _expected_video_sha256 is not None and persisted_digest != _expected_video_sha256:
                raise UploadError("O hash do conteúdo difere do registro; preserve os recibos para revisão.",
                                  transient=False, phase="preflight", review_required=True)
            _expected_video_sha256 = persisted_digest
            _verify_video_content_digest(video_path, _expected_video_sha256)
        sidecar_data = _sidecar_resume_payload(resume_row)
        if (resume_row.get("session_id") != session_id
                or resume_row.get("chunk_index", 0) != chunk_index
                or resume_row.get("org_key") != org_key
                or resume_row.get("task_id") != task_id
                or resume_row.get("recorded_at") != recorded_at
                or register_first is not True or sidecar is not True):
            raise _existing_receipt_error()
    if type(register_first) is not bool:
        error = "Política de ordem do upload inválida; use um valor booleano."
        if checkpoint:
            checkpoint(state=STATE_FAILED, phase="preflight", error=error)
        return ChunkResult(upload_id="", chunk_index=chunk_index,
                           log_id=f"{session_id}_{chunk_index}", blob_path="",
                           size_bytes=-1, duration_ms=0, state=STATE_FAILED, error=error)
    _limits = config.recording_limits()
    file_size = (resume_row["size_bytes"] if resume_row is not None
                 else video_path.stat().st_size if video_path.exists() else -1)
    # Padrão nativo: logId = "{sessionId}_{chunk_index}".
    log_id = f"{session_id}_{chunk_index}"
    # Padrão nativo observado no SAS real: filename = "{logId}.mp4" (SEM _preview).
    # Com esse nome o servidor gera blob_path = .../{logId}/{logId}.mp4, que é
    # exatamente o caminho que o evaluate procura (video.ffprobe_ok).
    file_name = f"{log_id}.mp4"
    sidecar_file_name = f"{log_id}.data.zip"
    duration_ms = (resume_row["duration_ms"] if resume_row is not None
                   else _original_capture.metadata["durationMs"] if _original_capture is not None
                   else _probe_duration_ms(video_path) if video_path.exists() else 0)

    def _emit(phase: str, state: str, attempt: int, **details: Any) -> None:
        if on_progress:
            on_progress(phase, state, attempt, **details)

    def _checkpoint(state: str, phase: str, **details: Any) -> None:
        if checkpoint:
            checkpoint(state=state, phase=phase, **details)

    _checkpoint(
        STATE_TRANSPORT if resume_row is not None else STATE_CREATING,
        "sidecar_preflight" if resume_row is not None else "preflight",
        local_video_path=str(video_path.resolve()),
        size_bytes=file_size, duration_ms=duration_ms,
    )
    if resume_row is None and not video_path.exists():
        return ChunkResult(
            upload_id="", chunk_index=chunk_index, log_id=log_id, blob_path="",
            size_bytes=-1, duration_ms=0, state=STATE_LOSS,
            error="arquivo local ausente antes do preflight",
        )
    if (file_size <= 0
            or not _limits["min_duration_ms"] <= duration_ms <= _limits["max_duration_ms"]):
        return ChunkResult(
            upload_id="", chunk_index=chunk_index, log_id=log_id, blob_path="",
            size_bytes=file_size, duration_ms=duration_ms, state=STATE_FAILED,
            error=(
                "preflight recusou vídeo vazio ou fora da duração permitida "
                f"({duration_ms} ms; esperado {_limits['min_duration_ms']}–"
                f"{_limits['max_duration_ms']} ms)"),
        )

    # --- 0. sidecar .data.zip (nativo) ------------------------------------
    sidecar_bytes: bytes | None = None
    sidecar_metadata: dict[str, Any] | None = None
    if sidecar and (resume_row is not None or video_path.exists()):
        try:
            if sidecar_data is not None:
                sidecar_bytes = sidecar_data
            else:
                probe = probe_video(video_path)
                # Sem device_meta/platform_meta: o metadata.json do sidecar usa
                # o device COMPLETO Android (Build.MODEL + systemName +
                # systemVersion; platform {os,version} no 1.29) — o que o catbear espera.
                # Com `profile` (anti-colusão): intrinsics Brown-Conrady e
                # elapsedRealtimeNanos DO APARELHO DA CONTA — cada upload tem
                # identidade de sensor própria.
                sidecar_kwargs: dict[str, Any] = {}
                if profile is not None:
                    wall_ms = recorded_at_to_wall_ms(recorded_at)
                    sidecar_kwargs = {
                        "calib": profile.calib,
                        "frames_gop": profile.frames_gop,
                        "uptime_ns": (profile.uptime_ns_at(wall_ms)
                                      if wall_ms else None),
                        # IMU própria por conta (sem ela todas sobem a MESMA IMU)
                        "imu_seed": profile.device_id,
                    }
                    # device COMPLETO do perfil (Build.MODEL Samsung da conta)
                    sidecar_kwargs["device_meta"] = profile.sidecar_device_meta()
                    sidecar_kwargs["platform_meta"] = profile.sidecar_platform_meta()
                sidecar_bytes = build_sidecar_zip(
                    session_id=session_id,
                    chunk_index=chunk_index,
                    duration_ms=duration_ms,
                    recorded_at=recorded_at,
                    video_probe=probe,
                    # frames.csv derivado do MP4 real (PTS/keyframes) como o
                    # EgoSidecar lê com MediaExtractor — em QUALQUER upload.
                    video_path=video_path,
                    **sidecar_kwargs,
                )
            sidecar_metadata = _validate_sidecar_zip(
                sidecar_bytes, log_id=log_id, duration_ms=duration_ms)
        except Exception as exc:  # noqa: BLE001 — convertido em falha fechada
            error = exc if isinstance(exc, UploadError) else UploadError(
                f"falha ao criar sidecar obrigatório: {exc}", transient=False)
            _emit("sidecar", STATE_FAILED, 1, error=str(error))
            _checkpoint(STATE_FAILED, "sidecar", error=str(error))
            return ChunkResult(
                upload_id=resume_row["upload_id"] if resume_row is not None else "",
                chunk_index=chunk_index, log_id=log_id,
                blob_path="", size_bytes=file_size, duration_ms=duration_ms,
                state=STATE_FAILED, error=str(error),
            )
        _checkpoint(
            STATE_TRANSPORT if resume_row is not None else STATE_CREATING,
            "sidecar_preflight" if resume_row is not None else "sidecar_validated",
            sidecar_size_bytes=len(sidecar_bytes),
            sidecar_sha256=hashlib.sha256(sidecar_bytes).hexdigest(),
        )

    upload_id = resume_row["upload_id"] if resume_row is not None else ""
    create_data: dict[str, Any] = {"id": upload_id} if resume_row is not None else {}
    create_attempts = 1
    conflict_action = "complete" if resume_row is not None else ""
    transport_sidecar_only = resume_row is not None

    # --- 2. POST /api/v1/storage/sas/blobs --------------------------------
    sas_progress_attempt = 0

    def _sas() -> tuple[dict[str, Any], dict[str, str], str, str]:
        nonlocal sas_progress_attempt
        sas_progress_attempt += 1
        _emit("sas", STATE_TRANSPORT, sas_progress_attempt)
        files: list[dict[str, Any]] = [] if transport_sidecar_only else [
            {"filename": file_name, "content_type": content_type},
        ]
        if sidecar_bytes is not None:
            files.append({"filename": sidecar_file_name, "content_type": "application/zip"})
        sas_body = {
            "session_id": session_id,
            "files": files,
            "organization_resource_key": org_key,
        }
        status, text, response_headers = _session_request(
            session, "POST", "/api/v1/storage/sas/blobs", sas_body)
        if status != 200:
            raise _http_upload_error("SAS", status, text, response_headers)
        try:
            sas_data = decode_json_state(text)
        except JsonStateError:
            raise UploadError("Resposta SAS sem JSON válido.", status_code=status,
                              transient=False, phase="sas") from None
        urls_by_filename, paths_by_filename = _validated_sas_urls(
            sas_data, [item["filename"] for item in files],
            _native_response_schema=_native_response_schema and register_first)
        return sas_data, urls_by_filename, paths_by_filename.get(file_name, ""), \
            paths_by_filename.get(sidecar_file_name, "")

    sas_data: dict[str, Any] = {}
    urls_by_filename: dict[str, str] = {}
    blob_path = ""
    sidecar_blob_path = ""
    blob_url = ""
    sas_attempts = 1

    def _request_sas_stage() -> ChunkResult | None:
        nonlocal sas_data, urls_by_filename, blob_path, sidecar_blob_path
        nonlocal blob_url, sas_attempts
        try:
            sas_result, sas_attempts = _with_retry(
                _sas, max_retries=max_retries,
                retry_backoff=retry_backoff, label="SAS")
            sas_data, urls_by_filename, blob_path, sidecar_blob_path = sas_result
            blob_url = urls_by_filename.get(file_name, "")
        except UploadError as exc:
            sas_attempts = exc.attempts or sas_attempts
            failure_state = STATE_RETRY_LATE if exc.retryable else STATE_FAILED
            _checkpoint(
                failure_state, "sas", upload_id=upload_id,
                error=str(exc), attempts=sas_attempts,
            )
            # No fluxo nativo o registro já existe. Falha transiente preserva o
            # ID para a retomada reutilizar o mesmo registro; somente uma falha
            # permanente pode encerrá-lo no servidor.
            if upload_id and fail_on_error and not exc.retryable:
                try:
                    _emit("fail", STATE_FAILED, 1)
                    fail_upload(session, upload_id, f"SAS falhou: {exc}")
                except UploadError:
                    pass
            return ChunkResult(
                upload_id=str(upload_id), chunk_index=chunk_index, log_id=log_id,
                blob_path=blob_path, size_bytes=file_size,
                duration_ms=duration_ms, raw_create=create_data,
                state=failure_state, attempts=sas_attempts, error=str(exc),
                sidecar_blob_path=sidecar_blob_path,
                sidecar_size_bytes=len(sidecar_bytes) if sidecar_bytes else 0,
            )
        _checkpoint(
            STATE_TRANSPORT, "sas_ready", upload_id=upload_id,
            blob_path=blob_path, sidecar_blob_path=sidecar_blob_path,
            attempts=sas_attempts,
        )
        return None

    # Compatibilidade do modo legado. Campanhas usam register_first=True e
    # solicitam a SAS somente depois de o registro ser criado/reconciliado.
    if not register_first:
        sas_failure = _request_sas_stage()
        if sas_failure is not None:
            return sas_failure

    # --- 3. PUT no Azure Blob Storage (mp4 + sidecar) ---------------------
    if resume_row is None and not video_path.exists():
        # [upload] local file missing -> loss record
        loss = ChunkResult(
            upload_id="", chunk_index=chunk_index, log_id=log_id,
            blob_path=blob_path, size_bytes=-1, duration_ms=0,
            state=STATE_LOSS, attempts=1,
            error="arquivo local sumiu antes do transport (loss record)",
        )
        return loss

    if resume_row is None:
        file_size = video_path.stat().st_size
    transport_progress_attempt = 0

    def _transport() -> int:
        nonlocal transport_progress_attempt
        transport_progress_attempt += 1
        _emit("transport", STATE_TRANSPORT, transport_progress_attempt)

        def _transport_progress(sent: int, total: int, elapsed: float,
                                *, artifact: str | None = None) -> None:
            speed_bps = sent / elapsed if elapsed > 0 else 0.0
            remaining = max(0, total - sent)
            eta_s = remaining / speed_bps if speed_bps > 0 else None
            _emit(
                "transport", STATE_TRANSPORT, transport_progress_attempt,
                sent_bytes=sent, total_bytes=total,
                speed_bps=speed_bps, eta_s=eta_s,
                percent=(sent * 100.0 / total if total else 100.0),
                **({"artifact": artifact} if artifact else {}),
            )

        put_status = 201 if transport_sidecar_only else _put_blob_file(
            blob_url, video_path, content_type=content_type,
            timeout=timeout_blob, on_progress=_transport_progress,
            **({"expected_sha256": _original_capture.media.sha256}
               if _original_capture is not None else
               {"expected_sha256": _expected_video_sha256}
               if _expected_video_sha256 is not None else {}))
        if put_status != 201:
            raise _http_upload_error(
                "PUT Blob", int(put_status), "status inesperado")
        if sidecar_bytes is not None:
            zip_url = urls_by_filename.get(sidecar_file_name, "")
            if zip_url:
                _emit("transport", STATE_TRANSPORT, transport_progress_attempt,
                      artifact="sidecar")
                # O nativeUploadTransport (AzureBlockUploader) sobe QUALQUER
                # arquivo em blocos (mp4 e .data.zip) via OkHttp.
                zip_status = _put_blob(zip_url, sidecar_bytes,
                                       content_type="application/zip",
                                       timeout=timeout_blob,
                                       resumable=None,
                                       on_progress=lambda sent, total, elapsed: _transport_progress(
                                           sent, total, elapsed, artifact="sidecar"))
                if zip_status != 201:
                    raise _http_upload_error(
                        "PUT sidecar", int(zip_status), "status inesperado")
        return put_status

    sas_remints = 0
    sas_remint_error: UploadError | None = None

    def _transport_resilient() -> int:
        """Uma SAS expirada é renovada; outros 403 continuam permanentes."""
        nonlocal sas_data, urls_by_filename, blob_path, sidecar_blob_path
        nonlocal blob_url, sas_attempts, sas_remints, sas_remint_error
        # The old authorization was already refused. An exhausted renewal
        # cannot make those URLs usable; outer retries must retain its cause
        # rather than turn a transient SAS failure into permanent PUT /fail.
        if sas_remint_error is not None:
            raise sas_remint_error
        try:
            return _transport()
        except UploadError as exc:
            if exc.status_code not in (401, 403) or sas_remints >= 1:
                raise
            sas_remints += 1
            try:
                renewed, remint_attempts = _with_retry(
                    _sas, max_retries=max_retries,
                    retry_backoff=retry_backoff, label="SAS remint")
            except UploadError as renewal_error:
                sas_remint_error = renewal_error
                raise
            sas_data, urls_by_filename, blob_path, sidecar_blob_path = renewed
            blob_url = urls_by_filename.get(file_name, "")
            sas_attempts += remint_attempts
            _checkpoint(
                STATE_TRANSPORT, "sas_reminted", blob_path=blob_path,
                sidecar_blob_path=sidecar_blob_path,
            )
            return _transport()

    # --- 1. POST /api/v1/uploads -------------------------------------------
    # O app nativo monta o meta como: {...metadata.json, chunk_index, size_bytes,
    # source, ...deviceUploadMeta}. Ele NÃO envia blob_path, network,
    # app_version (usa appVersion) nem sidecar_blob_path no meta — replicar ISSO
    # (espalhar o metadata.json inteiro) é o que o catbear espera.
    def _register() -> None:
        nonlocal upload_id, create_data, create_attempts, conflict_action, transport_sidecar_only
        # O app nativo espalha o metadata.json do sidecar como meta do POST
        # /uploads. Se um .data.zip foi fornecido (sidecar_bytes), extrair o
        # metadata.json dele (fonte de verdade: imu/frames/metadata REAIS) —
        # replica o comportamento que gerou sessões "Ótimo". Caso contrário,
        # monta com build_metadata_json.
        meta: dict[str, Any] | None = (
            dict(sidecar_metadata) if sidecar_metadata is not None else None)
        if meta is None:
            probe = probe_video(video_path) if video_path.exists() else {}
            log_id_local = f"{session_id}_{chunk_index}"
            meta = build_metadata_json(
                session_id=session_id,
                chunk_index=chunk_index,
                duration_ms=duration_ms,
                recorded_at=recorded_at,
                video_probe=probe,
                device_meta=device_meta,
                platform_meta=platform_meta,
                log_id=log_id_local,
            )
        meta["chunk_index"] = chunk_index
        meta["size_bytes"] = file_size
        # O app nativo espalha o metadata.json do sidecar e DEPOIS sobrescreve
        # device/platform/appVersion com getDeviceUploadMeta() — formato curto
        # Android (DETALHAMENTO §2.2):
        #   device   = {"model": "SM-S901E"}   (só Build.MODEL)
        #   platform = {"os": "android"}       (só os)
        #   appVersion = binaryAppVersion
        # O metadata.json DENTRO do sidecar permanece COMPLETO (model +
        # systemName/systemVersion; platform {os,version} no 1.29) — é o
        # sidecar que alimenta os checks; o POST usa o formato curto.
        # Com perfil: o MODELO SAMSUNG DO APARELHO DA CONTA (S21–S24).
        if _original_capture is not None:
            # Short POST fields come from the reviewed capture. The ZIP bytes
            # retain the full original device/platform and unknown metadata.
            meta["device"] = {"model": _original_capture.metadata["device"]["model"]}
            source_platform = _original_capture.metadata["platform"]
            meta["platform"] = {"os": source_platform.get("os", source_platform.get("type"))}
            meta["appVersion"] = _original_capture.metadata["appVersion"]
        else:
            meta["device"] = default_device_meta(profile)
            meta["platform"] = default_platform_meta(profile)
            meta["appVersion"] = config.APP_VERSION

        # QA local: o backend compara o meta do POST com o sidecar
        # (xcheck.metadata_json_matches_upload) — qualquer drift cai aqui.
        from .validate import summarize as _summarize
        from .validate import validate_upload_meta as _check_upload_meta
        _meta_check = _summarize(_check_upload_meta(
            meta, log_id=log_id, duration_ms=duration_ms))
        if _meta_check["counts"].get("fail", 0):
            details = "; ".join(f"{item['id']}: {item['detail']}"
                                for item in _meta_check["failures"])
            raise UploadError(
                f"meta do POST /uploads reprovado no checklist local: {details}",
                transient=False,
            )

        upload_body: dict[str, Any] = {
            "session_id": session_id,
            "log_id": log_id,
            "duration_ms": duration_ms,
            "recorded_at": recorded_at,
            "meta": meta,
        }
        if task_id:
            upload_body["task_id"] = task_id
        create_progress_attempt = 0

        def _create_upload() -> dict[str, Any]:
            nonlocal create_progress_attempt, conflict_action, upload_id, create_data
            create_progress_attempt += 1
            _emit("create", STATE_CREATING, create_progress_attempt)
            if _expected_video_sha256 is not None:
                _verify_video_content_digest(video_path, _expected_video_sha256)
            if _original_capture is not None:
                try:
                    current = inspect_original_capture(_original_capture.media.path,
                                                       _original_capture.sidecar.path)
                    if current != _original_capture:
                        raise CaptureImportError("source_changed")
                except (CaptureImportError, OSError):
                    raise UploadError("A captura original mudou antes do registro; arquivos preservados para revisão.",
                                      transient=False, phase="preflight", review_required=True) from None
            # Commit intent before the request: an abrupt process exit may
            # otherwise leave an accepted remote upload with no local ID.
            _checkpoint(STATE_CREATING, "create_in_flight",
                        create_attempted=True, attempts=create_progress_attempt)
            # O app nativo envia o org_key como QUERY PARAM (/uploads?org_key=...),
            # não no corpo — replicar isso é o que associa o upload ao catbear/org.
            try:
                status, text, response_headers = _session_request(
                    session, "POST", f"/api/v1/uploads?org_key={org_key}", upload_body)
            except Exception as exc:
                # Auth failures retain their typed account diagnosis. Adapter
                # failures may follow an accepted request and must stay private.
                from .minute_api import AuthError
                if isinstance(exc, AuthError):
                    raise
                typed = isinstance(exc, UploadError)
                status_code = exc.status_code if typed else _status_from_exception(exc)
                blocked_reason = exc.blocked_reason if typed else None
                status_detail = f" ({status_code})" if status_code is not None else ""
                message = f"CREATE sem confirmação do serviço{status_detail}; preserve o registro para revisão."
                if blocked_reason:
                    reason = (blocked_reason if isinstance(blocked_reason, str) and re.fullmatch(
                        r"[a-z][a-z0-9_-]{0,63}", blocked_reason) else "não especificado")
                    message += f" [bloqueio: {reason}]"
                raise UploadError(
                    message, status_code=status_code,
                    transient=exc.transient if typed else True,
                    blocked_reason=blocked_reason, phase="create") from None
            if status == 409:
                data, conflict_action = _conflict_upload(text)
                return data
            # O schema diz response 201 (Created), não 200.
            if status not in (200, 201):
                raise _http_upload_error(
                    "POST /uploads", status, text, response_headers)
            try:
                data = decode_json_state(text)
            except JsonStateError:
                raise UploadError("Resposta de registro do upload sem JSON válido.",
                                  status_code=status, transient=False, phase="create") from None
            # Keep a trustworthy ID even when the remaining acknowledgement
            # cannot authorize SAS/transport. Never replace it with a new CREATE.
            upload_id = _response_upload_id(data, status=status)
            create_data = data
            if _native_response_schema and register_first:
                _validate_native_upload_out(data, "create")
            return data

        # session_id/log_id are stable request bindings, not a demonstrated
        # provider idempotency guarantee. A lost acknowledgement cannot safely
        # authorize another CREATE. Known409 receipts still follow their driver.
        create_data, create_attempts = _with_retry(
            _create_upload, max_retries=1, retry_backoff=retry_backoff,
            label="create",
        )
        # UploadOut usa "id" como campo do uploadId.
        upload_id = _response_upload_id(create_data)
        # conflict_action is set only by the validated409 parser, never by a
        # similarly named field in an ordinary server success response.
        dead_end = conflict_action == "dead-end"
        transport_sidecar_only = (register_first and conflict_action == "complete"
                                  and sidecar_bytes is not None)
        _checkpoint(
            STATE_FAILED if dead_end else STATE_TRANSPORT,
            "create_conflict_dead_end" if dead_end else "registered", upload_id=upload_id,
            conflict_action=conflict_action, attempts=create_attempts,
            **({"transport_artifact": "sidecar"} if transport_sidecar_only else {}),
        )
        if dead_end:
            raise UploadError(
                "Registro conflitante em estado failed; envio preservado para revisão.",
                status_code=409, transient=False, phase="create_conflict_dead_end")

    # Ordem observada em driveCreate do APK 1.29.0: registro -> SAS -> transporte.
    if register_first:
        try:
            if resume_row is None:
                _register()
        except UploadError as exc:
            create_attempts = exc.attempts or create_attempts
            failure_state = (STATE_QUARANTINE if exc.review_required else
                             STATE_RETRY_LATE if exc.retryable else STATE_FAILED)
            _checkpoint(failure_state,
                        "registration_review" if exc.review_required else
                        "create_conflict_dead_end" if conflict_action == "dead-end" else "create",
                        error=str(exc), upload_id=upload_id,
                        attempts=create_attempts)
            return ChunkResult(
                upload_id=str(upload_id), chunk_index=chunk_index, log_id=log_id, blob_path=blob_path,
                size_bytes=file_size, duration_ms=duration_ms,
                raw_create=create_data, state=failure_state,
                attempts=create_attempts, error=str(exc),
            )
        if conflict_action != "complete" or transport_sidecar_only:
            sas_failure = _request_sas_stage()
            if sas_failure is not None:
                return sas_failure

    put_attempts = 1
    if conflict_action != "complete" or transport_sidecar_only:
        try:
            _, put_attempts = _with_retry(
                _transport_resilient, max_retries=max_retries,
                retry_backoff=retry_backoff,
                label="transport",
            )
        except UploadError as exc:
            put_attempts = exc.attempts or 1
            failure_state = (STATE_QUARANTINE if exc.review_required else
                             STATE_RETRY_LATE if exc.retryable else STATE_FAILED)
            _checkpoint(
                failure_state, "transport_review" if exc.review_required else "transport", upload_id=upload_id,
                error=str(exc), attempts=put_attempts,
            )
            # Só uma falha permanente deve encerrar o registro no servidor.
            if (register_first and upload_id and fail_on_error
                    and not exc.retryable and not exc.review_required):
                try:
                    _emit("fail", STATE_FAILED, 1)
                    fail_upload(session, upload_id, f"transport falhou: {exc}")
                except UploadError:
                    pass
            return ChunkResult(
                upload_id=str(upload_id), chunk_index=chunk_index, log_id=log_id,
                blob_path=blob_path, size_bytes=file_size, duration_ms=duration_ms,
                state=failure_state, attempts=put_attempts, error=str(exc),
            )
        _checkpoint(
            STATE_COMPLETING, "transport_done", upload_id=upload_id,
            attempts=put_attempts,
        )

    if not register_first:
        try:
            _register()
        except UploadError as exc:
            create_attempts = exc.attempts or create_attempts
            failure_state = (STATE_QUARANTINE if exc.review_required else
                             STATE_RETRY_LATE if exc.retryable else STATE_FAILED)
            _checkpoint(failure_state,
                        "registration_review" if exc.review_required else
                        "create_conflict_dead_end" if conflict_action == "dead-end" else "create",
                        error=str(exc), upload_id=upload_id,
                        attempts=create_attempts)
            return ChunkResult(
                upload_id=str(upload_id), chunk_index=chunk_index, log_id=log_id, blob_path=blob_path,
                size_bytes=file_size, duration_ms=duration_ms,
                raw_create=create_data, state=failure_state,
                attempts=create_attempts, error=str(exc),
            )

    # --- 4. PATCH /api/v1/uploads/{id}/complete ----------------------------
    complete_progress_attempt = 0

    def _complete() -> dict[str, Any]:
        nonlocal complete_progress_attempt
        complete_progress_attempt += 1
        _emit("complete", STATE_COMPLETING, complete_progress_attempt)
        return complete_upload(
            session, upload_id, file_size,
            suppress_per_chunk_catbear=suppress_per_chunk_catbear,
            session_complete=session_complete,
            _native_response_schema=_native_response_schema and register_first,
        )

    complete_attempts = 1
    _checkpoint(STATE_COMPLETING, "completing", upload_id=upload_id)
    try:
        complete_data, complete_attempts = _with_retry(
            _complete, max_retries=max_retries, retry_backoff=retry_backoff,
            label="complete",
        )
    except UploadError as exc:
        complete_attempts = exc.attempts or complete_attempts
        error_msg = f"complete falhou após {complete_attempts} tentativas: {exc}"
        # 5xx/rede preservam upload_id e estado para retomar só o complete.
        failure_state = (STATE_QUARANTINE if exc.review_required else
                         STATE_COMPLETING if exc.retryable else STATE_FAILED)
        _checkpoint(
            failure_state, "completion_review" if exc.review_required else "complete", upload_id=upload_id,
            error=error_msg, attempts=complete_attempts,
        )
        if fail_on_error and not exc.retryable and not exc.review_required:
            try:
                _emit("fail", STATE_FAILED, 1)
                fail_upload(session, upload_id, error_msg)
            except UploadError:
                pass  # melhor esforço — o erro original é o que importa
        return ChunkResult(
            upload_id=str(upload_id), chunk_index=chunk_index, log_id=log_id,
            blob_path=blob_path, size_bytes=file_size, duration_ms=duration_ms,
            raw_create=create_data, state=failure_state,
            attempts=complete_attempts, error=error_msg,
        )

    _checkpoint(STATE_DONE, "done", upload_id=upload_id)
    return ChunkResult(
        upload_id=str(upload_id),
        chunk_index=chunk_index,
        log_id=log_id,
        blob_path=blob_path,
        size_bytes=file_size,
        duration_ms=duration_ms,
        raw_create=create_data,
        raw_complete=complete_data,
        state=STATE_DONE,
        attempts=max(sas_attempts, put_attempts, create_attempts, complete_attempts),
        sidecar_blob_path=sidecar_blob_path,
        sidecar_size_bytes=len(sidecar_bytes) if sidecar_bytes else 0,
    )


# --- Etapa 5: Finalize sessão -------------------------------------------------

def _finalize_session(
    session: Any,
    org_key: str,
    session_id: str,
    expected_chunk_count: int,
) -> tuple[bool, int]:
    """POST /api/v1/organizations/{org}/sessions/{sid}/finalize.

    Marca a sessão como completa e elegível para catbear (avaliação de qualidade).
    Retorna (sucesso, status_http). Sucesso é 204 No Content.
    """
    finalize_body = {"expected_chunk_count": expected_chunk_count}
    status, text = session.request(
        "POST",
        f"/api/v1/organizations/{org_key}/sessions/{session_id}/finalize",
        finalize_body,
    )
    return status == 204, status


# --- Etapa 6: Evaluate (opcional) --------------------------------------------

def evaluate_upload(session: Any, upload_id: str) -> dict[str, Any]:
    """POST /api/v1/uploads/{id}/evaluate — roda o checklist de qualidade.

    Retorna o EvaluationResult ({upload_id, checks:[...]}).
    Levanta UploadError se falhar (404, 429, etc.).
    """
    status, text = session.request("POST", f"/api/v1/uploads/{upload_id}/evaluate")
    if status != 200:
        raise UploadError(f"Avaliação não concluída (HTTP {status}).", status_code=status,
                          transient=(status == -1 or status in (408, 429) or status >= 500),
                          phase="evaluate")
    try:
        evaluation = decode_json_state(text)
    except JsonStateError:
        raise UploadError("Avaliação devolveu uma resposta inválida.", transient=False,
                          phase="evaluate") from None
    if (not isinstance(evaluation, dict) or evaluation.get("upload_id") != upload_id
            or not summarize_checks(evaluation)["valid"]
            or any(not isinstance(check.get("id"), str)
                   or not isinstance(check.get("label"), str)
                   or "detail" not in check
                   or (check["detail"] is not None and not isinstance(check["detail"], str))
                   for check in evaluation.get("checks", []))):
        raise UploadError("Avaliação incompleta ou referente a outro vídeo; precisa de revisão.",
                          transient=False, phase="evaluate")
    return evaluation


def summarize_checks(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Resume o EvaluationResult: contagem pass/fail/skip e lista de falhas."""
    checks = evaluation.get("checks") if isinstance(evaluation, dict) else None
    valid = (isinstance(checks, list) and bool(checks)
             and all(isinstance(c, dict) and c.get("status") in ("pass", "fail", "skip")
                     for c in checks))
    if not valid:
        return {"counts": {"pass": 0, "fail": 0, "skip": 0}, "failures": [], "valid": False}
    counts = {"pass": 0, "fail": 0, "skip": 0}
    failures: list[dict[str, Any]] = []
    for c in checks:
        status = c.get("status")
        counts[status] = counts.get(status, 0) + 1
        if status == "fail":
            failures.append(c)
    return {"counts": counts, "failures": failures, "valid": True}


def is_perfect(evaluation: dict[str, Any]) -> bool:
    """True se o checklist do evaluate não tem nenhum check 'fail'.

    Checks 'skip' são aceitos (não aplicáveis à plataforma); qualquer 'fail'
    reprova a gravação (política perfect-only).
    """
    summary = summarize_checks(evaluation)
    return summary["valid"] and summary["counts"].get("fail", 0) == 0


def require_perfect_upload(
    session: Any,
    upload_id: str,
    org_key: str,
    session_id: str,
    *,
    fail_on_error: bool = True,
    delete_on_fail: bool = True,
) -> dict[str, Any]:
    """Política perfect-only: roda evaluate e, se houver qualquer 'fail',
    marca PATCH /fail e deleta o upload + a sessão no backend.

    Mimica a regra do operador: "toda gravação que não for perfeita, cancele o
    envio e delete-a no app Minute".

    Retorna dict com: perfect (bool), evaluation (EvaluationResult),
    failed_ids (lista de ids de checks que falharam),
    deleted (bool), deleted_session (bool).
    """
    evaluation = evaluate_upload(session, upload_id)
    summary = summarize_checks(evaluation)
    perfect = is_perfect(evaluation)
    result: dict[str, Any] = {
        "perfect": perfect,
        "evaluation": evaluation,
        "failed_ids": [c.get("id") for c in summary["failures"]],
        "deleted": False,
        "deleted_session": False,
    }
    if perfect:
        return result

    error_msg = "reprovado no evaluate: " + ", ".join(
        c.get("id", "?") for c in summary["failures"])
    if fail_on_error:
        try:
            fail_upload(session, upload_id, error_msg)
        except UploadError:
            pass
    if delete_on_fail:
        try:
            result["deleted"] = delete_upload(session, upload_id)
        except UploadError:
            result["deleted"] = False
        try:
            result["deleted_session"] = delete_session(session, org_key, session_id)
        except UploadError:
            result["deleted_session"] = False
    return result


# --- Fluxo completo (sessão com 1+ chunks) -----------------------------------

def _original_group_payloads(
    captures: list[CaptureDescriptor] | tuple[CaptureDescriptor, ...],
    paths: list[Path], *, session: Any, owner: str, org_key: str, task_id: str | None,
    session_id: str | None, recorded_at: str | list[str] | None,
    supplied: bytes | list[bytes] | None,
) -> tuple[list[CaptureDescriptor], list[bytes], list[int], list[str]]:
    """Reinspect all parts before journaling/HTTP; never repair source data."""
    def refuse() -> None:
        raise UploadError("A captura original não corresponde ao plano revisado; "
                          "arquivos preservados para revisão.", transient=False,
                          phase="preflight", review_required=True) from None

    if (not isinstance(captures, (list, tuple)) or not captures
            or len(captures) != len(paths) or not owner
            or not isinstance(org_key, str) or not org_key.strip()
            or not isinstance(task_id, str) or not task_id.strip()
            or not isinstance(recorded_at, list) or len(recorded_at) != len(paths)):
        refuse()
    if any(not isinstance(capture, CaptureDescriptor) for capture in captures):
        refuse()
    # The campaign, API and direct uploader share the same pure group policy.
    # The import is deferred because that validator calls the ZIP validator in
    # this module; it performs no upload or persistent-state operation.
    from .original_capture import (OriginalCaptureError, prepare_original_capture_plan,
                                   validate_original_capture_session_policy)
    try:
        fresh = prepare_original_capture_plan(
            [(capture.media.path, capture.sidecar.path) for capture in captures],
            account_email=owner, org_key=org_key, task_id=task_id,
            expected_chunk_count=len(paths), probe=probe_video)
    except OriginalCaptureError:
        refuse()
    if fresh.session_id != session_id or list(fresh.recorded_at) != recorded_at:
        refuse()
    observed = list(fresh.captures)
    payloads: list[bytes] = []
    for i, (expected, actual, path) in enumerate(zip(captures, observed, paths, strict=True)):
        try:
            if actual != expected or path.absolute() != actual.media.path:
                refuse()
            meta = actual.metadata
            source_os = meta["platform"].get("os", meta["platform"].get("type"))
            with actual.sidecar.path.open("rb") as stream:
                payload = stream.read(actual.sidecar.bytes + 1)
            if (len(payload) != actual.sidecar.bytes
                    or hashlib.sha256(payload).hexdigest() != actual.sidecar.sha256):
                refuse()
            declared = meta["durationMs"]
            from .validate import summarize, validate_upload_meta
            post_meta = {**meta, "device": {"model": meta["device"]["model"]},
                         "platform": {"os": source_os},
                         "chunk_index": i, "size_bytes": actual.media.bytes}
            if summarize(validate_upload_meta(post_meta, log_id=f"{session_id}_{i}",
                                              duration_ms=declared))["counts"].get("fail", 0):
                refuse()
        except (CaptureImportError, OSError, KeyError, TypeError, ValueError, UploadError):
            refuse()
        payloads.append(payload)
    if supplied is not None:
        given = supplied if isinstance(supplied, list) else [supplied]
        if given != payloads:
            refuse()
    validate_original_capture_session_policy(fresh, session)
    return observed, payloads, list(fresh.durations_ms), list(fresh.recorded_at)


def _verify_video_content_digest(path: Path, digest: Any) -> None:
    if type(digest) is not str or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise UploadError("Hash do conteúdo inválido; novo preparo necessário.",
                          transient=False, phase="preflight", review_required=True)
    from .content_provenance import fingerprint
    try:
        if fingerprint(path)["sha256"] != digest:
            raise ValueError("changed")
    except (OSError, ValueError):
        raise UploadError("O conteúdo local mudou; novo preparo necessário, arquivos preservados para revisão.",
                          transient=False, phase="preflight", review_required=True) from None


from .upload_protocol_lease import session_protocol, pending_protocol


@session_protocol
def upload_session(
    session: Any,
    video_path: str | Path | list[str | Path],
    org_key: str,
    task_id: str | None = None,
    session_id: str | None = None,
    recorded_at: str | list[str] | None = None,
    content_type: str = "video/mp4",
    timeout_blob: int = 300,
    finalize: bool = True,
    evaluate: bool = False,
    device_meta: dict[str, Any] | None = None,
    platform_meta: dict[str, Any] | None = None,
    video_meta: dict[str, Any] | None = None,
    network_meta: dict[str, Any] | None = None,
    max_retries: int = 3,
    retry_backoff: float = 1.5,
    suppress_per_chunk_catbear: bool = True,
    fail_on_error: bool = True,
    persist_sidecar: bool = False,
    sidecar: bool = True,
    sidecar_data: bytes | list[bytes] | None = None,
    ego_meta: dict[str, Any] | None = None,
    require_perfect: bool = False,
    normalize: bool = True,
    register_first: bool = True,
    on_progress: Callable[..., None] | None = None,
    profile: DeviceProfile | None = None,
    chunk_index_start: int = 0,
    campaign_context: dict[str, Any] | None = None,
    original_captures: list[CaptureDescriptor] | tuple[CaptureDescriptor, ...] | None = None,
    expected_video_sha256: str | list[str] | None = None,
) -> UploadResult:
    """Executa o upload completo de uma sessão (1+ chunks) ao backend do Minute.

    Parâmetros:
        session       — `moneymin.minute_api.Session` autenticada.
        video_path    — caminho do vídeo (str/Path) ou lista de caminhos (multi-chunk).
        org_key       — resourceKey da organização destino.
        task_id       — UUID da task do Minute (opcional, liga o upload à task).
        session_id    — ID da sessão de gravação (UUID v4 gerado se não informado).
        content_type  — MIME type do vídeo (default video/mp4).
        timeout_blob  — timeout em segundos para o PUT no Azure (default 300).
        finalize      — se True, chama POST /sessions/{sid}/finalize ao final.
        evaluate      — se True, chama POST /uploads/{id}/evaluate em cada chunk.
        device_meta   — metadados de dispositivo (default: Pixel 8 Pro / Android 14).
        platform_meta — metadados de plataforma (default: android / app_version).
        video_meta    — metadados de vídeo (default: h264 / 1080p / 30fps).
        network_meta  — metadados de rede (default: wifi).
        max_retries   — limite de tentativas nas etapas compatíveis (default3).
                      CREATE sem recibo usa uma tentativa; falha mantém o
                      registro para revisão, sem presumir idempotência remota.
        retry_backoff — multiplicador do backoff entre retries (default 1.5).
        suppress_per_chunk_catbear — True em envios novos, como no helper do APK
                      1.29.0, independentemente da contagem de chunks. False
                      explícito mantém a omissão legada. Journal existente governa
                      a política efetiva: valor booleano salvo ou False se ausente.
        fail_on_error — se True, marca PATCH /fail quando o registro já existe
                      e a confirmação falha (comportamento _failUpload do app).
        persist_sidecar — se True, grava data/sidecars/<session_id>.json com o
                      estado de cada chunk (permite retomada via pump_pending).
        sidecar       — se True (default), gera e sobe o `.data.zip` nativo
                      (imu.csv/frames.csv/metadata.json) junto com o MP4 e envia
                      meta ego (source/timebase/cameras/codecActuals). Requisito
                      do evaluate do backend.
        require_perfect — política perfect-only: após o upload, roda o evaluate
                      e, se QUALQUER check falhar, marca PATCH /fail e DELETA o
                      upload + a sessão no backend (DELETE /uploads/{id} e
                      DELETE /sessions/{sid}). Só finaliza a sessão se perfeita.
        profile       — perfil de APARELHO da conta (device_profile). Com ele,
                      o sidecar usa calibração/uptime próprios e o meta usa o
                      modelo daquele aparelho (anti-colusão). Sem ele, usa o
                      Samsung padrão de referência (config.NATIVE_*).
        chunk_index_start — índice inicial do chunk; usado pela retomada para
                      manter a identidade original e evitar registros duplicados.
        on_progress   — callback síncrono(phase, state, attempt) no fluxo padrão, antes de
                      CREATE, SAS, PUT de vídeo/ZIP, COMPLETE, EVALUATE,
                      FINALIZE e compensação /fail, incluindo novas tentativas
                      dessas etapas. O chamador pode aguardar Retomar nele.
                      O progresso do transporte consulta o chamador antes de
                      cada bloco/tentativa e BlockList, inclusive no ZIP.
                      Requisições já em andamento podem terminar.
                      A política require_perfect tem compensações próprias.

    Devolve `UploadResult` com os IDs, metadados e status de finalize/evaluate.
    Levanta `UploadError` em qualquer falha.
    """
    _require_completion_flags(suppress_per_chunk_catbear)
    if type(register_first) is not bool:
        raise UploadError("Política de ordem do upload inválida; use um valor booleano.",
                          transient=False, phase="preflight")
    if type(chunk_index_start) is not int or chunk_index_start < 0:
        raise UploadError("Índice inicial do envio inválido.", transient=False, phase="preflight")
    session_owner = _upload_owner(session, profile)
    # Normaliza video_path para lista de Paths.
    if isinstance(video_path, (str, Path)):
        paths = [Path(video_path)]
    else:
        paths = [Path(p) for p in video_path]
    expected_digests: list[str] | None = None
    if expected_video_sha256 is not None:
        if normalize is not False:
            raise UploadError("Conteúdo byte-bound exige normalize=False.", transient=False, phase="preflight")
        expected_digests = ([expected_video_sha256] if type(expected_video_sha256) is str
                            else expected_video_sha256)
        if type(expected_digests) is not list or not paths or len(expected_digests) != len(paths):
            raise UploadError("Grupo de hashes do conteúdo inválido.", transient=False, phase="preflight")
        # Validate the WHOLE group before any journal, CREATE or normalization.
        for path, digest in zip(paths, expected_digests, strict=True):
            _verify_video_content_digest(path, digest)

    original_durations: list[int] | None = None
    original_sequence: list[str] | None = None
    if original_captures is not None:
        if (normalize is not False or register_first is not True or sidecar is not True
                or persist_sidecar is not True or chunk_index_start != 0
                or any(type(value) is not bool for value in (evaluate, finalize, require_perfect, fail_on_error))
                or profile is not None or ego_meta is not None
                or any(value is not None for value in (device_meta, platform_meta, video_meta, network_meta))):
            raise UploadError("A captura original exige envio integral sem alteração de arquivos ou metadados.",
                              transient=False, phase="preflight", review_required=True)
        original_captures, sidecar_data, original_durations, original_sequence = _original_group_payloads(
            original_captures, paths, session=session, owner=session_owner, org_key=org_key, task_id=task_id,
            session_id=session_id, recorded_at=recorded_at, supplied=sidecar_data)
        if expected_digests is not None and expected_digests != [capture.media.sha256 for capture in original_captures]:
            raise UploadError("O hash informado difere da captura original.", transient=False, phase="preflight")

    explicit_session_id = session_id is not None
    session_id = session_id or str(uuid.uuid4())
    existing_journals: list[dict[str, Any] | None] = []
    for offset in range(len(paths)):
        existing = (load_sidecar(session_id, chunk_index_start + offset)
                    if persist_sidecar or explicit_session_id else None)
        if existing is not None:
            if existing.get("video_content_sha256") is not None:
                persisted_digest = existing["video_content_sha256"]
                if normalize is not False:
                    raise UploadError("O registro byte-bound exige normalize=False; preserve os arquivos para revisão.",
                                      transient=False, phase="recovery", review_required=True)
                _verify_video_content_digest(paths[offset], persisted_digest)
                if expected_digests is not None and expected_digests[offset] != persisted_digest:
                    raise UploadError("O hash do conteúdo difere do registro; preserve os recibos para revisão.",
                                      transient=False, phase="recovery", review_required=True)
            if original_captures is not None:
                # The original campaign core classifies/reconciles existing
                # receipts. Direct upload never upgrades an unowned legacy row
                # or replaces a queued attempt with a new CREATE.
                raise _existing_receipt_error()
            if not journal_flags_valid(existing):
                raise UploadError("Política do registro de envio inválida; preserve o arquivo para revisão.",
                                  transient=False, phase="recovery")
            if _journal_has_remote_receipt(existing):
                raise _existing_receipt_error()
            if _journal_creation_uncertain(existing):
                raise _uncertain_creation_error()
            _journal_recorded_at(existing)
            _journal_flag(existing, "suppress_per_chunk_catbear")
            _journal_flag(existing, "register_first")
            _validate_journal_owner(existing, session_owner)
            # Preserve the binding before normalizing media or replacing state.
            idx = chunk_index_start + offset
            expected_identity = {"org_key": org_key, "task_id": task_id,
                                 "log_id": f"{session_id}_{idx}",
                                 "filename": f"{session_id}_{idx}.mp4"}
            if any(existing.get(key) is not None and existing[key] != value
                   for key, value in expected_identity.items()):
                raise UploadError("A identidade do envio difere do registro existente; preserve o arquivo para revisão.",
                                  transient=False, phase="recovery")
        existing_journals.append(existing)

    # A caller may add a quality gate, but cannot remove one already recorded
    # for a chunk. Legacy campaign journals follow the same policy as recovery.
    quality_required = [bool(
        evaluate or require_perfect or (row is not None and row.get(
            "evaluation_required", bool(row.get("campaign_context")))))
        for row in existing_journals]

    for p in paths:
        if not p.exists():
            raise UploadError(f"vídeo não encontrado: {p}")

    preserved_sequence: list[str] | None = None
    if any(row is not None for row in existing_journals):
        original_durations = original_durations or [_probe_duration_ms(path) for path in paths]
        if recorded_at is None:
            if any(row is None for row in existing_journals):
                raise UploadError("Uma sessão parcialmente registrada exige horários explícitos para os novos chunks; "
                                  "os horários existentes foram preservados.",
                                  transient=False, phase="recovery")
            preserved_sequence = [_journal_recorded_at(row) for row in existing_journals]
        else:
            if isinstance(recorded_at, list):
                candidate_sequence = _normalize_recorded_at_sequence(
                    [str(value) for value in recorded_at], original_durations)
            else:
                candidate_sequence = _recorded_at_sequence_from_base(
                    str(recorded_at), original_durations)
            preserved_sequence = list(candidate_sequence)
            for offset, row in enumerate(existing_journals):
                if row is None:
                    continue
                original_time = _journal_recorded_at(row)
                if recorded_at_to_wall_ms(candidate_sequence[offset]) != recorded_at_to_wall_ms(original_time):
                    raise UploadError("O horário solicitado difere do horário original registrado; "
                                      "preserve o arquivo para revisão.", transient=False, phase="recovery")
                preserved_sequence[offset] = original_time
        # Validate bounds/order, but retain the exact original clock text.
        _normalize_recorded_at_sequence(preserved_sequence, original_durations)

    # Reencode full-range (yuvj420p) -> yuv420p antes de subir. Sem isso o
    # backend rejeita o preview (previewStatus "unavailable") e o vídeo não
    # abre no app. Com normalize=False o usuário assume o risco.
    if normalize:
        normalized: list[Path] = []
        for p in paths:
            np = normalize_video(p)
            if np != p:
                print(f"    [normalize] {p.name}: full-range yuvj420p -> yuv420p "
                      f"({np.name})")
            normalized.append(np)
        paths = normalized

    # Defaults de meta (mimica app nativo) se não fornecidos.
    if device_meta is None and original_captures is None:
        device_meta = default_device_meta(profile)
    if platform_meta is None and original_captures is None:
        platform_meta = default_platform_meta(profile)
    if video_meta is None and original_captures is None:
        video_meta = default_video_meta()
    if network_meta is None and original_captures is None:
        network_meta = default_network_meta()

    # recorded_at = início da gravação, formato JS toISOString (3 dígitos).
    # Sem valor: a gravação acabou de terminar (agora - duração), dentro do
    # backlog de 4h. Nunca o literal `.000Z` nem 6 dígitos.
    durations_ms = original_durations if original_captures is not None else [_probe_duration_ms(p) for p in paths]
    total_ms = sum(durations_ms)
    recorded_at_list: list[str] | None = None
    if original_sequence is not None:
        recorded_at_list = original_sequence
        recorded_at = original_sequence[0]
    elif preserved_sequence is not None:
        _normalize_recorded_at_sequence(preserved_sequence, durations_ms)
        recorded_at = preserved_sequence[0]
        recorded_at_list = preserved_sequence
    elif isinstance(recorded_at, list):
        recorded_at_list = _normalize_recorded_at_sequence(
            [str(value) for value in recorded_at], durations_ms)
        recorded_at = recorded_at_list[0] if recorded_at_list else ""
    if recorded_at_list is None:
        base_recorded_at = (
            str(recorded_at) if recorded_at else format_recorded_at(
                recording_start_epoch(total_ms / 1000.0))
        )
        sequence = _recorded_at_sequence_from_base(
            base_recorded_at, durations_ms)
        recorded_at = sequence[0]
        if len(sequence) > 1:
            recorded_at_list = sequence

    chunks: list[ChunkResult] = []
    total_size = 0
    total_duration = 0

    def _update_persisted_journals(**updates: Any) -> None:
        if not persist_sidecar:
            return
        for journal_index in range(
                int(chunk_index_start), int(chunk_index_start) + len(paths)):
            stored = load_sidecar(session_id, journal_index)
            if not stored:
                continue
            stored.update(updates)
            stored["updated_at"] = _iso_now()
            save_sidecar(stored)

    def _save_quality_checkpoint(chunk: ChunkResult, **updates: Any) -> None:
        if not persist_sidecar:
            return
        stored = load_sidecar(session_id, chunk.chunk_index)
        if stored is None:
            raise UploadError("Registro de qualidade ausente; preserve os recibos para revisão.",
                              transient=False, phase="evaluate", review_required=True)
        if updates.get("phase") == "evaluation_review" and "quality_review_origin_phase" not in stored:
            updates["quality_review_origin_phase"] = stored.get("phase", "")
        stored.update(updates)
        stored.update(evaluate_result=chunk.evaluate_result, updated_at=_iso_now())
        save_sidecar(stored)

    for offset, vpath in enumerate(paths):
        idx = chunk_index_start + offset
        existing_row = existing_journals[offset]
        effective_suppression = (suppress_per_chunk_catbear if existing_row is None
                                 else _journal_flag(existing_row, "suppress_per_chunk_catbear"))
        effective_register_first = (register_first if existing_row is None
                                    else _journal_flag(existing_row, "register_first"))
        effective_native_schema = (register_first if existing_row is None
                                   else _journal_flag(existing_row, "native_response_schema"))
        chunk_recorded_at = (
            recorded_at_list[offset]
            if recorded_at_list and offset < len(recorded_at_list)
            else recorded_at
        )
        chunk_sidecar_data: bytes | None = None
        if isinstance(sidecar_data, list):
            if offset < len(sidecar_data):
                chunk_sidecar_data = sidecar_data[offset]
        else:
            chunk_sidecar_data = sidecar_data
        journal: dict[str, Any] | None = None
        checkpoint_callback: Callable[..., None] | None = None
        if persist_sidecar:
            existing = existing_row or {}
            journal = {
                **existing,
                "campaign_context": existing.get("campaign_context") or campaign_context,
                "schema_version": 2,
                "session_id": session_id,
                "org_key": org_key,
                "task_id": task_id,
                "chunk_index": idx,
                "log_id": f"{session_id}_{idx}",
                "filename": f"{session_id}_{idx}.mp4",
                "local_video_path": str(vpath.resolve()),
                "size_bytes": vpath.stat().st_size,
                "recorded_at": chunk_recorded_at,
                "state": STATE_CREATING,
                "phase": "queued",
                "create_attempted": False,
                "attempts": int(existing.get("attempts") or 0),
                "crash_resumes": int(existing.get("crash_resumes") or 0),
                "finalize_requested": bool(
                    existing.get("finalize_requested", finalize)),
                "evaluation_required": quality_required[offset],
                "evaluation_verified": False,
                "expected_chunk_count": int(
                    existing.get("expected_chunk_count") or len(paths)),
                "updated_at": _iso_now(),
            }
            # An absent legacy flag stays absent; changing its spelling/policy
            # retroactively would change the next recovery operation.
            if existing_row is None or "register_first" in existing:
                journal["register_first"] = effective_register_first
            if existing_row is None or "suppress_per_chunk_catbear" in existing:
                journal["suppress_per_chunk_catbear"] = effective_suppression
            if existing_row is None or "native_response_schema" in existing:
                journal["native_response_schema"] = effective_native_schema
            if existing_row is None:
                journal["account_email"] = session_owner
            if expected_digests is not None:
                journal["video_content_sha256"] = expected_digests[offset]
            if original_captures is not None:
                original = original_captures[offset]
                journal.update(source_mode="original", original_media_sha256=original.media.sha256,
                               original_sidecar_sha256=original.sidecar.sha256,
                               physical_provenance_verified=False,
                               completion_strategy="explicit_finalize")
            if chunk_sidecar_data is not None:
                archive_path = _sidecar_archive_path(session_id, idx)
                save_bytes(archive_path, chunk_sidecar_data)
                journal["sidecar_data_path"] = str(archive_path.resolve())
            save_sidecar(journal)

            def _save_checkpoint(
                *, _journal: dict[str, Any] = journal,
                **updates: Any,
            ) -> None:
                _journal.update(updates)
                _journal["updated_at"] = _iso_now()
                save_sidecar(_journal)

            checkpoint_callback = _save_checkpoint

        chunk = _upload_single_chunk(
            session=session,
            video_path=vpath,
            org_key=org_key,
            session_id=session_id,
            chunk_index=idx,
            task_id=task_id,
            content_type=content_type,
            timeout_blob=timeout_blob,
            recorded_at=chunk_recorded_at,
            device_meta=device_meta,
            platform_meta=platform_meta,
            video_meta=video_meta,
            network_meta=network_meta,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            suppress_per_chunk_catbear=effective_suppression,
            # Estratégia explícita de conclusão do QMoney. O sinal Android é
            # condicional a accepted && saveGated, sem equivalência comprovada
            # com estes estados desktop; não combinamos as duas estratégias.
            session_complete=False,
            fail_on_error=fail_on_error,
            sidecar=sidecar,
            sidecar_data=chunk_sidecar_data,
            ego_meta=ego_meta,
            register_first=effective_register_first,
            on_progress=on_progress,
            profile=profile,
            checkpoint=checkpoint_callback,
            _native_response_schema=effective_native_schema,
            **({"_original_capture": original_captures[offset]} if original_captures is not None else {}),
            **({"_expected_video_sha256": expected_digests[offset]} if expected_digests is not None else
               {"_expected_video_sha256": existing_row["video_content_sha256"]}
               if existing_row is not None and existing_row.get("video_content_sha256") is not None else {}),
        )
        chunks.append(chunk)
        total_size += chunk.size_bytes
        total_duration += chunk.duration_ms

        if persist_sidecar:
            final_journal = _chunk_sidecar(
                chunk, session_id, org_key, task_id, chunk_recorded_at,
                f"{chunk.log_id}.mp4", chunk.state, chunk.attempts, chunk.error,
            )
            if journal is not None:
                final_journal = {**journal, **final_journal}
            final_journal["local_video_path"] = str(vpath.resolve())
            final_journal["phase"] = (
                "done" if chunk.state == STATE_DONE else final_journal.get("phase")
                or chunk.state)
            save_sidecar(final_journal)

        if (chunk.state == STATE_DONE and chunk.upload_id
                and not require_perfect and (evaluate or quality_required[offset])):
            try:
                if on_progress:
                    on_progress("evaluate", STATE_COMPLETING, 1)
                chunk.evaluate_result = evaluate_upload(session, chunk.upload_id)
            except UploadError as exc:
                # Keep the original upload for review; an unavailable evaluation
                # must not be treated as a passed quality check.
                chunk.evaluate_result = {"error": str(exc), "http_status": exc.status_code}

        if original_captures is not None and chunk.state != STATE_DONE:
            # Stop creating more parts after an unresolved original receipt.
            # Recovery must review the whole fixed SID, without a new identity.
            break

    result = UploadResult(
        session_id=session_id,
        org_key=org_key,
        task_id=task_id,
        chunks=chunks,
        total_size_bytes=total_size,
        total_duration_ms=total_duration,
        recorded_at=recorded_at,
    )

    if any(quality_required) and not require_perfect:
        blocked = []
        for offset, chunk in enumerate(chunks):
            if (not quality_required[offset] or chunk.state != STATE_DONE
                    or is_perfect(chunk.evaluate_result)):
                continue
            summary = summarize_checks(chunk.evaluate_result)
            failed = ", ".join(str(c.get("id") or "sem identificação")
                               for c in summary["failures"])
            http = (chunk.evaluate_result or {}).get("http_status")
            http_detail = f" (HTTP {http})" if type(http) is int else ""
            chunk.error = (f"Avaliação reprovada: {failed}." if summary["valid"] else
                           f"Avaliação inconclusiva{http_detail}; envio preservado para revisão.")
            chunk.state = STATE_FAILED
            blocked.append(chunk)
        if blocked:
            _update_persisted_journals(
                state=STATE_QUARANTINE, phase="evaluation_review", finalized=False,
                evaluation_required=True, evaluation_verified=False,
                error="; ".join(dict.fromkeys(c.error for c in blocked)))
            return result

    # --- política perfect-only (se requisitada) ----------------------------
    # Evaluate the whole group once before cleanup. An inconclusive sibling
    # preserves the group for review. Valid rejection still uses the operator's
    # cleanup policy, with durable terminal journals before any remote write.
    if require_perfect:
        all_perfect = True
        evaluation_uncertain = False
        rejected: list[ChunkResult] = []
        for chunk in chunks:
            if not chunk.upload_id or chunk.state != STATE_DONE:
                all_perfect = False
                evaluation_uncertain = True
                continue
            try:
                if on_progress:
                    on_progress("evaluate", STATE_COMPLETING, 1)
                chunk.evaluate_result = evaluate_upload(session, chunk.upload_id)
            except UploadError as exc:
                chunk.evaluate_result = {"error": str(exc)}
                chunk.error = str(exc)
                chunk.state = STATE_QUARANTINE
                evaluation_uncertain = True
                all_perfect = False
                continue
            if not is_perfect(chunk.evaluate_result):
                summary = summarize_checks(chunk.evaluate_result)
                failed = ", ".join(c.get("id", "?") for c in summary["failures"])
                print(f"    [perfect] chunk {chunk.chunk_index} REPROVADO: {failed}")
                chunk.state = STATE_FAILED
                chunk.error = f"evaluate reprovado: {failed}"
                all_perfect = False
                rejected.append(chunk)
        if evaluation_uncertain:
            for chunk in chunks:
                _save_quality_checkpoint(
                    chunk, state=STATE_QUARANTINE, phase="evaluation_review", finalized=False,
                    evaluation_required=True, evaluation_verified=False,
                    error="Avaliação inconclusiva; recibos da sessão preservados para revisão.")
            return result
        if rejected:
            # Commit the whole group's decision before compensation. A crash or
            # failed DELETE must never make recovery evaluate a deleted receipt.
            for chunk in chunks:
                _save_quality_checkpoint(
                    chunk, state=STATE_QUARANTINE, phase="quality_rejected", finalized=False,
                    evaluation_required=True, evaluation_verified=False,
                    error=chunk.error or "Sessão reprovada na política de qualidade perfeita.",
                    remote_fail_attempted=False, remote_fail_confirmed=False,
                    upload_delete_attempted=False, upload_delete_confirmed=False,
                    session_delete_attempted=False, session_delete_confirmed=False)
            for chunk in rejected:
                for phase, attempted, confirmed, operation in (
                    ("fail", "remote_fail_attempted", "remote_fail_confirmed",
                     lambda current=chunk: fail_upload(session, current.upload_id, current.error or "quality rejected")),
                    ("delete-upload", "upload_delete_attempted", "upload_delete_confirmed",
                     lambda current=chunk: delete_upload(session, current.upload_id)),
                ):
                    if on_progress:
                        on_progress(phase, STATE_FAILED, 1)
                    _save_quality_checkpoint(chunk, **{attempted: True})
                    try:
                        operation()
                    except UploadError as exc:
                        _save_quality_checkpoint(chunk, **{confirmed: False, phase.replace("-", "_") + "_status": exc.status_code})
                    else:
                        _save_quality_checkpoint(chunk, **{confirmed: True, phase.replace("-", "_") + "_status": 200 if phase == "fail" else 204})
            if on_progress:
                on_progress("delete-session", STATE_FAILED, 1)
            for chunk in chunks:
                _save_quality_checkpoint(chunk, session_delete_attempted=True)
            try:
                delete_session(session, org_key, session_id)
            except UploadError as exc:
                for chunk in chunks:
                    _save_quality_checkpoint(chunk, session_delete_confirmed=False, session_delete_status=exc.status_code)
            else:
                for chunk in chunks:
                    _save_quality_checkpoint(chunk, session_delete_confirmed=True, session_delete_status=204)
            return result
        if all_perfect:
            print(f"    [perfect] todos os {len(chunks)} chunk(s) passaram no evaluate")
        else:
            # não finaliza sessão reprovada
            return result

    if persist_sidecar and any(quality_required):
        for offset, chunk in enumerate(chunks):
            if (not quality_required[offset] or chunk.state != STATE_DONE
                    or not is_perfect(chunk.evaluate_result)):
                continue
            stored = load_sidecar(session_id, chunk.chunk_index)
            if stored:
                stored.update(evaluation_required=True, evaluation_verified=True)
                save_sidecar(stored)

    # --- 5. Finalize sessão ------------------------------------------------
    # O app só finaliza quando todos os chunks chegaram (expected_chunk_count).
    if finalize and all(c.state == STATE_DONE for c in chunks):
        _update_persisted_journals(
            state=STATE_COMPLETING, phase="finalizing", finalized=False)
        finalize_progress_attempt = 0

        def _finalize() -> int:
            nonlocal finalize_progress_attempt
            finalize_progress_attempt += 1
            if on_progress:
                on_progress("finalize", STATE_COMPLETING, finalize_progress_attempt)
            ok, status = _finalize_session(
                session, org_key, session_id,
                expected_chunk_count=len(chunks),
            )
            result.finalize_status = status
            if not ok:
                raise UploadError(
                    f"finalize da sessão falhou ({status}); esperado HTTP 204",
                    status_code=status,
                    transient=(status == -1 or status in (408, 429) or status >= 500),
                    phase="finalize",
                )
            return status

        try:
            _with_retry(_finalize, max_retries=max_retries,
                        retry_backoff=retry_backoff, label="finalize")
            result.finalized = True
            _update_persisted_journals(
                state=STATE_DONE, phase="done", finalized=True, error=None)
        except UploadError as exc:
            result.finalized = False
            _update_persisted_journals(
                state=(STATE_COMPLETING if exc.retryable else STATE_FAILED),
                phase="finalize", finalized=False, error=str(exc),
            )
            # O chunk chegou, mas a sessão não está entregue. Preserve a causa
            # para o orquestrador repetir a conta em vez de aceitar falso ok.
            if chunks:
                chunks[-1].error = str(exc)

    if (persist_sidecar and all(c.state == STATE_DONE for c in chunks)
            and (not finalize or result.finalized)):
        for chunk in chunks:
            stored = load_sidecar(session_id, chunk.chunk_index) or {}
            if stored.get("finalize_requested") and stored.get("finalized") is not True:
                continue
            _remove_sidecar_archive(session_id, chunk.chunk_index)

    return result


# --- Compatibilidade: upload_video (chunk único, interface antiga) ------------

def upload_video(
    session: Any,
    video_path: str | Path,
    org_key: str,
    task_id: str | None = None,
    session_id: str | None = None,
    log_id: str | None = None,
    content_type: str = "video/mp4",
    chunk_index: int = 0,
    timeout_blob: int = 300,
) -> UploadResult:
    """Compatibilidade com a interface antiga de chunk único.

    Usa upload_session internamente. Se log_id for fornecido, extrai o
    session_id e chunk_index dele (formato: {sessionId}_{chunkIndex}).

    Devolve `UploadResult` (use .upload_id, .blob_path, .log_id para os
    valores do primeiro/único chunk).
    """
    # Se log_id fornecido, decompor em session_id + chunk_index.
    if log_id and not session_id:
        parts = log_id.rsplit("_", 1)
        if len(parts) == 2 and parts[0] and parts[1].isdigit():
            session_id = parts[0]
            chunk_index = int(parts[1])

    return upload_session(
        session=session,
        video_path=video_path,
        org_key=org_key,
        task_id=task_id,
        session_id=session_id,
        content_type=content_type,
        timeout_blob=timeout_blob,
        finalize=True,
        evaluate=False,
        chunk_index_start=chunk_index,
    )


# --- Fila de retomada (mimica pumpUploadsScreen do app) ------------------------

def enqueue_upload(
    session: Any,
    video_path: str | Path,
    org_key: str,
    task_id: str | None = None,
    session_id: str | None = None,
    content_type: str = "video/mp4",
    **kwargs: Any,
) -> UploadResult:
    """Executa o upload e persiste o sidecar — o upload entra na "fila".

    Depois de enfileirar, `pump_pending()` pode ser chamado para tentar de novo
    os chunks que ficaram em retry_late/loss/failed (comportamento do app de
    retomar uploads pendentes ao reabrir / voltar online).
    """
    kwargs.setdefault("persist_sidecar", True)
    return upload_session(
        session=session,
        video_path=video_path,
        org_key=org_key,
        task_id=task_id,
        session_id=session_id,
        content_type=content_type,
        **kwargs,
    )


def _pending_recovery_stage(item: dict[str, Any]) -> str:
    """Validate a selected journal before effects; shared with recovery UI."""
    index = item.get("chunk_index", 0)
    resumes = item.get("crash_resumes", 0)
    attempts = item.get("attempts", 0)
    expected = item.get("expected_chunk_count", 1)
    if (not journal_flags_valid(item) or type(index) is not int or index < 0
            or type(resumes) is not int or resumes < 0
            or type(attempts) is not int or attempts < 0
            or type(expected) is not int or expected < 1):
        raise UploadError("Registro de retomada inválido; os arquivos foram preservados para revisão.",
                          transient=False, phase="recovery")
    _journal_flag(item, "register_first")
    _journal_flag(item, "suppress_per_chunk_catbear")
    receipt = _journal_has_remote_receipt(item)
    if _journal_creation_uncertain(item):
        raise _uncertain_creation_error()
    phase = item.get("phase", "")
    upload_id = item.get("upload_id")
    has_id = isinstance(upload_id, str) and bool(upload_id.strip())
    complete_phases = {"transport_done", "completing", "complete"}
    finalize_phases = {"awaiting_finalize", "finalize", "finalizing"}
    if has_id and phase in complete_phases:
        stage = "complete"
    elif has_id and item.get("finalize_requested") is True and (
            phase in finalize_phases or is_pending_finalization(item)):
        stage = "finalize"
    elif has_id and item.get("transport_artifact") == "sidecar" and phase in {
            "registered", "sas", "sas_ready", "sas_reminted", "transport", "sidecar_preflight"}:
        _sidecar_resume_payload(item)
        stage = "sidecar"
    else:
        if receipt or phase in complete_phases | finalize_phases:
            # A post-transport state without a receipt cannot safely become
            # another create/PUT. Unknown receipt phases need explicit review.
            raise _existing_receipt_error()
        _journal_recorded_at(item)
        stage = "upload"
    _sidecar_filename(item.get("session_id"), index)
    return stage


@pending_protocol
def pump_pending(
    session: Any,
    state: str | None = None,
    max_retries: int = 3,
    retry_backoff: float = 1.5,
    account_email: str | None = None,
    required_org_key: str | None = None,
    session_ids: set[str] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Processa sidecars pendentes (retry-late / loss / failed) e tenta de novo.

    Mimica `pumpUploadsScreen` / "SeeUploads resume when you're back online" do
    app: os uploads que falharam de forma transiente ficam persistidos e são
    retomados numa execução posterior.

    Para cada sidecar pendente, chama upload_session com o MESMO session_id e
    chunk_index, preservando a identidade do envio. Se o blob já chegou, retoma
    somente complete/finalize, sem reenviar o vídeo. O vídeo local precisa existir
    para retomadas anteriores ao transporte. Quando account_email é informado,
    somente journals daquela conta são processados. Com required_org_key,
    pendências de outras organizações ficam preservadas sem reenvio.

    Recibos conhecidos usam as rotas diretas complete/finalize. Um recibo em
    fase anterior sem driver de retomada compatível é preservado e recusado
    antes de checkpoints; não é convertido em um envio novo. Nessas rotas
    diretas, um horário histórico ausente é conservado sem gerar outro clock.

    Retorna a lista de sidecars atualizados (estado final de cada tentativa).
    """
    on_progress = kwargs.get("on_progress")
    if on_progress is not None and not callable(on_progress):
        raise UploadError("Callback de retomada inválido.", transient=False, phase="preflight")
    all_journals = list_sidecars()
    if state is None:
        pending = [s for s in all_journals if (isinstance(s.get("state"), str) and s.get("state") in TRANSIENT_STATES)
                   or s.get("state") == STATE_LOSS or is_pending_finalization(s)]
    else:
        pending = [item for item in all_journals if item.get("state") == state]
    session_email = getattr(session, "email", None)
    if isinstance(session_email, str) and session_email.strip():
        if account_email is not None and account_email.strip().casefold() != session_email.strip().casefold():
            raise UploadError("A conta da retomada difere da sessão autenticada.", transient=False)
        account_email = session_email
    if account_email is not None:
        wanted_email = account_email.strip().casefold()
        pending = [item for item in pending
                   if str(item.get("account_email") or "").strip().casefold()
                   == wanted_email]

    if required_org_key is not None:
        pending = [item for item in pending
                   if item.get("org_key") == required_org_key]
    if session_ids is not None:
        pending = [item for item in pending if item.get("session_id") in session_ids]

    # Reject malformed selected journals before changing any checkpoint or
    # calling a service. Coercion can turn bool/string counters into a valid
    # chunk and resume a different operation than the persisted one.
    selected_chunks: set[tuple[str, int]] = set()
    for item in pending:
        recovery_stage = _pending_recovery_stage(item)
        if recovery_stage == "sidecar" and account_email is None:
            raise UploadError("A retomada do ZIP requer uma conta autenticada identificada.",
                              transient=False, phase="recovery")
        index = item.get("chunk_index", 0)
        identity = (item["session_id"], index)
        if identity in selected_chunks:
            raise UploadError("Registros de retomada duplicados; os arquivos foram preservados para revisão.",
                              transient=False, phase="recovery")
        selected_chunks.add(identity)

    # A sibling marked done/finalized can be absent from the pending filter.
    # Validate its receipt before spending another chunk's resume budget.
    selected_sessions = {item[0] for item in selected_chunks}
    for item in all_journals:
        if item.get("session_id") in selected_sessions:
            if item.get("finalized") is True:
                if not journal_delivery_confirmed(item):
                    raise _existing_receipt_error()
            elif item.get("state") == STATE_DONE:
                _pending_recovery_stage(item)

    updated: list[dict[str, Any]] = []
    touched_sessions: set[str] = set()
    for sidecar in pending:
        sid = str(sidecar.get("session_id") or "")
        idx = int(sidecar.get("chunk_index") or 0)
        touched_sessions.add(sid)
        resumes = int(sidecar.get("crash_resumes") or 0)
        if resumes >= MAX_CRASH_RESUMES:
            sidecar.update({
                "state": STATE_QUARANTINE,
                "phase": "crash_resume_limit",
                "error": (
                    f"limite de {MAX_CRASH_RESUMES} retomadas após reinício atingido"),
            })
            save_sidecar(sidecar)
            updated.append(sidecar)
            continue
        sidecar["crash_resumes"] = resumes + 1
        save_sidecar(sidecar)

        upload_id = str(sidecar.get("upload_id") or "")
        phase = str(sidecar.get("phase") or "")
        try:
            if upload_id and sidecar.get("finalize_requested") is True and (
                    phase in {"awaiting_finalize", "finalize", "finalizing"}
                    or is_pending_finalization(sidecar)):
                # A crash can leave every completed chunk in done before the
                # session-level checkpoint is written. Preserve its receipt
                # and continue only evaluation/finalize, without local media.
                if is_pending_finalization(sidecar):
                    sidecar.update(state=STATE_COMPLETING, phase="awaiting_finalize")
                    save_sidecar(sidecar)
                updated.append(sidecar)
                continue
            # Blob já chegou: não repete vídeo nem cria outro registro.
            if upload_id and phase in {
                    "transport_done", "completing", "complete"}:
                complete_progress_attempt = 0
                def _resume_complete(
                    current_upload_id: str = upload_id,
                    current_size: int = int(sidecar.get("size_bytes") or 0),
                    suppress: bool = _journal_flag(sidecar, "suppress_per_chunk_catbear"),
                ) -> dict[str, Any]:
                    nonlocal complete_progress_attempt
                    complete_progress_attempt += 1
                    if on_progress:
                        on_progress("complete", STATE_COMPLETING, complete_progress_attempt)
                    return complete_upload(
                        session, current_upload_id, current_size,
                        suppress_per_chunk_catbear=suppress,
                        session_complete=False,
                        _native_response_schema=_journal_flag(sidecar, "native_response_schema"),
                    )

                complete_data, attempts = _with_retry(
                    _resume_complete,
                    max_retries=max_retries,
                    retry_backoff=retry_backoff,
                    label="resume complete",
                )
                sidecar.update({
                    "state": (STATE_COMPLETING if sidecar.get("finalize_requested")
                              else STATE_DONE),
                    "phase": ("awaiting_finalize"
                              if sidecar.get("finalize_requested") else "done"),
                    "attempts": attempts,
                    "error": None,
                    "raw_complete": complete_data,
                })
                save_sidecar(sidecar)
                if not sidecar.get("finalize_requested"):
                    _remove_sidecar_archive(sid, idx)
                updated.append(sidecar)
                continue

            if _pending_recovery_stage(sidecar) == "sidecar":
                def _save_zip_checkpoint(**updates: Any) -> None:
                    sidecar.update(updates)
                    sidecar["updated_at"] = _iso_now()
                    save_sidecar(sidecar)

                chunk = _upload_single_chunk(
                    session=session, video_path=Path(str(sidecar.get("local_video_path") or "")),
                    org_key=sidecar["org_key"], session_id=sid, chunk_index=idx,
                    task_id=sidecar.get("task_id"), content_type="video/mp4",
                    timeout_blob=kwargs.get("timeout_blob", 300),
                    recorded_at=sidecar["recorded_at"], device_meta=None,
                    platform_meta=None, video_meta=None, network_meta=None,
                    max_retries=max_retries, retry_backoff=retry_backoff,
                    suppress_per_chunk_catbear=_journal_flag(sidecar, "suppress_per_chunk_catbear"),
                    register_first=True, sidecar=True,
                    fail_on_error=kwargs.get("fail_on_error", True),
                    checkpoint=_save_zip_checkpoint, _resume_sidecar_row=sidecar,
                    on_progress=on_progress,
                )
                if chunk.state == STATE_DONE:
                    sidecar.update(state=STATE_COMPLETING if sidecar.get("finalize_requested") else STATE_DONE,
                                   phase="awaiting_finalize" if sidecar.get("finalize_requested") else "done",
                                   raw_complete=chunk.raw_complete, error=None)
                    save_sidecar(sidecar)
                    if not sidecar.get("finalize_requested"):
                        _remove_sidecar_archive(sid, idx)
                elif chunk.error:
                    sidecar.update(state=chunk.state, error=chunk.error)
                    save_sidecar(sidecar)
                updated.append(sidecar)
                continue

            filename = str(sidecar.get("filename") or f"{sid}_{idx}.mp4")
            local_name = str(sidecar.get("local_video_path") or filename)
            video = Path(local_name)
            if not video.exists():
                alt = config.VIDEOS_DIR / filename
                if alt.exists():
                    video = alt
                else:
                    sidecar.update({
                        "state": STATE_LOSS,
                        "phase": "missing_local_file",
                        "error": "arquivo local ausente (loss record)",
                    })
                    save_sidecar(sidecar)
                    updated.append(sidecar)
                    continue

            archive_name = str(sidecar.get("sidecar_data_path") or "")
            archive = Path(archive_name) if archive_name else None
            sidecar_payload = archive.read_bytes() if archive and archive.exists() else None
            require_sidecar = bool(sidecar.get("sidecar_data_path"))
            if require_sidecar and sidecar_payload is None:
                raise UploadError(
                    "arquivo .data.zip persistido desapareceu",
                    transient=False, phase="preflight")

            result = upload_session(
                session=session,
                video_path=video,
                org_key=str(sidecar.get("org_key") or ""),
                task_id=sidecar.get("task_id"),
                session_id=sid,
                recorded_at=str(sidecar.get("recorded_at") or "") or None,
                content_type="video/mp4",
                finalize=False,
                normalize=False,
                register_first=_journal_flag(sidecar, "register_first"),
                suppress_per_chunk_catbear=_journal_flag(sidecar, "suppress_per_chunk_catbear"),
                max_retries=max_retries,
                retry_backoff=retry_backoff,
                persist_sidecar=True,
                sidecar=require_sidecar,
                sidecar_data=sidecar_payload,
                chunk_index_start=idx,
                evaluate=bool(sidecar.get("evaluation_required", bool(sidecar.get("campaign_context")))
                              or kwargs.get("evaluate")),
                **{k: v for k, v in kwargs.items() if k in (
                    "fail_on_error", "timeout_blob",
                    "device_meta", "platform_meta", "video_meta",
                    "network_meta", "profile",
                    "on_progress",
                )},
            )
            chunk = result.chunks[0]
            current = load_sidecar(sid, idx) or sidecar
            if chunk.state == STATE_DONE and current.get("finalize_requested"):
                current.update(state=STATE_COMPLETING, phase="awaiting_finalize")
                save_sidecar(current)
            updated.append(current)
        except UploadError as exc:
            sidecar.update({
                "state": (STATE_QUARANTINE if exc.review_required else
                          STATE_RETRY_LATE if exc.retryable else STATE_FAILED),
                "phase": "completion_review" if exc.review_required else exc.phase or phase or "resume",
                "error": str(exc),
            })
            save_sidecar(sidecar)
            updated.append(sidecar)

    # Finalize só quando todos os chunks da sessão já estiverem completos.
    for sid in touched_sessions:
        journals = [item for item in list_sidecars()
                    if str(item.get("session_id") or "") == sid]
        if required_org_key is not None and any(
                item.get("org_key") != required_org_key for item in journals):
            continue
        if not journals or not any(item.get("finalize_requested") for item in journals):
            continue
        if any(not journal_flags_valid(item) for item in journals):
            continue
        owners = {str(item.get("account_email") or "").strip().casefold()
                  for item in journals}
        organizations = {str(item.get("org_key") or "") for item in journals}
        if len(owners) != 1 or not all(owners) or len(organizations) != 1 or not all(organizations):
            continue
        if any(item.get("task_id") != journals[0].get("task_id")
               or item.get("campaign_context") != journals[0].get("campaign_context")
               for item in journals):
            continue
        if account_email is not None and owners != {account_email.strip().casefold()}:
            continue
        counts = [item.get("expected_chunk_count", 1) for item in journals]
        if any(type(count) is not int or count < 1 for count in counts) or len(set(counts)) != 1:
            continue
        expected = counts[0]
        ready = [item for item in journals if item.get("phase") in {
            "awaiting_finalize", "finalize", "finalizing", "done"}
            and item.get("state") in {STATE_COMPLETING, STATE_RETRY_LATE, STATE_DONE}
            and isinstance(item.get("upload_id"), str) and item["upload_id"].strip()]
        indices = [item.get("chunk_index") for item in ready]
        if (expected < 1 or len(ready) != expected or len(journals) != expected
                or any(type(index) is not int for index in indices)
                or set(indices) != set(range(expected))):
            continue

        # Recovery must not bypass the quality gate after complete, or after
        # a crash between evaluation and session finalization. Legacy campaign
        # journals without the new flag also require evaluation conservatively.
        evaluation_blocked = False
        for item in ready:
            required = item.get("evaluation_required", bool(item.get("campaign_context")))
            if not required or item.get("evaluation_verified") is True:
                continue
            try:
                if on_progress:
                    on_progress("evaluate", STATE_COMPLETING, 1)
                evaluation = evaluate_upload(session, str(item["upload_id"]))
                if not is_perfect(evaluation):
                    raise UploadError("Avaliação reprovada; envio preservado para revisão.",
                                      transient=False, phase="evaluate")
            except UploadError as exc:
                item.update(state=STATE_QUARANTINE, phase="evaluation_review", finalized=False,
                            evaluation_required=True, evaluation_verified=False, error=str(exc))
                evaluation_blocked = True
            else:
                item.update(evaluation_required=True, evaluation_verified=True)
            save_sidecar(item)
        if evaluation_blocked:
            continue

        org_key = str(ready[0].get("org_key") or "")
        finalize_progress_attempt = 0

        def _resume_finalize(
            current_org: str = org_key,
            current_sid: str = sid,
            current_expected: int = expected,
        ) -> int:
            nonlocal finalize_progress_attempt
            finalize_progress_attempt += 1
            if on_progress:
                on_progress("finalize", STATE_COMPLETING, finalize_progress_attempt)
            ok, status = _finalize_session(
                session, current_org, current_sid, current_expected)
            if not ok:
                raise UploadError(
                    f"finalize da sessão falhou ({status})",
                    status_code=status,
                    transient=(status == -1 or status in (408, 429) or status >= 500),
                    phase="finalize",
                )
            return status

        try:
            _with_retry(
                _resume_finalize, max_retries=max_retries,
                retry_backoff=retry_backoff, label="resume finalize")
        except UploadError as exc:
            for item in ready:
                item.update({
                    "state": STATE_COMPLETING if exc.retryable else STATE_FAILED,
                    "phase": "finalize", "finalized": False,
                    "error": str(exc),
                })
                save_sidecar(item)
        else:
            for item in ready:
                item.update({
                    "state": STATE_DONE, "phase": "done",
                    "finalized": True, "error": None,
                })
                save_sidecar(item)
                _remove_sidecar_archive(
                    sid, int(item.get("chunk_index") or 0))

    return [load_sidecar(str(item.get("session_id") or ""),
                         int(item.get("chunk_index") or 0)) or item
            for item in updated]
