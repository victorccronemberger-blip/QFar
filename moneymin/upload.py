"""
upload.py — Réplica do fluxo de upload do app Minute (com.bakerdata.minute).

Pipeline de 6 etapas (espelha o fluxo nativo do app Android):
  1. POST  /api/v1/uploads                                  -> registra o chunk
  2. POST  /api/v1/storage/sas/blobs                        -> SAS URLs do Azure
  3. PUT   <blob_url>  (x-ms-blob-type: BlockBlob)          -> bytes direto no Azure
  4. PATCH /api/v1/uploads/{id}/complete                    -> confirma conclusão
  5. POST  /api/v1/organizations/{org}/sessions/{sid}/finalize -> sessão elegível p/ catbear
  6. POST  /api/v1/uploads/{id}/evaluate  (opcional)         -> roda checklist de qualidade

Comportamento nativo adicional (observado no bundle do app e na spec):
  - PATCH /api/v1/uploads/{id}/fail          -> marca o upload como falho (error_message)
  - GET   /api/v1/uploads/{upload_id}        -> consulta status (upload_status)
  - suppress_per_chunk_catbear no PATCH complete -> suprime catbear por-chunk (multi-chunk)
  - auto-retry com backoff em falhas transientes (create/sas, transport, complete)
  - retry-late: após exaurir retries o chunk fica pendente p/ tentativa futura
  - loss record: arquivo local sumiu -> registra perda em vez de abortar
  - sidecar persistente (data/sidecars/<session_id>.json) + fila de retomada

A API nunca toca nos bytes do MP4 — eles vão direto pro Azure Blob Storage
(figcbapp.blob.core.windows.net). O registro é criado antes da emissão das URLs
SAS, como no `uploadRecordingImpl` da v1.22. Cada arquivo _N.mp4 é um chunk de
uma mesma sessionId.

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
from .atomic_io import load_json, save_bytes, save_json
from .device_profile import DeviceProfile, recorded_at_to_wall_ms
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

def default_device_meta() -> dict[str, Any]:
    """Metadados de dispositivo que o app nativo envia em meta.device.

    Capturado do app iOS real (req 072): envia APENAS {"model": "iPhone 13"}.
    NÃO envia systemName nem systemVersion no POST /uploads.
    """
    return {
        "model": config.NATIVE_DEVICE_MODEL,
    }


def default_platform_meta() -> dict[str, Any]:
    """Metadados de plataforma que o app nativo envia em meta.platform.

    Capturado do app iOS real (req 072): envia APENAS {"os": "ios"}.
    NÃO envia version no POST /uploads.
    """
    return {
        "os": config.NATIVE_PLATFORM_OS,
    }


def default_video_meta() -> dict[str, Any]:
    """Metadados de vídeo que o app nativo envia em meta.video.

    Shape nativo iOS: {height, path, rotationDeg, width} (path = {logId}.mp4).
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
              timeout: int = 300, resumable: bool | None = None) -> int:
    """Faz PUT dos bytes no Azure Blob Storage. Devolve o status HTTP.

    O transporte (e o fingerprint de rede) é do `transport.py`: com curl_cffi
    o MP4 usa o upload resumável do NSURLSession (Put Block 4MB + Put Block
    List — captura 06/08); o sidecar `.data.zip` vai com `resumable=False`
    (PUT BlockBlob único, como o app). Sem curl_cffi, PUT BlockBlob. Sucesso
    é 201 Created, não 200.

    Levanta UploadError se o Azure recusar (status != 201) ou em erro de rede.
    """
    try:
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
                   on_progress: Callable[[int, int, float], None] | None = None) -> int:
    """PUT do vídeo a partir do disco, sem uma cópia integral na RAM."""
    try:
        return transport.put_blob_file(blob_url, file_path,
                                       content_type=content_type,
                                       timeout=timeout,
                                       on_progress=on_progress)
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
    """handler_name do stream de vídeo (ex.: 'Core Media Video')."""
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
    """Reencoda o vídeo para o formato EGO nativo do app (1440x1080 4:3).

    O iPhone grava a ultra-wide em 1440x1080 (4:3); datasets/YouTube vêm em
    16:9 (1920x1080). O Catbear pontua melhor (clarity=great) o que replica a
    gravação iPhone: proporção 4:3, yuv420p limited-range, handler
    "Core Media Video" e sem tags de dataset.

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
    needs = (pix_fmt == "yuvj420p" or "core media" not in handler.lower()
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
        "-profile:v", "high", "-level", "4.0",
        "-vf", ("scale=1440:1080:force_original_aspect_ratio=increase,"
                "crop=1440:1080,"
                "scale=in_range=full:out_range=tv,format=yuv420p"),
        "-r", "30",
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-metadata:s:v:0", "handler_name=Core Media Video",
        "-c:a", "aac", "-ac", "1", "-ar", "48000",
        "-movflags", "+faststart", "-f", "mp4", str(out),
    ]
    try:
        res = _run_ffmpeg(cmd)
    except FileNotFoundError as exc:
        raise UploadError(
            "vídeo precisa de reencode para formato iPhone (1440x1080 yuv420p), "
            "mas ffmpeg não está instalado — instale ffmpeg") from exc
    if res.returncode != 0:
        raise UploadError(
            f"falha ao normalizar vídeo para formato iPhone: {res.stderr.strip()[:300]}")
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
        "-profile:v", "high", "-level", "4.0",
        "-vf", ("scale=1440:1080:force_original_aspect_ratio=increase,"
                "crop=1440:1080,"
                "scale=in_range=full:out_range=tv,format=yuv420p"),
        "-r", "30",
        "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-metadata:s:v:0", "handler_name=Core Media Video",
        "-c:a", "aac", "-ac", "1", "-ar", "48000",
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
    detail = _error_detail(text)
    message = f"{phase} falhou ({status}): {detail}"
    if blocked:
        message += f" [bloqueio: {blocked}]"
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
        raise UploadError(f"PATCH /fail falhou ({status}): {text[:500]}")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise UploadError(f"resposta /fail não-JSON: {text[:300]}") from exc


def delete_upload(session: Any, upload_id: str) -> bool:
    """DELETE /api/v1/uploads/{upload_id} — remove o upload do backend.

    Usado pela política perfect-only: se o evaluate reprovar, o registro de
    upload é apagado (rows + referência). Sucesso = 204 No Content.
    Retorna True se deletado.
    """
    status, text = session.request("DELETE", f"/api/v1/uploads/{upload_id}")
    if status == 204:
        return True
    raise UploadError(f"DELETE /uploads/{upload_id} falhou ({status}): {text[:300]}")


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
        f"DELETE /sessions/{session_id} falhou ({status}): {text[:300]}")


def get_upload(session: Any, upload_id: str) -> dict[str, Any]:
    """GET /api/v1/uploads/{upload_id} — consulta o status do upload.

    O app nativo consulta o registro para saber o estado (`upload_status`).
    Devolve o dict com campos camelCase (uploadId, sessionId, logId, status,
    durationMs, recordedAt, ...). Levanta UploadError se não achar (404).
    """
    status, text = session.request("GET", f"/api/v1/uploads/{upload_id}")
    if status != 200:
        raise UploadError(f"GET /uploads/{upload_id} falhou ({status}): {text[:500]}")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise UploadError(f"resposta /uploads/{upload_id} não-JSON: {text[:300]}") from exc


def complete_upload(
    session: Any,
    upload_id: str,
    size_bytes: int,
    *,
    suppress_per_chunk_catbear: bool = False,
) -> dict[str, Any]:
    """Confirma um blob já enviado; operação reutilizável após reinício."""
    body: dict[str, Any] = {"size_bytes": int(size_bytes)}
    if suppress_per_chunk_catbear:
        body["suppress_per_chunk_catbear"] = True
    status, text, response_headers = _session_request(
        session, "PATCH", f"/api/v1/uploads/{upload_id}/complete", body)
    if status == 409:
        try:
            conflict = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            conflict = {}
        normalized = json.dumps(conflict, ensure_ascii=False).casefold()
        if any(word in normalized for word in (
                '"completed"', '"complete"', '"done"')):
            return conflict if isinstance(conflict, dict) else {}
        try:
            current = get_upload(session, upload_id)
        except UploadError:
            current = {}
        current_status = str(
            current.get("status") or current.get("upload_status") or ""
        ).casefold()
        if current_status in {"completed", "complete", "done"}:
            return current
    if status not in (200, 204):
        raise _http_upload_error(
            "PATCH /complete", status, text, response_headers)
    try:
        parsed = json.loads(text) if text.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- Sidecar persistente + fila (mimica recording.saveBody / pumpUploads) ------

def sidecars_dir() -> Path:
    """Diretório onde ficam os sidecars de upload (`data/sidecars/`)."""
    d = config.MEDIA_DATA_DIR / "sidecars"
    d.mkdir(parents=True, exist_ok=True)
    return d


MAX_CRASH_RESUMES = 3


def _sidecar_path(session_id: str, chunk_index: int = 0) -> Path:
    suffix = "" if chunk_index == 0 else f"__{chunk_index}"
    return sidecars_dir() / f"{session_id}{suffix}.json"


def _sidecar_archive_path(session_id: str, chunk_index: int = 0) -> Path:
    suffix = "" if chunk_index == 0 else f"__{chunk_index}"
    return sidecars_dir() / f"{session_id}{suffix}.data.zip"


def _remove_sidecar_archive(session_id: str, chunk_index: int = 0) -> None:
    """Remove somente o ZIP temporário depois da entrega confirmada."""
    try:
        _sidecar_archive_path(session_id, chunk_index).unlink(missing_ok=True)
    except OSError:
        pass


def save_sidecar(sidecar: dict[str, Any]) -> Path:
    """Persiste o estado de um upload em `data/sidecars/<session_id>.json`."""
    sid = sidecar.get("session_id") or sidecar.get("sessionId") or ""
    if not sid:
        raise UploadError("sidecar sem session_id")
    chunk_index = int(sidecar.get("chunk_index") or 0)
    path = _sidecar_path(sid, chunk_index)
    save_json(path, sidecar)
    return path


def load_sidecar(session_id: str, chunk_index: int = 0) -> dict[str, Any] | None:
    """Carrega um sidecar pelo session_id (None se não existir)."""
    path = _sidecar_path(session_id, chunk_index)
    if not path.exists():
        return None
    data = load_json(path, None)
    return data if isinstance(data, dict) else None


def list_sidecars(state: str | None = None) -> list[dict[str, Any]]:
    """Lista sidecars persistidos; filtra por estado se informado."""
    out: list[dict[str, Any]] = []
    for path in sorted(sidecars_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            continue
        if state is None or data.get("state") == state:
            out.append(data)
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
    missing = [column for column in required_columns if column not in header]
    if missing:
        raise UploadError(
            "sidecar CSV sem coluna(s): " + ", ".join(missing),
            transient=False,
        )
    index = header.index(timestamp_column)
    try:
        first = int(lines[1].split(",")[index])
        last = int(lines[-1].split(",")[index])
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
            metadata = json.loads(archive.read(f"{log_id}.metadata.json"))
            imu = archive.read(f"{log_id}.imu.csv")
            frames = archive.read(f"{log_id}.frames.csv")
    except UploadError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
        raise UploadError(f"sidecar inválido: {exc}", transient=False) from exc
    if not isinstance(metadata, dict):
        raise UploadError("metadata.json do sidecar não é objeto", transient=False)
    if str(metadata.get("logId") or metadata.get("id") or "") != log_id:
        raise UploadError("sidecar pertence a outro log_id", transient=False)
    declared = int(metadata.get("durationMs") or 0)
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
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
    }


def _conflict_upload(text: str) -> dict[str, Any]:
    """Normaliza 409: reutiliza registro conhecido ou falha de forma permanente."""
    try:
        body = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise UploadError(
            f"POST /uploads conflitante sem JSON: {text[:300]}",
            status_code=409, transient=False, phase="create",
        ) from exc
    if not isinstance(body, dict):
        raise UploadError(
            "POST /uploads conflitante com corpo inválido",
            status_code=409, transient=False, phase="create",
        )

    dictionaries = [body]
    dictionaries.extend(value for value in body.values() if isinstance(value, dict))
    upload_id = ""
    for item in dictionaries:
        upload_id = str(
            item.get("id") or item.get("uploadId") or item.get("upload_id") or "")
        if upload_id:
            break
    normalized = json.dumps(body, ensure_ascii=False).casefold()
    if "session-deleted" in normalized or "session_deleted" in normalized:
        raise UploadError(
            "POST /uploads: a sessão foi removida pelo servidor",
            status_code=409, transient=False, phase="create",
        )
    if not upload_id:
        raise UploadError(
            f"POST /uploads conflitante sem upload_id reutilizável: {text[:300]}",
            status_code=409, transient=False, phase="create",
        )
    if any(word in normalized for word in ('"completed"', '"complete"', '"done"')):
        action = "done"
    elif '"uploaded"' in normalized:
        action = "complete"
    else:
        action = "reuse-and-upload"
    merged = dict(body)
    merged["id"] = upload_id
    merged["_conflict_action"] = action
    return merged


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
    suppress_per_chunk_catbear: bool = False,
    fail_on_error: bool = True,
    sidecar: bool = True,
    sidecar_data: bytes | None = None,
    ego_meta: dict[str, Any] | None = None,
    register_first: bool = False,
    on_progress: Callable[..., None] | None = None,
    profile: DeviceProfile | None = None,
    checkpoint: Callable[..., None] | None = None,
) -> ChunkResult:
    """Executa registro -> SAS -> PUT Blob -> PATCH /complete.

    register_first=True replica a ordem confirmada no `uploadRecordingImpl` do
    app Android v1.22: POST /uploads -> SAS -> transporte -> complete. O modo
    False preserva o fluxo legado SAS -> transporte -> registro -> complete.

    Espelha o app nativo:
      - auto-retry com backoff nas etapas transientes (create/sas, transport);
      - PATCH complete com `suppress_per_chunk_catbear`;
      - se o registro foi criado e a confirmação falhar, marca `PATCH /fail`
        (fail_on_error) — comportamento `_failUpload` do app;
      - se o arquivo local sumir, devolve um ChunkResult em estado `loss`
        (loss record) em vez de abortar a sessão.
      - (sidecar=True) gera e sobe o `.data.zip` nativo junto com o MP4
        (imu.csv, frames.csv, metadata.json) e envia meta ego
        (source/timebase/cameras/codecActuals) — requisito do evaluate.

    Retorna o ChunkResult com os IDs e metadados.
    """
    file_size = video_path.stat().st_size if video_path.exists() else -1
    # Padrão nativo: logId = "{sessionId}_{chunk_index}".
    log_id = f"{session_id}_{chunk_index}"
    # Padrão nativo observado no SAS real: filename = "{logId}.mp4" (SEM _preview).
    # Com esse nome o servidor gera blob_path = .../{logId}/{logId}.mp4, que é
    # exatamente o caminho que o evaluate procura (video.ffprobe_ok).
    file_name = f"{log_id}.mp4"
    sidecar_file_name = f"{log_id}.data.zip"
    duration_ms = _probe_duration_ms(video_path) if video_path.exists() else 0

    def _emit(phase: str, state: str, attempt: int, **details: Any) -> None:
        if on_progress:
            on_progress(phase, state, attempt, **details)

    def _checkpoint(state: str, phase: str, **details: Any) -> None:
        if checkpoint:
            checkpoint(state=state, phase=phase, **details)

    _checkpoint(
        STATE_CREATING, "preflight", local_video_path=str(video_path.resolve()),
        size_bytes=file_size, duration_ms=duration_ms,
    )
    if not video_path.exists():
        return ChunkResult(
            upload_id="", chunk_index=chunk_index, log_id=log_id, blob_path="",
            size_bytes=-1, duration_ms=0, state=STATE_LOSS,
            error="arquivo local ausente antes do preflight",
        )
    if (file_size <= 0
            or not UPLOAD_MIN_DURATION_MS <= duration_ms <= UPLOAD_MAX_DURATION_MS):
        return ChunkResult(
            upload_id="", chunk_index=chunk_index, log_id=log_id, blob_path="",
            size_bytes=file_size, duration_ms=duration_ms, state=STATE_FAILED,
            error=(
                "preflight recusou vídeo vazio ou fora da duração permitida "
                f"({duration_ms} ms; esperado {UPLOAD_MIN_DURATION_MS}–"
                f"{UPLOAD_MAX_DURATION_MS} ms)"),
        )

    # --- 0. sidecar .data.zip (nativo) ------------------------------------
    sidecar_bytes: bytes | None = None
    sidecar_metadata: dict[str, Any] | None = None
    if sidecar and video_path.exists():
        try:
            if sidecar_data is not None:
                sidecar_bytes = sidecar_data
            else:
                probe = probe_video(video_path)
                # Sem device_meta/platform_meta: o metadata.json do sidecar usa o
                # device COMPLETO do iPhone real (iPhone14,5 + systemName +
                # systemVersion; platform com version) — o que o catbear espera.
                # Com `profile` (anti-colusão): calibração, clockOffset e
                # uptime DO APARELHO DA CONTA — cada upload tem identidade
                # de sensor própria.
                sidecar_kwargs: dict[str, Any] = {}
                if profile is not None:
                    wall_ms = recorded_at_to_wall_ms(recorded_at)
                    sidecar_kwargs = {
                        "calib": profile.calib,
                        "clock_offset_ns": profile.clock_offset_ns,
                        "frames_gop": profile.frames_gop,
                        "uptime_ns": (profile.uptime_ns_at(wall_ms)
                                      if wall_ms else None),
                    }
                    # device COMPLETO do perfil (modelo técnico da conta)
                    sidecar_kwargs["device_meta"] = {
                        "model": profile.sidecar_model,
                        "systemName": config.NATIVE_SIDECAR_SYSTEM_NAME,
                        "systemVersion": profile.sidecar_system_version,
                    }
                    sidecar_kwargs["platform_meta"] = {
                        "os": config.NATIVE_PLATFORM_OS,
                        "version": profile.sidecar_system_version,
                    }
                sidecar_bytes = build_sidecar_zip(
                    session_id=session_id,
                    chunk_index=chunk_index,
                    duration_ms=duration_ms,
                    recorded_at=recorded_at,
                    video_probe=probe,
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
                upload_id="", chunk_index=chunk_index, log_id=log_id,
                blob_path="", size_bytes=file_size, duration_ms=duration_ms,
                state=STATE_FAILED, error=str(error),
            )
        _checkpoint(
            STATE_CREATING, "sidecar_validated",
            sidecar_size_bytes=len(sidecar_bytes),
        )

    upload_id = ""
    create_data: dict[str, Any] = {}
    create_attempts = 1
    conflict_action = ""

    # --- 2. POST /api/v1/storage/sas/blobs --------------------------------
    def _sas() -> tuple[dict[str, Any], dict[str, str], str, str]:
        files: list[dict[str, Any]] = [
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
            sas_data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise UploadError(f"resposta SAS não-JSON: {text[:300]}") from exc
        signed_urls = sas_data.get("signed_urls") or []
        if not signed_urls:
            raise UploadError(f"nenhuma signed_url na resposta SAS: {text[:300]}")

        urls_by_filename: dict[str, str] = {}
        paths_by_filename: dict[str, str] = {}
        for entry in signed_urls:
            fn = entry.get("filename")
            url = entry.get("blob_url")
            if fn and url:
                urls_by_filename[fn] = url
                paths_by_filename[fn] = urllib.parse.urlparse(url).path.lstrip("/")
        if file_name not in urls_by_filename:
            raise UploadError(f"blob_url do mp4 ausente na resposta SAS: {text[:300]}")
        return sas_data, urls_by_filename, paths_by_filename[file_name], \
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
        _emit("sas", STATE_TRANSPORT, 1)
        try:
            sas_result, sas_attempts = _with_retry(
                _sas, max_retries=max_retries,
                retry_backoff=retry_backoff, label="SAS")
            sas_data, urls_by_filename, blob_path, sidecar_blob_path = sas_result
            blob_url = urls_by_filename[file_name]
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
    if not video_path.exists():
        # [upload] local file missing -> loss record
        loss = ChunkResult(
            upload_id="", chunk_index=chunk_index, log_id=log_id,
            blob_path=blob_path, size_bytes=-1, duration_ms=0,
            state=STATE_LOSS, attempts=1,
            error="arquivo local sumiu antes do transport (loss record)",
        )
        return loss

    file_size = video_path.stat().st_size

    def _transport() -> int:
        def _transport_progress(sent: int, total: int, elapsed: float) -> None:
            speed_bps = sent / elapsed if elapsed > 0 else 0.0
            remaining = max(0, total - sent)
            eta_s = remaining / speed_bps if speed_bps > 0 else None
            _emit(
                "transport", STATE_TRANSPORT, 1,
                sent_bytes=sent, total_bytes=total,
                speed_bps=speed_bps, eta_s=eta_s,
                percent=(sent * 100.0 / total if total else 100.0),
            )

        put_status = _put_blob_file(blob_url, video_path,
                                    content_type=content_type,
                                    timeout=timeout_blob,
                                    on_progress=_transport_progress)
        if put_status != 201:
            raise _http_upload_error(
                "PUT Blob", int(put_status), "status inesperado")
        if sidecar_bytes is not None:
            zip_url = urls_by_filename.get(sidecar_file_name, "")
            if zip_url:
                # mitm 06/08: sidecar é PUT BlockBlob único, não Put Block.
                zip_status = _put_blob(zip_url, sidecar_bytes,
                                       content_type="application/zip",
                                       timeout=timeout_blob,
                                       resumable=False)
                if zip_status != 201:
                    raise _http_upload_error(
                        "PUT sidecar", int(zip_status), "status inesperado")
        return put_status

    sas_remints = 0

    def _transport_resilient() -> int:
        """Uma SAS expirada é renovada; outros 403 continuam permanentes."""
        nonlocal sas_data, urls_by_filename, blob_path, sidecar_blob_path
        nonlocal blob_url, sas_attempts, sas_remints
        try:
            return _transport()
        except UploadError as exc:
            if exc.status_code not in (401, 403) or sas_remints >= 1:
                raise
            sas_remints += 1
            renewed, remint_attempts = _with_retry(
                _sas, max_retries=max_retries,
                retry_backoff=retry_backoff, label="SAS remint")
            sas_data, urls_by_filename, blob_path, sidecar_blob_path = renewed
            blob_url = urls_by_filename[file_name]
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
        nonlocal upload_id, create_data, create_attempts, conflict_action
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
        # device/platform/appVersion com getDeviceUploadMeta() — formato
        # simplificado verificado na captura iOS real (sessão 44df642a, 06/08):
        #   device   = {"model": "iPhone 13"}            (só model)
        #   platform = {"os": "ios"}                     (só os)
        #   appVersion = binaryAppVersion
        # O metadata.json DENTRO do sidecar permanece COMPLETO (iPhone14,5 +
        # systemName/systemVersion) — é o sidecar que alimenta os checks de
        # integridade (artifact.metadata_json.*); o POST usa o formato curto.
        # Formato curto do getDeviceUploadMeta (captura 072): só model/os.
        # Com perfil: o MODELO DO APARELHO DA CONTA (iPhone 12+, não todos 13).
        meta["device"] = {
            "model": (profile.device_model if profile
                      else config.NATIVE_DEVICE_MODEL),
        }
        meta["platform"] = {"os": config.NATIVE_PLATFORM_OS}
        meta["appVersion"] = config.APP_VERSION
        # O app real envia camera_source no meta (captura 072: "built-in").
        meta["camera_source"] = config.NATIVE_CAMERA_SOURCE

        upload_body: dict[str, Any] = {
            "session_id": session_id,
            "log_id": log_id,
            "duration_ms": duration_ms,
            "recorded_at": recorded_at,
            "meta": meta,
        }
        if task_id:
            upload_body["task_id"] = task_id

        def _create_upload() -> dict[str, Any]:
            # O app nativo envia o org_key como QUERY PARAM (/uploads?org_key=...),
            # não no corpo — replicar isso é o que associa o upload ao catbear/org.
            status, text, response_headers = _session_request(
                session, "POST", f"/api/v1/uploads?org_key={org_key}", upload_body)
            if status == 409:
                return _conflict_upload(text)
            # O schema diz response 201 (Created), não 200.
            if status not in (200, 201):
                raise _http_upload_error(
                    "POST /uploads", status, text, response_headers)
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                raise UploadError(f"resposta /uploads não-JSON: {text[:300]}") from exc

        create_data, create_attempts = _with_retry(
            _create_upload, max_retries=max_retries, retry_backoff=retry_backoff,
            label="create",
        )
        # UploadOut usa "id" como campo do uploadId.
        upload_id = (
            create_data.get("id")
            or create_data.get("uploadId")
            or create_data.get("upload_id")
        ) or ""
        if not upload_id:
            raise UploadError("uploadId ausente na resposta /uploads")
        conflict_action = str(create_data.get("_conflict_action") or "")
        _checkpoint(
            STATE_TRANSPORT, "registered", upload_id=upload_id,
            conflict_action=conflict_action, attempts=create_attempts,
        )

    # Ordem Android v1.22: registro -> SAS -> transporte.
    if register_first:
        _emit("create", STATE_CREATING, 1)
        try:
            _register()
        except UploadError as exc:
            create_attempts = exc.attempts or create_attempts
            failure_state = STATE_RETRY_LATE if exc.retryable else STATE_FAILED
            _checkpoint(failure_state, "create", error=str(exc),
                        attempts=create_attempts)
            return ChunkResult(
                upload_id="", chunk_index=chunk_index, log_id=log_id, blob_path=blob_path,
                size_bytes=file_size, duration_ms=duration_ms,
                state=failure_state, attempts=create_attempts, error=str(exc),
            )
        if conflict_action not in ("complete", "done"):
            sas_failure = _request_sas_stage()
            if sas_failure is not None:
                return sas_failure

    put_attempts = 1
    if conflict_action not in ("complete", "done"):
        try:
            _, put_attempts = _with_retry(
                _transport_resilient, max_retries=max_retries,
                retry_backoff=retry_backoff,
                label="transport",
            )
        except UploadError as exc:
            put_attempts = exc.attempts or 1
            failure_state = STATE_RETRY_LATE if exc.retryable else STATE_FAILED
            _checkpoint(
                failure_state, "transport", upload_id=upload_id,
                error=str(exc), attempts=put_attempts,
            )
            # Só uma falha permanente deve encerrar o registro no servidor.
            if (register_first and upload_id and fail_on_error
                    and not exc.retryable):
                try:
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
            failure_state = STATE_RETRY_LATE if exc.retryable else STATE_FAILED
            _checkpoint(failure_state, "create", error=str(exc),
                        attempts=create_attempts)
            return ChunkResult(
                upload_id="", chunk_index=chunk_index, log_id=log_id, blob_path=blob_path,
                size_bytes=file_size, duration_ms=duration_ms,
                state=failure_state, attempts=create_attempts, error=str(exc),
            )

    if conflict_action == "done":
        _checkpoint(STATE_DONE, "done", upload_id=upload_id)
        return ChunkResult(
            upload_id=str(upload_id), chunk_index=chunk_index, log_id=log_id,
            blob_path=blob_path, size_bytes=file_size, duration_ms=duration_ms,
            raw_create=create_data, state=STATE_DONE,
            attempts=max(sas_attempts, create_attempts),
            sidecar_blob_path=sidecar_blob_path,
            sidecar_size_bytes=len(sidecar_bytes) if sidecar_bytes else 0,
        )

    # --- 4. PATCH /api/v1/uploads/{id}/complete ----------------------------
    def _complete() -> dict[str, Any]:
        return complete_upload(
            session, upload_id, file_size,
            suppress_per_chunk_catbear=suppress_per_chunk_catbear,
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
        failure_state = STATE_COMPLETING if exc.retryable else STATE_FAILED
        _checkpoint(
            failure_state, "complete", upload_id=upload_id,
            error=error_msg, attempts=complete_attempts,
        )
        if fail_on_error and not exc.retryable:
            try:
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
        raise UploadError(f"evaluate falhou ({status}): {text[:500]}")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise UploadError(f"resposta evaluate não-JSON: {text[:300]}") from exc


def summarize_checks(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Resume o EvaluationResult: contagem pass/fail/skip e lista de falhas."""
    checks = evaluation.get("checks") or []
    counts = {"pass": 0, "fail": 0, "skip": 0}
    failures: list[dict[str, Any]] = []
    for c in checks:
        status = c.get("status")
        counts[status] = counts.get(status, 0) + 1
        if status == "fail":
            failures.append(c)
    return {"counts": counts, "failures": failures}


