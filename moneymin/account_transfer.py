"""Portable account backups. No campaign history or device identity is transferred."""
from __future__ import annotations

import json
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, minute_api
from .atomic_io import load_json, save_json, save_bytes

LOCK = threading.RLock()
MAX_BYTES = 10 * 1024 * 1024
MAX_ACCOUNTS = 1000
FORMAT = "qmoney-accounts"


def _mapping(path: Path) -> dict:
    value = load_json(path, None) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError("Um arquivo local de contas está inválido. Nenhum dado será substituído.")
    return value


def email_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("E-mail ausente ou inválido.")
    value = value.strip().casefold()
    if len(value) > 200 or not re.fullmatch(r"[a-z0-9.!#$%&'+_=~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", value):
        raise ValueError("E-mail inválido.")
    return value


def token_accounts() -> dict[str, tuple[Path, dict]]:
    result = {}
    for path in sorted(config.tokens_dir().glob("token_*.json")):
        data = load_json(path, None)
        if not isinstance(data, dict):
            continue
        try:
            email = email_key(data.get("email"))
        except ValueError:
            continue
        result.setdefault(email, (path, data))
    return result


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
        if not isinstance(source, dict) or email_key(source.get("email", email)) != email:
            raise ValueError("O e-mail do token não corresponde à conta.")
        token = {"email": email}
        for key in ("idToken", "refreshToken", "localId", "expiresIn"):
            alias = {"idToken": "id_token", "refreshToken": "refresh_token"}.get(key, key)
            value = source.get(key, source.get(alias))
            if value is not None:
                if not isinstance(value, str) or len(value) > 32768:
                    raise ValueError("Credencial de sessão inválida.")
                token[key] = value
        if not token.get("refreshToken") or not token.get("idToken"):
            raise ValueError("Token incompleto: faltam idToken ou refreshToken.")
        expiry = source.get("expires_at", 0)
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry < 0:
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
        for index, source in enumerate(records, 1):
            row = {"row": index, "email": "", "status": "invalid"}
            try:
                record = clean_record(source)
                email = row["email"] = record["email"]
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
            except (OSError, RuntimeError):
                row.update(status="error", message="Falha ao autenticar ou salvar a conta. Verifique o acesso e tente novamente.")
            results.append(row)
        counts = {status: sum(r["status"] == status for r in results)
                  for status in ("new", "imported", "duplicate", "invalid", "error")}
        return {"applied": apply, "total": len(results), "counts": counts, "results": results}


def _save_record(record: dict, token_path: Path, passwords_path: Path, removed_path: Path) -> None:
    paths = [token_path, passwords_path, removed_path]
    before = {p: p.read_bytes() if p.exists() else None for p in paths}
    try:
        email = record["email"]
        if record["token"]:
            save_json(token_path, record["token"])
        else:
            minute_api.login(email, record["password"])
        if record["password"]:
            passwords = _mapping(passwords_path)
            passwords[email] = record["password"]
            save_json(passwords_path, passwords)
        removed = _mapping(removed_path)
        removed["emails"] = [e for e in removed.get("emails", []) if str(e).casefold() != email]
        removed["schema"] = 1
        save_json(removed_path, removed)
    except Exception:
        for path, content in before.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                save_bytes(path, content)
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
            if passwords.get(email):
                raw["password"] = passwords[email]
            record = clean_record(raw)
            records.append({k: v for k, v in record.items() if v is not None})
        return {"format": FORMAT, "version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat(), "accounts": records}
