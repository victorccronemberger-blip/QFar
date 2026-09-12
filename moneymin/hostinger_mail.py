"""
hostinger_mail.py — Leitura da caixa catch-all via Hostinger Mail API.

Usado para receber códigos de verificação de serviços (ex.: crowtado/Clerk)
em endereços gerados no domínio catch-all (qualquer @academy4u.com.br cai na
mesma caixa). Somente stdlib.

Requer `HOSTINGER_MAIL_TOKEN` no `.env` (hPanel -> Advanced -> API).

Exemplo:
    from moneymin.hostinger_mail import wait_for_code
    code = wait_for_code("fulano123@academy4u.com.br", sender="crowtado.com")
"""
from __future__ import annotations

import json
import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import config, tls


class MailError(RuntimeError):
    """Falha na Hostinger Mail API (token inválido, caixa não achada, etc.)."""


def _connection_id(values: dict[str, Any]) -> str:
    explicit = str(values.get("id") or "").strip()
    if explicit:
        return explicit
    material = (str(values.get("token") or "") + "\0"
                + str(values.get("mailbox_id") or "")).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def _routes(values: dict[str, Any]) -> list[str]:
    raw = values.get("routes") or []
    if isinstance(raw, str):
        raw = re.split(r"[,;\s]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item).strip().lower().lstrip("@") for item in raw
            if str(item).strip()]


def configured_connections(to_address: str | None = None) -> list[dict[str, Any]]:
    """Conexões válidas, priorizadas pelas regras do endereço destinatário."""
    raw = getattr(config, "HOSTINGER_MAIL_PROFILES", None) or []
    connections = [dict(item) for item in raw
                   if isinstance(item, dict)
                   and str(item.get("token") or "").strip()]
    if not connections and config.HOSTINGER_MAIL_TOKEN:
        connections = [{
            "id": "legacy",
            "name": "Caixa principal",
            "token": config.HOSTINGER_MAIL_TOKEN,
            "mailbox_id": config.HOSTINGER_MAILBOX_ID,
            "routes": [],
        }]
    for item in connections:
        item["id"] = _connection_id(item)
        item["routes"] = _routes(item)
    target = str(to_address or "").strip().lower()
    if not target:
        return connections
    domain = target.rpartition("@")[2]
    matched = [item for item in connections if any(
        route == target or route == domain
        for route in item["routes"]
    )]
    return matched or connections


