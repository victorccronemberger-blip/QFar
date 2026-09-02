"""
transport.py — Transporte HTTP da réplica (fingerprint de rede).

O urllib do Python tem TLS (JA3/JA4) e headers visivelmente diferentes dos do
NSURLSession do app nativo — uma assinatura de "não é o app" independente do
corpo das requisições. Este módulo centraliza o transporte com dois backends:

  - `curl` (preferido) — `curl_cffi` (pip install curl_cffi), que impersona o
    TLS/HTTP2 do iOS Safari. Para o PUT no blob usa o transporte REAL do app
    (captura mitm 06/08): Put Block em blocos de 4MB + Put Block List, com os
    headers de upload resumável do NSURLSession (`Upload-Draft-Interop-Version:
    6`, `Upload-Complete: ?1`, `x-ms-blob-content-type` no blocklist).
  - `urllib` (fallback) — stdlib puro: PUT BlockBlob único (o que já passava
    23/23). Usado quando o curl_cffi não está instalado.

Seleção: env `MINUTE_TRANSPORT` = auto (default) | curl | urllib;
`MINUTE_IMPERSONATE` força o alvo de impersonate (ex.: safari260_ios).
O backend é resolvido uma vez e reportado por `kind()`.
Somente stdlib no caminho fallback; curl_cffi é opcional (como o boto3).
"""
from __future__ import annotations

import base64
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, tls

BLOCK_SIZE = 4 * 1024 * 1024  # 4MB — bloco do NSURLSession (captura 06/08)
BLOCK_MAX_ATTEMPTS = 3

_kind: str | None = None
_cffi: Any = None
_impersonate: str | None = None


def _resolve() -> None:
    """Escolhe o backend (uma vez). curl_cffi + impersonate iOS se disponível."""
    global _kind, _cffi, _impersonate
    if _kind is not None:
        return
    # auto: curl_cffi impersonate iOS + Put Block 4MB (app nativo, mitm 06/08).
    forced = os.environ.get("MINUTE_TRANSPORT", "auto").strip().lower()
    forced_imp = os.environ.get("MINUTE_IMPERSONATE", "").strip()
    _cffi = None
    _impersonate = None
    if forced in ("auto", "curl"):
        try:
            from curl_cffi import requests as cffi_requests
            try:
                from curl_cffi.requests import BrowserType
            except Exception:  # noqa: BLE001
                BrowserType = None  # type: ignore[assignment]
            if BrowserType is not None:
                # Alvos iOS em ordem de preferência (nomes variam por versão
                # do curl_cffi — usa o primeiro que existir no enum). Prioriza
                # o iOS 26 (contas rodam 26.5) e cai para iOS 18/17/15.
                candidates = [forced_imp] if forced_imp else [
                    "ios26", "ios26_0", "safari260_ios",
                    "ios18_4", "safari18_4_ios", "ios18_0",
                    "safari18_0_ios", "ios17_2", "safari17_2_ios",
                    "ios16_4", "safari17_0_ios",
                    "safari15_5_ios", "safari15_5",
                ]
                for candidate in candidates:
                    if candidate and hasattr(BrowserType, candidate):
                        _impersonate = candidate
                        break
            _cffi = cffi_requests
            _kind = "curl"
            return
        except Exception:  # noqa: BLE001 — sem curl_cffi, cai no urllib
            if forced == "curl":
                # forçado mas ausente: mantém curl para o erro falar o motivo
                _kind = "curl"
                return
    _kind = "urllib"


def kind() -> str:
    """Backend ativo: 'curl' (curl_cffi/impersonate iOS) ou 'urllib' (stdlib)."""
    _resolve()
    assert _kind is not None
    return _kind


def _headers_for(headers: dict[str, str] | None) -> dict[str, str]:
    """Headers base em forma de app iOS (urllib manda 'Accept-Encoding:
    identity' e 'Accept: */*' ausente — o app manda os dois)."""
    out = dict(headers or {})
    out.setdefault("Accept", "*/*")
    out.setdefault("Accept-Language", config.ACCEPT_LANGUAGE)
    return out


# --- HTTP genérico (API minute-api) ------------------------------------------

def http_request(method: str, url: str,
                 headers: dict[str, str] | None = None,
                 body: bytes | None = None, timeout: int = 30) -> tuple[int, bytes]:
    """Executa uma requisição e devolve (status, corpo_em_bytes).

    Nunca engole erro HTTP (HTTPError vira (code, body)); erro de rede levanta.
    """
    status, payload, _response_headers = http_request_detailed(
        method, url, headers=headers, body=body, timeout=timeout)
    return status, payload


