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
  - Quando não sobra nenhum clipe novo de um cenário (100% enviado), o chamador
    reseta o cenário (`reset`) e recomeça do início — ver `campaign.run_campaign`.
  - Na primeira leitura, se o arquivo não existe, o registro é SEMEADO a partir
    dos logs de campanha anteriores (`data/campaign_*.json`) — envios antigos
    continuam valendo.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from . import config
from .atomic_io import load_json, save_json

FILE_NAME = "sent_videos.json"
_LOCK = threading.Lock()


def _path() -> Path:
    """Caminho do registro (lido na hora — DATA_DIR é patchável nos testes)."""
    return config.DATA_DIR / FILE_NAME


def _seed_from_logs() -> dict[str, dict[str, list[str]]]:
    """Reconstrói o registro a partir dos logs de campanha (envios com ok=true)."""
    data: dict[str, dict[str, list[str]]] = {}
    data_dir = config.DATA_DIR
    if not data_dir.exists():
        return data
    for p in data_dir.iterdir():
        if not (p.name.startswith("campaign_") and p.name.endswith(".json")):
            continue
        log = load_json(p, {})
        if not isinstance(log, dict):
            continue
        for item in log.get("items", []):
            uid = item.get("clip_uid")
            task_id = item.get("task_id")
            task_name = item.get("task_name")
            scenario = (f"minute|{task_id}|{task_name}"
                        if task_id and task_name else item.get("task_scenario") or "")
            if not uid:
                continue
            for acc in item.get("accounts", []):
                if acc.get("ok") and acc.get("email"):
                    entry = data.setdefault(scenario, {}).setdefault(uid, [])
                    if acc["email"] not in entry:
                        entry.append(acc["email"])
    return data


def load() -> dict[str, dict[str, list[str]]]:
    """Carrega o registro (semando dos logs de campanha na 1ª vez)."""
    path = _path()
    if not path.exists():
        data = _seed_from_logs()
        if data:
            _save(data)
        return data
    raw = load_json(path, {})
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


def mark_sent(scenario: str, clip_uid: str, email: str) -> None:
    """Registra que `clip_uid` foi enviado com sucesso para `email`."""
    with _LOCK:
        data = load()
        entry = data.setdefault(scenario, {}).setdefault(clip_uid, [])
        if email not in entry:
            entry.append(email)
            _save(data)


def sent_emails(scenario: str, clip_uid: str) -> set[str]:
    """Contas que já receberam `clip_uid` neste cenário."""
    return set(load().get(scenario, {}).get(clip_uid, []))


def is_sent_to_all(scenario: str, clip_uid: str, emails: list[str]) -> bool:
    """True se o clipe já foi enviado para TODAS as contas da campanha."""
    if not emails:
        return False
    done = sent_emails(scenario, clip_uid)
    return all(e in done for e in emails)


def filter_unsent(scenario: str, clip_uids: list[str], emails: list[str]) -> list[str]:
    """Devolve só os uids que ainda NÃO foram enviados para todas as contas."""
    return [u for u in clip_uids if not is_sent_to_all(scenario, u, emails)]


def reset(scenario: str | None = None) -> None:
    """Limpa o registro — de um cenário ou de tudo (botão de reset / 100%)."""
    with _LOCK:
        data = load()
        if scenario is None:
            data = {}
        else:
            data.pop(scenario, None)
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