def _request(path: str, method: str = "GET", body: Any = None,
             *, token: str | None = None) -> tuple[int, Any]:
    """Chamada HTTP à Mail API. Devolve (status, corpo_parseado_ou_texto)."""
    credential = token if token is not None else config.HOSTINGER_MAIL_TOKEN
    if not credential:
        raise MailError("HOSTINGER_MAIL_TOKEN não configurado — ajuste o .env.")
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else None
    req = urllib.request.Request(config.HOSTINGER_MAIL_BASE + path, method=method, data=data)
    req.add_header("Authorization", f"Bearer {credential}")
    req.add_header("Accept", "application/json")
    # Cloudflare (erro 1010) bloqueia o UA padrão do urllib — usar UA neutro.
    req.add_header("User-Agent", "curl/8.5.0")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with tls.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
            return resp.status, _parse(text)
    except urllib.error.HTTPError as exc:
        return exc.code, _parse(exc.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise MailError(f"falha de rede na Mail API: {exc}") from exc


def test_connection(token: str | None = None,
                    mailbox: str | None = None) -> dict[str, Any]:
    """Valida credencial sem devolver token ou conteúdo de mensagens."""
    mailboxes = discover_mailboxes(token)
    ids = {item["resource_id"] for item in mailboxes}
    requested = str(mailbox or "").strip()
    if requested and requested not in ids:
        raise MailError("a caixa informada não pertence a esta credencial")
    return {
        "ok": True,
        "mailboxes": len(mailboxes),
        "mailbox_selected": bool(requested),
        "detected": mailboxes,
    }


def discover_mailboxes(token: str | None = None) -> list[dict[str, str]]:
    """Detecta caixas e domínios da credencial sem expor o token."""
    status, body = _request("/api/v1/me", token=token)
    if status != 200 or not isinstance(body, dict):
        raise MailError(f"a Hostinger recusou a credencial (HTTP {status})")
    raw = (body.get("data") or {}).get("mailboxes") or []
    detected: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        resource_id = str(item.get("resourceId") or "").strip()
        address = str(item.get("address") or "").strip().lower()
        if not resource_id:
            continue
        detected.append({
            "resource_id": resource_id,
            "address": address,
            "domain": address.rpartition("@")[2] if "@" in address else "",
        })
    if not detected:
        raise MailError("a credencial não possui nenhuma caixa de email")
    return detected


def _parse(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def mailbox_id(*, token: str | None = None, mailbox: str | None = None) -> str:
    """resourceId da caixa catch-all (config.HOSTINGER_MAILBOX_ID ou a 1ª da conta)."""
    selected = mailbox if mailbox is not None else config.HOSTINGER_MAILBOX_ID
    if selected:
        return selected
    status, body = _request("/api/v1/me", token=token)
    if status != 200:
        raise MailError(f"/me falhou ({status}): {str(body)[:200]}")
    mailboxes = (body.get("data") or {}).get("mailboxes") or []
    if not mailboxes:
        raise MailError("nenhuma caixa de email na conta Hostinger.")
    return mailboxes[0]["resourceId"]


def search_messages(
    folder: str = "INBOX",
    to: str | None = None,
    from_: str | None = None,
    subject: str | None = None,
    since: str | None = None,
    per_page: int = 10,
    *,
    token: str | None = None,
    mailbox: str | None = None,
) -> list[dict[str, Any]]:
    """Busca mensagens na caixa. `since` é data ISO (YYYY-MM-DD)."""
    criteria = {"to": to, "from": from_, "subject": subject, "since": since}
    body = {k: v for k, v in criteria.items() if v}
    status, resp = _request(
        f"/api/v1/mailboxes/{mailbox_id(token=token, mailbox=mailbox)}/folders/{folder}/messages/search"
        f"?perPage={per_page}&sort=-date",
        "POST",
        body,
        token=token,
    )
    if status != 200:
        raise MailError(f"search falhou ({status}): {str(resp)[:200]}")
    return resp.get("data") or []


def message_text(uid: int, folder: str = "INBOX", *,
                 token: str | None = None, mailbox: str | None = None) -> str:
    """Corpo em texto puro de uma mensagem."""
    status, body = _request(
        f"/api/v1/mailboxes/{mailbox_id(token=token, mailbox=mailbox)}/folders/{folder}/messages/{uid}/text",
        token=token,
    )
    if status != 200:
        raise MailError(f"text da mensagem {uid} falhou ({status}): {str(body)[:200]}")
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, dict):
            return data.get("text") or json.dumps(data, ensure_ascii=False)
        return str(data) if data else json.dumps(body, ensure_ascii=False)
    return str(body)


def delete_message(uid: int, folder: str = "INBOX", *,
                   token: str | None = None, mailbox: str | None = None) -> bool:
    """Apaga uma mensagem. Da INBOX ela vai para a Trash; da Trash some de vez.

    Best-effort: 404 (já apagada) conta como sucesso.
    """
    status, _ = _request(
        f"/api/v1/mailboxes/{mailbox_id(token=token, mailbox=mailbox)}/folders/{folder}/messages/{uid}",
        "DELETE", token=token)
    return status in (200, 204, 404)


def purge_sender(sender: str, folder: str = "INBOX",
                 older_than_s: float = 0, now: float | None = None) -> int:
    """Apaga mensagens de um remetente. Com `older_than_s`, só as mais velhas
    que N segundos (protege e-mails recém-chegados ainda não consumidos).
    Devolve quantas foram apagadas."""
    from datetime import datetime
    agora = now if now is not None else time.time()
    apagadas = 0
    for msg in search_messages(folder=folder, from_=sender, per_page=50):
        if older_than_s:
            try:
                ts = datetime.fromisoformat(
                    str(msg.get("date", "")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if agora - ts < older_than_s:
                continue
        if delete_message(msg["uid"], folder=folder):
            apagadas += 1
    return apagadas


def purge_trash() -> int:
    """Esvazia a Trash (o DELETE da INBOX só MOVE para lá — continua ocupando
    espaço na cota da caixa). Pagina até esvaziar; para se uma página não
    apagar nada (proteção contra loop)."""
    apagadas = 0
    while True:
        msgs = search_messages(folder="Trash", per_page=50)
        if not msgs:
            return apagadas
        antes = apagadas
        for msg in msgs:
            if delete_message(msg["uid"], folder="Trash"):
                apagadas += 1
        if apagadas == antes:
            return apagadas


_CODE_RE = re.compile(r"\b(\d{6})\b")


def extract_code(text: str) -> str | None:
    """Extrai um código de verificação de 6 dígitos do corpo do email."""
    match = _CODE_RE.search(text)
    return match.group(1) if match else None


def max_uid(to_address: str | None = None) -> int | dict[str, int]:
    """Maior uid atual na INBOX (para ignorar mensagens antigas no wait_for_code)."""
    connections = configured_connections(to_address)
    if not connections:
        raise MailError("nenhuma conexão Hostinger configurada")
    cursors: dict[str, int] = {}
    errors: list[str] = []
    for item in connections:
        try:
            msgs = search_messages(
                to=to_address, per_page=1,
                token=str(item["token"]),
                mailbox=str(item.get("mailbox_id") or "") or None,
            )
            cursors[str(item["id"])] = max(
                (int(message.get("uid", 0)) for message in msgs), default=0)
        except Exception as exc:  # noqa: BLE001 — outra caixa ainda pode responder
            errors.append(f"{item.get('name') or item['id']}: {exc}")
    if not cursors:
        raise MailError("nenhuma caixa Hostinger respondeu: " + "; ".join(errors))
    return next(iter(cursors.values())) if len(connections) == 1 else cursors


def wait_for_code(
    to_address: str,
    sender: str | None = None,
    min_uid: int | dict[str, int] = 0,
    timeout: int = 180,
    poll: int = 5,
) -> str:
    """Espera chegar um email para `to_address` e devolve o código de 6 dígitos.

    `min_uid` ignora mensagens antigas (passe o maior uid visto antes de
    disparar o envio). Levanta MailError se estourar o timeout.

    Notas de robustez (verificadas ao vivo, 22/08):
      - A busca da API IGNORA o filtro `to` quando combinado com `from` —
        por isso o destinatário é conferido aqui, no client.
      - O código do Clerk vem no SUBJECT ("123456 is your verification code") —
        checa o subject antes do corpo (o corpo pode ter outros números).
    """
    deadline = time.time() + timeout
    alvo = to_address.lower()
    while time.time() < deadline:
        # com sender: busca por remetente e filtra destinatário no client;
        # sem sender: o filtro `to` server-side funciona sozinho
        connections = configured_connections(to_address)
        if not connections:
            raise MailError("nenhuma conexão Hostinger configurada")
        errors: list[str] = []
        successful_searches = 0
        for connection in connections:
            token = str(connection["token"])
            mailbox = str(connection.get("mailbox_id") or "") or None
            connection_id = str(connection["id"])
            threshold = (int(min_uid.get(connection_id, 0))
                         if isinstance(min_uid, dict) else int(min_uid))
            try:
                msgs = (search_messages(from_=sender, per_page=20,
                                        token=token, mailbox=mailbox) if sender
                        else search_messages(to=to_address, per_page=20,
                                             token=token, mailbox=mailbox))
            except Exception as exc:  # noqa: BLE001 — tenta as demais conexões
                errors.append(
                    f"{connection.get('name') or connection_id}: {exc}")
                continue
            successful_searches += 1
            for msg in msgs:
                if int(msg.get("uid", 0)) <= threshold:
                    continue
                if sender:
                    dests = [str(t.get("address", "")).lower()
                             for t in (msg.get("to") or [])]
                    if dests and alvo not in dests:
                        continue
                code = extract_code(str(msg.get("subject") or ""))
                if not code:
                    code = extract_code(message_text(
                        msg["uid"], token=token, mailbox=mailbox))
                if code:
                    # código é de uso único e a caixa catch-all lota rápido:
                    # apaga o e-mail depois de consumir (best-effort)
                    try:
                        delete_message(msg["uid"], token=token, mailbox=mailbox)
                    except Exception:  # noqa: BLE001 — limpeza não derruba o fluxo
                        pass
                    return code
        if not successful_searches:
            raise MailError(
                "nenhuma caixa Hostinger respondeu: " + ", ".join(errors))
        time.sleep(poll)
    raise MailError(
        f"timeout ({timeout}s) esperando email para {to_address}"
        + (f" de {sender}" if sender else "")
    )