def http_request_detailed(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
) -> tuple[int, bytes, dict[str, str]]:
    """Como :func:`http_request`, preservando também os headers da resposta."""
    _resolve()
    headers = _headers_for(headers)
    if _kind == "curl" and _cffi is not None:
        resp = _cffi.request(
            method.upper(), url, headers=headers, data=body,
            timeout=timeout, impersonate=_impersonate,
        )
        return resp.status_code, resp.content, {
            str(key): str(value) for key, value in dict(resp.headers).items()
        }
    req = urllib.request.Request(url, method=method.upper(), data=body)
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with tls.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict((exc.headers or {}).items())


# --- PUT no Azure Blob (etapa 2 do upload) ------------------------------------

def put_blob(blob_url: str, file_bytes: bytes,
             content_type: str = "video/mp4",
             timeout: int = 300,
             resumable: bool | None = None) -> int:
    """Sobe os bytes no Azure Blob e devolve o status HTTP (201 = ok).

    - vídeo (`resumable` True/None + curl): Put Block 4MB + Put Block List
      (NSURLSession, captura mitm 06/08, sessão d05dde0a).
    - sidecar (`resumable=False`): PUT BlockBlob único com
      `x-ms-blob-type: BlockBlob` — o app nativo sobe o `.data.zip` assim.
    - urllib: sempre BlockBlob único.
    """
    _resolve()
    use_blocks = (
        resumable is not False
        and _kind == "curl" and _cffi is not None
    )
    if use_blocks:
        return _put_blob_ios(blob_url, file_bytes, content_type, timeout)
    return _put_blob_blockblob(blob_url, file_bytes, content_type, timeout)


def put_blob_file(blob_url: str, file_path: str | Path,
                  content_type: str = "video/mp4",
                  timeout: int = 300,
                  on_progress: Callable[[int, int, float], None] | None = None) -> int:
    """Sobe um arquivo sem carregá-lo inteiro na memória.

    O caminho curl mantém o protocolo iOS de blocos de 4 MB. O fallback urllib
    entrega um stream com Content-Length, evitando picos de vários gigabytes
    quando a campanha envia vídeos longos para contas em paralelo.
    """
    _resolve()
    path = Path(file_path)
    if _kind == "curl" and _cffi is not None:
        return _put_blob_file_ios(
            blob_url, path, content_type, timeout, on_progress=on_progress)
    started = time.monotonic()
    with path.open("rb") as stream:
        req = urllib.request.Request(blob_url, method="PUT", data=stream)
        req.add_header("x-ms-blob-type", "BlockBlob")
        req.add_header("Content-Type", content_type)
        req.add_header("Content-Length", str(path.stat().st_size))
        with tls.urlopen(req, timeout=timeout) as resp:
            if on_progress:
                size = path.stat().st_size
                on_progress(size, size, time.monotonic() - started)
            return resp.status