def is_perfect(evaluation: dict[str, Any]) -> bool:
    """True se o checklist do evaluate não tem nenhum check 'fail'.

    Checks 'skip' são aceitos (não aplicáveis à plataforma); qualquer 'fail'
    reprova a gravação (política perfect-only).
    """
    summary = summarize_checks(evaluation)
    return summary["counts"].get("fail", 0) == 0


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
    perfect = summary["counts"].get("fail", 0) == 0
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

def upload_session(
    session: Any,
    video_path: str | Path | list[str | Path],
    org_key: str,
    task_id: str | None = None,
    session_id: str | None = None,
    recorded_at: str | None = None,
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
    suppress_per_chunk_catbear: bool = False,
    fail_on_error: bool = True,
    persist_sidecar: bool = False,
    sidecar: bool = True,
    sidecar_data: bytes | None = None,
    ego_meta: dict[str, Any] | None = None,
    require_perfect: bool = False,
    normalize: bool = True,
    register_first: bool = False,
    on_progress: Callable[..., None] | None = None,
    profile: DeviceProfile | None = None,
    chunk_index_start: int = 0,
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
        max_retries   — auto-retry por etapa transiente (mimica o app; default 3).
        retry_backoff — multiplicador do backoff entre retries (default 1.5).
        suppress_per_chunk_catbear — envia true no PATCH complete (default False);
                      em sessões multi-chunk o app suprime o catbear por-chunk e
                      deixa a avaliação para o finalize da sessão.
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
                      o sidecar usa calibração/clockOffset/uptime próprios e o
                      meta usa o modelo daquele aparelho (anti-colusão). Sem
                      ele, mantém os valores reais do iPhone do operador.
        chunk_index_start — índice inicial do chunk; usado pela retomada para
                      manter a identidade original e evitar registros duplicados.
        on_progress   — callback(phase, state, attempt) por etapa.

    Devolve `UploadResult` com os IDs, metadados e status de finalize/evaluate.
    Levanta `UploadError` em qualquer falha.
    """
    # Normaliza video_path para lista de Paths.
    if isinstance(video_path, (str, Path)):
        paths = [Path(video_path)]
    else:
        paths = [Path(p) for p in video_path]

    for p in paths:
        if not p.exists():
            raise UploadError(f"vídeo não encontrado: {p}")

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
    if device_meta is None:
        device_meta = default_device_meta()
    if platform_meta is None:
        platform_meta = default_platform_meta()
    if video_meta is None:
        video_meta = default_video_meta()
    if network_meta is None:
        network_meta = default_network_meta()

    session_id = session_id or str(uuid.uuid4())
    # O app iOS real envia recordedAt com microssegundos (ex: .609000Z).
    # Usar .000Z faz o backend rejeitar como "não autêntico".
    if not recorded_at:
        import random
        now = time.time()
        # Microssegundos realistas (0-999999) em vez de .000000
        micros = random.randint(0, 999999)
        recorded_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{micros:06d}Z"

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
            stored["updated_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            save_sidecar(stored)

    for offset, vpath in enumerate(paths):
        idx = int(chunk_index_start) + offset
        journal: dict[str, Any] | None = None
        checkpoint_callback: Callable[..., None] | None = None
        if persist_sidecar:
            existing = load_sidecar(session_id, idx) or {}
            journal = {
                "schema_version": 2,
                "session_id": session_id,
                "org_key": org_key,
                "task_id": task_id,
                "chunk_index": idx,
                "log_id": f"{session_id}_{idx}",
                "filename": f"{session_id}_{idx}.mp4",
                "local_video_path": str(vpath.resolve()),
                "size_bytes": vpath.stat().st_size,
                "recorded_at": recorded_at,
                "state": STATE_CREATING,
                "phase": "queued",
                "attempts": int(existing.get("attempts") or 0),
                "crash_resumes": int(existing.get("crash_resumes") or 0),
                "register_first": bool(
                    existing.get("register_first", register_first)),
                "account_email": str(
                    existing.get("account_email")
                    or (profile.email if profile is not None else "")),
                "finalize_requested": bool(
                    existing.get("finalize_requested", finalize)),
                "expected_chunk_count": int(
                    existing.get("expected_chunk_count") or len(paths)),
                "suppress_per_chunk_catbear": bool(existing.get(
                    "suppress_per_chunk_catbear", suppress_per_chunk_catbear)),
                "updated_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            }
            if sidecar_data is not None:
                archive_path = _sidecar_archive_path(session_id, idx)
                save_bytes(archive_path, sidecar_data)
                journal["sidecar_data_path"] = str(archive_path.resolve())
            save_sidecar(journal)

            def _save_checkpoint(
                *, _journal: dict[str, Any] = journal,
                **updates: Any,
            ) -> None:
                _journal.update(updates)
                _journal["updated_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
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
            recorded_at=recorded_at,
            device_meta=device_meta,
            platform_meta=platform_meta,
            video_meta=video_meta,
            network_meta=network_meta,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            suppress_per_chunk_catbear=suppress_per_chunk_catbear,
            fail_on_error=fail_on_error,
            sidecar=sidecar,
            sidecar_data=sidecar_data,
            ego_meta=ego_meta,
            register_first=register_first,
            on_progress=on_progress,
            profile=profile,
            checkpoint=checkpoint_callback,
        )
        chunks.append(chunk)
        total_size += chunk.size_bytes
        total_duration += chunk.duration_ms

        if persist_sidecar:
            final_journal = _chunk_sidecar(
                chunk, session_id, org_key, task_id, recorded_at,
                f"{chunk.log_id}.mp4", chunk.state, chunk.attempts, chunk.error,
            )
            if journal is not None:
                final_journal = {**journal, **final_journal}
            final_journal["local_video_path"] = str(vpath.resolve())
            final_journal["phase"] = (
                "done" if chunk.state == STATE_DONE else final_journal.get("phase")
                or chunk.state)
            save_sidecar(final_journal)

        if evaluate and chunk.upload_id:
            try:
                chunk.evaluate_result = evaluate_upload(session, chunk.upload_id)
            except UploadError as exc:
                # Evaluate é opcional — não aborta a sessão se falhar.
                chunk.evaluate_result = {"error": str(exc)}

    result = UploadResult(
        session_id=session_id,
        org_key=org_key,
        task_id=task_id,
        chunks=chunks,
        total_size_bytes=total_size,
        total_duration_ms=total_duration,
        recorded_at=recorded_at,
    )

    # --- política perfect-only (se requisitada) ----------------------------
    # Roda evaluate em cada chunk. Se QUALQUER check falhar, cancela e deleta
    # (PATCH /fail + DELETE /uploads/{id} + DELETE /sessions/{sid}). A sessão
    # só é finalizada se TODOS os chunks forem perfeitos.
    if require_perfect:
        all_perfect = True
        for chunk in chunks:
            if not chunk.upload_id:
                all_perfect = False
                continue
            try:
                chunk.evaluate_result = evaluate_upload(session, chunk.upload_id)
            except UploadError as exc:
                chunk.evaluate_result = {"error": str(exc)}
                all_perfect = False
                continue
            if not is_perfect(chunk.evaluate_result):
                summary = summarize_checks(chunk.evaluate_result)
                failed = ", ".join(c.get("id", "?") for c in summary["failures"])
                print(f"    [perfect] chunk {chunk.chunk_index} REPROVADO: {failed}")
                chunk.state = STATE_FAILED
                chunk.error = f"evaluate reprovado: {failed}"
                all_perfect = False
                # marca fail + deleta upload e sessão (política do operador)
                try:
                    fail_upload(session, chunk.upload_id, f"reprovado: {failed}")
                except UploadError:
                    pass
                try:
                    delete_upload(session, chunk.upload_id)
                    print(f"    [perfect] upload {chunk.upload_id[:8]} DELETADO")
                except UploadError as exc:
                    print(f"    [perfect] falha ao deletar upload: {exc}")
                try:
                    delete_session(session, org_key, session_id)
                    print(f"    [perfect] sessão {session_id[:8]} DELETADA")
                except UploadError as exc:
                    print(f"    [perfect] falha ao deletar sessão: {exc}")
        if all_perfect:
            print(f"    [perfect] todos os {len(chunks)} chunk(s) passaram no evaluate")
        else:
            # não finaliza sessão reprovada
            return result

    # --- 5. Finalize sessão ------------------------------------------------
    # O app só finaliza quando todos os chunks chegaram (expected_chunk_count).
    if finalize and all(c.state == STATE_DONE for c in chunks):
        _update_persisted_journals(
            state=STATE_COMPLETING, phase="finalizing", finalized=False)

        def _finalize() -> int:
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


def pump_pending(
    session: Any,
    state: str | None = None,
    max_retries: int = 3,
    retry_backoff: float = 1.5,
    account_email: str | None = None,
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
    somente journals daquela conta são processados.

    Retorna a lista de sidecars atualizados (estado final de cada tentativa).
    """
    if state is None:
        pending = [s for s in list_sidecars() if s.get("state") in TRANSIENT_STATES
                   or s.get("state") == STATE_LOSS]
    else:
        pending = list_sidecars(state)
    if account_email is not None:
        wanted_email = account_email.strip().casefold()
        pending = [item for item in pending
                   if str(item.get("account_email") or "").strip().casefold()
                   == wanted_email]

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
            # Blob já chegou: não repete vídeo nem cria outro registro.
            if upload_id and phase in {
                    "transport_done", "completing", "complete"}:
                def _resume_complete(
                    current_upload_id: str = upload_id,
                    current_size: int = int(sidecar.get("size_bytes") or 0),
                    suppress: bool = bool(
                        sidecar.get("suppress_per_chunk_catbear")),
                ) -> dict[str, Any]:
                    return complete_upload(
                        session, current_upload_id, current_size,
                        suppress_per_chunk_catbear=suppress,
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
                register_first=bool(sidecar.get("register_first")),
                suppress_per_chunk_catbear=bool(
                    sidecar.get("suppress_per_chunk_catbear")),
                max_retries=max_retries,
                retry_backoff=retry_backoff,
                persist_sidecar=True,
                sidecar=require_sidecar,
                sidecar_data=sidecar_payload,
                chunk_index_start=idx,
                **{k: v for k, v in kwargs.items() if k in (
                    "fail_on_error", "evaluate", "timeout_blob",
                    "device_meta", "platform_meta", "video_meta",
                    "network_meta", "profile",
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
                "state": STATE_RETRY_LATE if exc.retryable else STATE_FAILED,
                "phase": exc.phase or phase or "resume",
                "error": str(exc),
            })
            save_sidecar(sidecar)
            updated.append(sidecar)

    # Finalize só quando todos os chunks da sessão já estiverem completos.
    for sid in touched_sessions:
        journals = [item for item in list_sidecars()
                    if str(item.get("session_id") or "") == sid]
        if not journals or not any(item.get("finalize_requested") for item in journals):
            continue
        expected = max(int(item.get("expected_chunk_count") or 1)
                       for item in journals)
        ready = [item for item in journals if item.get("phase") in {
            "awaiting_finalize", "finalize", "done"}]
        if len(ready) < expected:
            continue

        org_key = str(ready[0].get("org_key") or "")

        def _resume_finalize(
            current_org: str = org_key,
            current_sid: str = sid,
            current_expected: int = expected,
        ) -> int:
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
