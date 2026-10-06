"""
sent_registry.py — Registro persistente de clipes já enviados ao Minute.

Guarda, por cenário Ego4D, quais clipes já foram enviados com sucesso e para
quais contas — assim uma campanha nova nunca repete vídeo para a mesma conta.

Arquivo: `data/sent_videos.json`, formato:

    {"<scenario>": {"<clip_uid>": ["email@conta", ...]}}

Semântica:
  - Um clipe é "esgotado" para uma campanha quando TODAS as contas da campanha
    já constam na lista dele (`is_sent_to_all`). A seleção automática pula
    esses clipes.
  - Quando não sobra nenhum clipe novo, a campanha informa esgotamento. O
    histórico só é limpo por uma solicitação explícita de reset.
  - Na primeira leitura, se o arquivo não existe, o registro é SEMEADO a partir
    dos logs de campanha anteriores (`data/campaign_*.json`) — envios antigos
    continuam valendo.
"""
from __future__ import annotations

import threading
from collections.abc import Collection
from pathlib import Path
from typing import Any

from . import config
from .atomic_io import JsonStateError, load_json_state, save_json
from .campaign_state import campaign_state_operation
from .campaign_history import is_campaign_history_name

FILE_NAME = "sent_videos.json"
_LOCK = threading.RLock()


def _path() -> Path:
    """Caminho do registro (lido na hora — DATA_DIR é patchável nos testes)."""
    return config.DATA_DIR / FILE_NAME


