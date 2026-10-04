"""Portable account backups. No campaign history or device identity is transferred."""
from __future__ import annotations

import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, credential_store, minute_api, account_bans, org_policy, token_store
from .atomic_io import load_json_state, save_json, save_bytes

LOCK = threading.RLock()
MAX_BYTES = 10 * 1024 * 1024
MAX_ACCOUNTS = 1000
FORMAT = "qmoney-accounts"


class ImportRecoveryError(RuntimeError):
    """Uma gravação falhou e nem todos os arquivos puderam ser restaurados."""


def _mapping(path: Path) -> dict:
    return load_json_state(path, {})


def email_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("E-mail ausente ou inválido.")
    value = value.strip().casefold()
    if len(value) > 200 or not re.fullmatch(r"[a-z0-9.!#$%&'+_=~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", value):
        raise ValueError("E-mail inválido.")
    return value


def token_accounts() -> dict[str, tuple[Path, dict]]:
    return token_store.records(config.tokens_dir(), strict=True)


def decode_document(raw: str) -> list:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("O arquivo deve ter no máximo 10 MB.")
    def unique_keys(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("JSON com campos repetidos.")
            out[key] = value
        return out
    try:
        doc = json.loads(raw.lstrip("\ufeff"), object_pairs_hook=unique_keys)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Arquivo JSON inválido.") from exc
    if isinstance(doc, dict):
        if "format" in doc and (doc.get("format") != FORMAT or type(doc.get("version")) is not int or doc["version"] != 1):
            raise ValueError("Formato ou versão do backup não suportado.")
        doc = doc.get("accounts", [doc] if "email" in doc else None)
    if not isinstance(doc, list) or not 1 <= len(doc) <= MAX_ACCOUNTS:
        raise ValueError("O JSON deve conter entre 1 e 1.000 contas.")
    return doc


def clean_record(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Cada conta deve ser um objeto JSON.")
    email = email_key(raw.get("email"))
    password = raw.get("password", raw.get("senha"))
    if password is not None and (not isinstance(password, str) or not password or len(password) > 4096):
        raise ValueError("Senha inválida.")
    source = raw.get("token", raw if "refreshToken" in raw or "refresh_token" in raw else None)
    token = None
    if source is not None:
        if not isinstance(source, dict) or email_key(source.get("email")) != email:
            raise ValueError("O e-mail do token não corresponde à conta.")
        token_store.validated(source, email)
        token = {"email": email}
        for key in ("idToken", "refreshToken", "localId", "user_id", "uid", "expiresIn"):
            alias = {"idToken": "id_token", "refreshToken": "refresh_token"}.get(key, key)
            value = source.get(key, source.get(alias))
            if value is not None:
                if not isinstance(value, str) or len(value) > 32768:
                    raise ValueError("Credencial de sessão inválida.")
                token[key] = value
        if not token.get("refreshToken") or not token.get("idToken"):
            raise ValueError("Token incompleto: faltam idToken ou refreshToken.")
        expiry = source.get("expires_at", 0)
        try:
            valid_expiry = (not isinstance(expiry, bool) and isinstance(expiry, (int, float))
                            and math.isfinite(expiry) and expiry >= 0)
        except OverflowError:
            valid_expiry = False
        if not valid_expiry:
            raise ValueError("Validade do token inválida.")
        token["expires_at"] = expiry
    if token is None and password is None:
        raise ValueError("Informe senha ou token de sessão completo.")
    return {"email": email, "password": password, "token": token}


def import_accounts(raw: str, *, apply: bool, passwords_path: Path, removed_path: Path) -> dict:
    records = decode_document(raw)
    with LOCK:
        existing = token_accounts()
        removed_data = _mapping(removed_path)
        if not isinstance(removed_data.get("emails", []), list):
            raise ValueError("O registro local de contas removidas está inválido.")
        _mapping(passwords_path)
        removed = {str(e).casefold() for e in removed_data.get("emails", [])}
        seen = set()
        destinations = {}
        results = []
        recovery_failed = False
        for index, source in enumerate(records, 1):
            row = {"row": index, "email": "", "status": "invalid"}
            if recovery_failed:
                row.update(status="error", message="Não importada: o lote foi interrompido por falha na restauração dos arquivos locais.")
                results.append(row)
                continue
            try:
                record = clean_record(source)
                email = row["email"] = record["email"]
                if (org_policy.account_kind(email) == "crowtado"
                        and not record["password"]):
                    raise ValueError(
                        "Conta Crowtado sem senha: o token Minute sozinho não "
                        "permite consultar saldo nem criar um backup completo."
                    )
                account_bans.require_not_banned(email)
                path = config.token_path(email)
                destination = str(path).casefold()
                if email in seen or (email in existing and email not in removed):
                    row.update(status="duplicate", message="Conta já cadastrada ou repetida no arquivo; preservada.")
                elif destination in destinations and destinations[destination] != email:
                    raise ValueError("Conflito de nome de arquivo entre contas; nenhuma substituição permitida.")
                elif path.exists() and (email not in existing or existing[email][0] != path):
                    raise ValueError("Arquivo local conflitante; conta não substituída.")
                else:
                    destinations[destination] = email
                    seen.add(email)
                    row.update(status="new", message="Pronta para importar; acesso ainda não verificado.")
                    if apply:
                        _save_record(record, path, passwords_path, removed_path)
                        existing[email] = (path, record)
                        row.update(status="imported", message="Importada. Use Verificar todas para validar o acesso.")
            except ValueError as exc:
                row["message"] = str(exc)
            except ImportRecoveryError:
                recovery_failed = True
                row.update(status="error", message="Falha ao restaurar os arquivos locais. O lote foi interrompido; confira as contas e o armazenamento antes de tentar novamente.")
            except (OSError, RuntimeError):
                row.update(status="error", message="Falha ao autenticar ou salvar a conta. Verifique o acesso e tente novamente.")
            results.append(row)
        counts = {status: sum(r["status"] == status for r in results)
                  for status in ("new", "imported", "duplicate", "invalid", "error")}
        return {"applied": apply, "total": len(results), "counts": counts, "results": results}


def _save_record(record: dict, token_path: Path, passwords_path: Path, removed_path: Path) -> None:
    # Session writers take path lock -> token lock; preserve that ordering here.
    with minute_api._lock_for(token_path), token_store.transaction():
        _save_record_transaction(record, token_path, passwords_path, removed_path)


def _save_record_transaction(record: dict, token_path: Path, passwords_path: Path, removed_path: Path) -> None:
    paths = [token_path, removed_path]
    if record["password"]:
        # A credencial protegida faz parte da mesma transação. O mapa legado
        # de senhas é somente leitura e não recebe novas cópias em texto puro.
        paths.append(credential_store.record_path(config.SECRETS_DIR, record["email"]))
    before = {p: p.read_bytes() if p.exists() else None for p in paths}
    token_written = False
    try:
        email = record["email"]
        if record["token"]:
            token_store.save(config.SECRETS_DIR, email, record["token"])
        else:
            minute_api.login(email, record["password"])
        token_written = True
        removed = _mapping(removed_path)
        removed["emails"] = [e for e in removed.get("emails", []) if str(e).casefold() != email]
        removed["schema"] = 1
        save_json(removed_path, removed)
        if record["password"]:
            credential_store.save(config.SECRETS_DIR, email, record["password"])
    except Exception:
        if not token_written:
            # A failed token/login call does not prove what it published.
            # Preserve every consulted destination and the original error.
            raise
        failed = False
        for path, content in before.items():
            try:
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    save_bytes(path, content)
            except OSError:
                failed = True
        if failed:
            raise ImportRecoveryError("Falha na restauração dos arquivos locais.")
        raise


def export_accounts(emails: list[str] | None, passwords: dict, removed: set[str]) -> dict:
    with LOCK:
        existing = {email: data for email, (_, data) in token_accounts().items() if email not in removed}
        selected = set(existing) if emails is None else {email_key(e) for e in emails}
        if not selected:
            raise ValueError("Nenhuma conta selecionada para exportar.")
        if selected - existing.keys():
            raise ValueError("Uma conta selecionada não existe mais. Atualize a lista.")
        passwords = {str(k).strip().casefold(): v for k, v in passwords.items()}
        records = []
        for email in sorted(selected):
            raw = {"email": email, "token": existing[email]}
            # A ausência permite fallback legado. Corrupção/proteção
            # indisponível não pode produzir um backup com senha antiga.
            password = credential_store.lookup(config.SECRETS_DIR, email, strict=True)
            if password is None:
                password = passwords.get(email)
            if password:
                raw["password"] = password
            if org_policy.account_kind(email) != "claru" and not password:
                raise ValueError("Exportação cancelada: há conta Crowtado sem senha local; nenhum backup incompleto foi criado.")
            record = clean_record(raw)
            records.append({k: v for k, v in record.items() if v is not None})
        return {"format": FORMAT, "version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat(), "accounts": records}