def _put_blob_file_ios(blob_url: str, file_path: Path,
                       content_type: str, timeout: int,
                       on_progress: Callable[[int, int, float], None] | None = None) -> int:
    """Envia blocos em uma conexão persistente e repete só o bloco que falhou."""
    block_ids: list[str] = []
    total = file_path.stat().st_size
    started = time.monotonic()
    session_factory = getattr(_cffi, "Session", None)
    client = session_factory(impersonate=_impersonate) if session_factory else _cffi
    owns_client = session_factory is not None

    def _put(url: str, **kwargs):
        if not owns_client:
            kwargs["impersonate"] = _impersonate
        return client.put(url, **kwargs)

    try:
        with file_path.open("rb") as stream:
            block_index = 0
            sent = 0
            while chunk := stream.read(BLOCK_SIZE):
                block_id = base64.b64encode(
                    f"{block_index:08d}".encode("ascii")).decode("ascii")
                sep = "&" if "?" in blob_url else "?"
                block_url = f"{blob_url}{sep}comp=block&blockid={block_id}"
                last_error: Exception | None = None
                for attempt in range(1, BLOCK_MAX_ATTEMPTS + 1):
                    try:
                        resp = _put(
                            block_url, data=chunk,
                            headers={"Upload-Draft-Interop-Version": "6"},
                            timeout=timeout,
                        )
                    except Exception as exc:  # noqa: BLE001 — rede/TLS transitórios
                        last_error = exc
                    else:
                        if resp.status_code == 201:
                            last_error = None
                            break
                        error = RuntimeError(
                            f"Put Block {block_index} falhou ({resp.status_code}): "
                            f"{resp.text[:300]}")
                        # Erros permanentes não melhoram com espera/repetição.
                        if resp.status_code not in (408, 409, 429) and resp.status_code < 500:
                            raise error
                        last_error = error
                    if attempt < BLOCK_MAX_ATTEMPTS:
                        time.sleep(0.5 * (2 ** (attempt - 1)))
                if last_error is not None:
                    raise last_error
                block_ids.append(block_id)
                block_index += 1
                sent += len(chunk)
                if on_progress:
                    on_progress(sent, total, time.monotonic() - started)

        xml = "<BlockList>" + "".join(f"<Latest>{bid}</Latest>" for bid in block_ids) \
            + "</BlockList>"
        sep = "&" if "?" in blob_url else "?"
        commit_url = f"{blob_url}{sep}comp=blocklist"
        commit_error: Exception | None = None
        for attempt in range(1, BLOCK_MAX_ATTEMPTS + 1):
            try:
                resp = _put(
                    commit_url,
                    data=xml.encode("utf-8"),
                    headers={
                        "Content-Type": "application/xml",
                        "x-ms-blob-content-type": content_type,
                        "Upload-Draft-Interop-Version": "6",
                        "Upload-Complete": "?1",
                    },
                    timeout=timeout,
                )
            except Exception as exc:  # noqa: BLE001 — rede/TLS transitórios
                commit_error = exc
            else:
                if resp.status_code == 201:
                    return resp.status_code
                commit_error = RuntimeError(
                    f"Put Block List falhou ({resp.status_code}): {resp.text[:300]}")
                if resp.status_code not in (408, 409, 429) and resp.status_code < 500:
                    raise commit_error
            if attempt < BLOCK_MAX_ATTEMPTS:
                time.sleep(0.5 * (2 ** (attempt - 1)))
        assert commit_error is not None
        raise commit_error
    finally:
        if owns_client:
            client.close()


def _put_blob_blockblob(blob_url: str, file_bytes: bytes,
                        content_type: str, timeout: int) -> int:
    """PUT único com x-ms-blob-type: BlockBlob (sidecar nativo + fallback urllib)."""
    headers = {
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": content_type,
    }
    if _kind == "curl" and _cffi is not None:
        resp = _cffi.put(
            blob_url, data=file_bytes, headers=headers,
            timeout=timeout, impersonate=_impersonate,
        )
        if resp.status_code != 201:
            raise RuntimeError(
                f"PUT BlockBlob falhou ({resp.status_code}): {resp.text[:300]}")
        return resp.status_code
    req = urllib.request.Request(blob_url, method="PUT", data=file_bytes)
    for key, value in headers.items():
        req.add_header(key, value)
    with tls.urlopen(req, timeout=timeout) as resp:
        return resp.status


def _put_blob_ios(blob_url: str, file_bytes: bytes,
                  content_type: str, timeout: int) -> int:
    """Upload resumável do NSURLSession: blocos de 4MB + blocklist final."""
    block_ids: list[str] = []
    n = len(file_bytes)
    for off in range(0, n, BLOCK_SIZE):
        chunk = file_bytes[off:off + BLOCK_SIZE]
        block_id = base64.b64encode(
            f"{off // BLOCK_SIZE:08d}".encode("ascii")).decode("ascii")
        sep = "&" if "?" in blob_url else "?"
        resp = _cffi.put(
            f"{blob_url}{sep}comp=block&blockid={block_id}",
            data=chunk,
            headers={"Upload-Draft-Interop-Version": "6"},
            timeout=timeout, impersonate=_impersonate,
        )
        if resp.status_code != 201:
            raise RuntimeError(
                f"Put Block {off // BLOCK_SIZE} falhou ({resp.status_code}): "
                f"{resp.text[:300]}")
        block_ids.append(block_id)

    # Block list: <Latest> preserva a ordem dos blocos (não <Uncommitted>).
    xml = "<BlockList>" + "".join(f"<Latest>{bid}</Latest>" for bid in block_ids) \
        + "</BlockList>"
    sep = "&" if "?" in blob_url else "?"
    resp = _cffi.put(
        f"{blob_url}{sep}comp=blocklist",
        data=xml.encode("utf-8"),
        headers={
            "Content-Type": "application/xml",
            "x-ms-blob-content-type": content_type,
            "Upload-Draft-Interop-Version": "6",
            "Upload-Complete": "?1",
        },
        timeout=timeout, impersonate=_impersonate,
    )
    if resp.status_code != 201:
        raise RuntimeError(
            f"Put Block List falhou ({resp.status_code}): {resp.text[:300]}")
    return resp.status_code