def _seed_from_logs() -> dict[str, dict[str, list[str]]]:
    """Reconstrói o registro a partir dos logs de campanha (envios com ok=true)."""
    data: dict[str, dict[str, list[str]]] = {}
    data_dir = config.DATA_DIR
    resets = _reset_history()
    if not data_dir.exists():
        return data
    for p in data_dir.iterdir():
        if not is_campaign_history_name(p.name):
            continue
        # A damaged historical receipt cannot prove that no clip was sent.
        log = load_json_state(p, {})
        items = log.get("items", [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            uid = item.get("clip_uid")
            task_id = item.get("task_id")
            task_name = item.get("task_name")
            scenario = item.get("registry_key") or (f"minute|{task_id}|{task_name}"
                        if task_id and task_name else item.get("task_scenario") or "")
            if not isinstance(uid, str) or not uid or not isinstance(scenario, str):
                continue
            if p.name in resets.get("all", []) or p.name in resets.get("scenarios", {}).get(scenario, []):
                continue
            accounts = item.get("accounts", [])
            if not isinstance(accounts, list):
                continue
            for acc in accounts:
                if (isinstance(acc, dict) and acc.get("ok") is True
                        and not acc.get("skipped") and acc.get("finalized") is not False
                        and isinstance(acc.get("email"), str)):
                    entry = data.setdefault(scenario, {}).setdefault(uid, [])
                    if acc["email"] not in entry:
                        entry.append(acc["email"])
    return data


@campaign_state_operation
def load(*, persist_seed: bool = True) -> dict[str, dict[str, list[str]]]:
    """Carrega o registro, semeando dos logs na 1ª vez.

    Planejamento usa persist_seed=False para observar o mesmo histórico sem
    gravar o índice antes de a campanha ser admitida.
    """
    with _LOCK:
        return _load_locked(persist_seed=persist_seed)


def _load_locked(*, persist_seed: bool = True) -> dict[str, dict[str, list[str]]]:
    path = _path()
    if not path.exists():
        data = _seed_from_logs()
        if data and persist_seed:
            _save(data)
        return data
    try:
        raw = load_json_state(path, None)
    except JsonStateError:
        raise ValueError("Registro de envios inválido ou ilegível; restaure o arquivo antes de continuar.") from None
    if not isinstance(raw, dict) or any(
        not isinstance(clips, dict) or any(
            not isinstance(emails, list) or any(not isinstance(email, str) for email in emails)
            for emails in clips.values())
        for clips in raw.values()
    ):
        raise ValueError("Registro de envios inválido ou ilegível; restaure o arquivo antes de continuar.")
    # normaliza: garante dict[str, dict[str, list[str]]]
    out: dict[str, dict[str, list[str]]] = {}
    if isinstance(raw, dict):
        for scen, clips in raw.items():
            if not isinstance(clips, dict):
                continue
            out[str(scen)] = {str(uid): [str(e) for e in (emails or [])]
                              for uid, emails in clips.items()
                              if isinstance(emails, list)}
    return out


def _save(data: dict[str, dict[str, list[str]]]) -> None:
    save_json(_path(), data)


def _reset_history() -> dict[str, Any]:
    path = config.DATA_DIR / "sent_reset_history.json"
    if not path.exists():
        return {}
    try:
        result = load_json_state(path, None)
    except JsonStateError:
        raise ValueError("Histórico de reset inválido; restaure o arquivo antes de continuar.") from None
    if (not isinstance(result, dict) or not isinstance(result.get("all", []), list)
            or not isinstance(result.get("completed_sessions", []), list)
            or not isinstance(result.get("scenarios", {}), dict)
            or any(not isinstance(value, list) for value in result.get("scenarios", {}).values())
            or any(not isinstance(value, str)
                   for values in [result.get("all", []), result.get("completed_sessions", []),
                                  *result.get("scenarios", {}).values()]
                   for value in values)):
        raise ValueError("Histórico de reset inválido; restaure o arquivo antes de continuar.")
    return result


@campaign_state_operation
def recovery_was_reset(session_id: str, scenario: str, history_name: str = "") -> bool:
    with _LOCK:
        resets = _reset_history()
        return (session_id in resets.get("completed_sessions", [])
                or bool(history_name) and (history_name in resets.get("all", [])
                    or history_name in resets.get("scenarios", {}).get(scenario, [])))


@campaign_state_operation
def recovery_reset_checker():
    """One validated reset snapshot for a read-only recovery listing."""
    with _LOCK:
        resets = _reset_history()
        lists = [resets.get("completed_sessions", []), resets.get("all", []),
                 *resets.get("scenarios", {}).values()]
        if any(not isinstance(value, str) for values in lists for value in values):
            raise ValueError("Histórico de reset inválido; restaure o arquivo antes de continuar.")
        sessions = set(resets.get("completed_sessions", []))
        histories = set(resets.get("all", []))
        scenarios = {name: set(values) for name, values in resets.get("scenarios", {}).items()}
    return lambda sid, scenario, history: (
        sid in sessions or bool(history) and (
            history in histories or history in scenarios.get(scenario, set())))


@campaign_state_operation
def mark_sent_many(deliveries: list[tuple[str, str, str]]) -> None:
    """Persist a validated reconciliation batch before acknowledging journals."""
    if not deliveries:
        return
    with _LOCK:
        data = load()
        changed = False
        for scenario, clip_uid, email in deliveries:
            entry = data.setdefault(scenario, {}).setdefault(clip_uid, [])
            if email not in entry:
                entry.append(email)
                changed = True
        if changed:
            _save(data)


@campaign_state_operation
def mark_sent(scenario: str, clip_uid: str, email: str) -> None:
    """Registra que `clip_uid` foi enviado com sucesso para `email`."""
    with _LOCK:
        data = load()
        entry = data.setdefault(scenario, {}).setdefault(clip_uid, [])
        if email not in entry:
            entry.append(email)
            _save(data)


def recorded_emails(registry: dict[str, dict[str, list[str]]],
                    scenario: str, clip_uid: str) -> set[str]:
    """Read old category labels by stable task ID, preserving stored evidence."""
    parts = scenario.split("|", 2)
    if len(parts) >= 2 and parts[0] == "minute" and parts[1]:
        task_prefix = f"minute|{parts[1]}"
        keys = [key for key in registry
                if key == task_prefix or key.startswith(task_prefix + "|")]
    else:
        keys = [scenario]
    return set().union(*(set(registry.get(key, {}).get(clip_uid, [])) for key in keys))


def sent_emails(scenario: str, clip_uid: str) -> set[str]:
    """Contas que já receberam `clip_uid` neste cenário."""
    return recorded_emails(load(), scenario, clip_uid)


def is_sent_to_all(scenario: str, clip_uid: str, emails: list[str]) -> bool:
    """True se o clipe já foi enviado para TODAS as contas da campanha."""
    if not emails:
        return False
    done = sent_emails(scenario, clip_uid)
    return all(e in done for e in emails)


def filter_unsent(scenario: str, clip_uids: list[str], emails: list[str]) -> list[str]:
    """Devolve só os uids que ainda NÃO foram enviados para todas as contas."""
    return [u for u in clip_uids if not is_sent_to_all(scenario, u, emails)]


def _delivery_identity(scenario: str, uid: str, email: str) -> tuple[str, str, str]:
    parts = scenario.split("|", 2)
    return (("minute|" + parts[1]) if len(parts) >= 2 and parts[0] == "minute" and parts[1]
            else scenario, uid, email)


def _history_deliveries(path: Path) -> list[tuple[str, str, str, str | None, bool]]:
    history = load_json_state(path, None)
    # Legacy CLI configurations share the campaign_*.json namespace with
    # receipts. They contain no attempts and must not invent sent deliveries.
    if (isinstance(history, dict) and "items" not in history
            and "started_at" not in history and "status" not in history
            and isinstance(history.get("work_dir"), str)
            and isinstance(history.get("accounts"), list)
            and isinstance(history.get("tasks"), list)):
        return []
    if not isinstance(history, dict) or not isinstance(history.get("items"), list):
        raise ValueError("Histórico de campanha inválido; preserve os arquivos para revisão.")
    deliveries = []
    for item in history["items"]:
        if not isinstance(item, dict):
            raise ValueError("Histórico de campanha inválido; preserve os arquivos para revisão.")
        uid, task_id, task_name = item.get("clip_uid"), item.get("task_id"), item.get("task_name")
        scenario = item.get("registry_key") or (f"minute|{task_id}|{task_name}"
                    if task_id and task_name else item.get("task_scenario"))
        if (not isinstance(uid, str) or not uid or not isinstance(scenario, str) or not scenario
                or not isinstance(item.get("accounts"), list)):
            raise ValueError("Histórico de campanha sem identidade válida; preserve os arquivos para revisão.")
        for account in item["accounts"]:
            if (not isinstance(account, dict) or not isinstance(account.get("email"), str)
                    or not account["email"] or any(key in account and type(account[key]) is not bool
                                                  for key in ("ok", "skipped"))
                    or "finalized" in account and account["finalized"] is not None
                    and type(account["finalized"]) is not bool
                    or account.get("session_id") is not None and not isinstance(account["session_id"], str)):
                raise ValueError("Histórico de campanha sem recibo válido; preserve os arquivos para revisão.")
            confirmed = (account.get("ok") is True and not account.get("skipped")
                         and ("finalized" not in account or account["finalized"] is True))
            deliveries.append((scenario, uid, account["email"], account.get("session_id"), confirmed))
    return deliveries


def _scoped_reset(scenario: str | None, history_names: Collection[str]) -> None:
    if isinstance(history_names, (str, bytes)) or not isinstance(history_names, Collection):
        raise ValueError("Selecione nomes de históricos válidos para limpar.")
    selected = set()
    root = config.DATA_DIR.resolve()
    for name in history_names:
        if (not isinstance(name, str) or Path(name).name != name or not name.startswith("campaign_")
                or not name.endswith(".json") or ":" in name):
            raise ValueError("Selecione nomes de históricos válidos para limpar.")
        path = config.DATA_DIR / name
        if (not path.is_file() or path.is_symlink() or path.resolve().parent != root
                or getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400):
            raise ValueError("Histórico selecionado ausente ou fora da instalação; preserve os arquivos.")
        selected.add(name)
    if not selected:
        return
    data = load(persist_seed=False)
    resets = _reset_history()
    old, retained, protected, session_entries, retained_sessions = set(), [], set(), {}, set()
    targeted_keys = {scenario} if scenario is not None else set()
    for path in sorted(config.DATA_DIR.glob("campaign_*.json")):
        if not is_campaign_history_name(path.name):
            continue
        if (path.is_symlink() or path.resolve().parent != root
                or getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400):
            raise ValueError("Histórico de campanha fora da instalação; preserve os arquivos.")
        for key, uid, email, sid, confirmed in _history_deliveries(path):
            identity = _delivery_identity(key, uid, email)
            in_scope = scenario is None or identity[0] == _delivery_identity(scenario, uid, email)[0]
            targeted = path.name in selected and in_scope
            already_reset = (path.name in resets.get("all", ())
                             or path.name in resets.get("scenarios", {}).get(key, ()))
            if sid:
                session_entries.setdefault(sid, []).append((identity, targeted, confirmed))
                if not targeted:
                    retained_sessions.add(sid)
            if targeted and confirmed:
                old.add(identity)
                targeted_keys.add(key)
            elif not targeted and confirmed and not already_reset:
                retained.append((key, uid, email))
                protected.add(identity)
            elif not confirmed:
                protected.add(identity)
    # Observe the installation's authoritative journals without migration,
    # account discovery, archive generation or rewriting any pending record.
    from . import upload
    from .media_lifecycle import media_state_lease
    from .upload_types import journal_delivery_confirmed
    completed = set()
    with media_state_lease(wait=True):
        directory = config.DATA_DIR / "sidecars"
        if (directory.is_symlink() or directory.exists() and
                (directory.resolve().parent != root
                 or getattr(directory.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)):
            raise ValueError("Registros de envio fora da instalação; preserve os arquivos.")
        sessions = {}
        for path in sorted(directory.iterdir()) if directory.is_dir() else ():
            if path.suffix.lower() != ".json":
                continue
            if path.is_symlink() or getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
                raise ValueError("Registro de envio inválido; preserve os arquivos.")
            row = upload._read_sidecar_file(path)
            if not isinstance(row, dict):
                raise ValueError("Registro de envio inválido; preserve os arquivos.")
            sessions.setdefault(row["session_id"], []).append(row)
        for sid, rows in sessions.items():
            expected = rows[0].get("expected_chunk_count", 1)
            context = rows[0].get("campaign_context")
            owner, task_id, org_key = (rows[0].get("account_email"), rows[0].get("task_id"),
                                       rows[0].get("org_key"))
            context_valid = (isinstance(context, dict)
                             and all(isinstance(context.get(key), str) and context[key]
                                     for key in ("registry_key", "clip_uid"))
                             and isinstance(owner, str) and bool(owner)
                             and (not context.get("task_id") or context["task_id"] == task_id))
            identity = _delivery_identity(context["registry_key"], context["clip_uid"], owner) if context_valid else None
            complete = (isinstance(org_key, str) and bool(org_key.strip())
                        and type(expected) is int and expected > 0 and len(rows) == expected
                        and all(type(row.get("chunk_index")) is int for row in rows)
                        and {row["chunk_index"] for row in rows} == set(range(expected))
                        and all(journal_delivery_confirmed(row) and row.get("account_email") == owner
                                and row.get("org_key") == org_key
                                and row.get("task_id") == task_id and row.get("campaign_context") == context
                                and type(row.get("expected_chunk_count", 1)) is int
                                and row.get("expected_chunk_count", 1) == expected for row in rows))
            old_receipts = [entry for entry in session_entries.get(sid, ()) if entry[1] and entry[2]]
            old_identities = {entry[0] for entry in old_receipts}
            matches_old = len(old_identities) == 1 and all(entry[0][2] == owner
                and (identity is None or entry[0] == identity)
                and (not entry[0][0].startswith("minute|")
                     or entry[0][0] == f"minute|{task_id}") for entry in old_receipts)
            if complete and matches_old and sid not in retained_sessions:
                completed.add(sid)
            else:
                # A pending/ambiguous group protects any known historic pair.
                # Confirmed receipts outside the chosen histories remain sent,
                # even if the persisted index was older than their campaign.
                protected.update(entry[0] for entry in session_entries.get(sid, ()))
                if identity is not None:
                    protected.add(identity)
                    if complete:
                        retained.append((context["registry_key"], context["clip_uid"], owner))
        removable = old - protected
        for key in list(data):
            for uid in list(data[key]):
                data[key][uid] = [email for email in data[key][uid]
                                  if _delivery_identity(key, uid, email) not in removable]
                if not data[key][uid]:
                    del data[key][uid]
            if not data[key]:
                del data[key]
        for key, uid, email in retained:
            entry = data.setdefault(key, {}).setdefault(uid, [])
            if email not in entry:
                entry.append(email)
        resets["completed_sessions"] = sorted(set(resets.get("completed_sessions", ())) | completed)
        if scenario is None:
            resets["all"] = sorted(set(resets.get("all", ())) | selected)
        else:
            histories = resets.setdefault("scenarios", {})
            for key in targeted_keys:
                histories[key] = sorted(set(histories.get(key, ())) | selected)
        save_json(config.DATA_DIR / "sent_reset_history.json", resets)
        _save(data)


@campaign_state_operation
def reset(scenario: str | None = None, *, history_names: Collection[str] | None = None) -> None:
    """Limpa tudo/um cenário, ou só recibos das campanhas explicitamente escolhidas."""
    with _LOCK:
        if history_names is not None:
            _scoped_reset(scenario, history_names)
            return
        data = load()
        if scenario is None:
            data = {}
        else:
            data.pop(scenario, None)
        resets = _reset_history()
        from .upload import list_sidecars
        sessions: dict[str, list[dict]] = {}
        for row in list_sidecars():
            if row.get("session_id"):
                sessions.setdefault(row["session_id"], []).append(row)
        completed = set()
        for sid, rows in sessions.items():
            expected = rows[0].get("expected_chunk_count", 1)
            if (type(expected) is int and expected > 0 and len(rows) == expected
                    and all(type(row.get("chunk_index")) is int for row in rows)
                    and {row["chunk_index"] for row in rows} == set(range(expected))
                    and all(row.get("finalized") is True and row.get("state") == "done"
                            and row.get("expected_chunk_count", 1) == expected
                            and (scenario is None or isinstance(row.get("campaign_context"), dict)
                                 and row["campaign_context"].get("registry_key") == scenario)
                            for row in rows)):
                completed.add(sid)
        resets["completed_sessions"] = sorted(set(resets.get("completed_sessions", [])) | completed)
        histories = {p.name for p in config.DATA_DIR.glob("campaign_*.json") if is_campaign_history_name(p.name)}
        if scenario is None:
            resets["all"] = sorted(set(resets.get("all", [])) | histories)
        else:
            scenarios = resets.setdefault("scenarios", {})
            scenarios[scenario] = sorted(set(scenarios.get(scenario, [])) | histories)
        # Grave a barreira antes de limpar: logs antigos não podem desfazer o reset.
        save_json(config.DATA_DIR / "sent_reset_history.json", resets)
        _save(data)


def all_uids() -> set[str]:
    """Todos os clip_uids já enviados (qualquer cenário/conta) — usado no wizard."""
    return {uid for clips in load().values() for uid in clips}


def summary() -> list[dict[str, Any]]:
    """Resumo por cenário: nº de clipes enviados e total de envios (pares conta×clipe)."""
    out = []
    for scen, clips in sorted(load().items()):
        label = scen
        task_id = None
        if scen.startswith("minute|"):
            _, task_id, label = (scen.split("|", 2) + [""])[:3]
        out.append({
            "scenario": label or "(sem cenário)",
            "task_id": task_id,
            "sent_clips": len(clips),
            "sends": sum(len(emails) for emails in clips.values()),
        })
    return out
