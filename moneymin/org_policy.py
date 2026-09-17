"""Organização Minute por conta, sem cair na primeira org da lista.

Contas Crowtado usam PE8EAR5V / Datoric. Contas Claru (@supply.claru.ai)
usam WSNEHSKC / Claru. Ter as duas orgs no perfil (join acidental) não
pode mandar Claru para a Crowtado nem Crowtado para o Hub antigo.
"""
from __future__ import annotations

from typing import Any

from . import config

Kind = str  # "crowtado" | "claru"


def account_kind(email: str) -> Kind:
    host = str(email).strip().casefold().rsplit("@", 1)[-1]
    if host == "claru.ai" or host.endswith(".claru.ai"):
        return "claru"
    return "crowtado"


def target_invite(email: str) -> str:
    if account_kind(email) == "claru":
        return config.CLARU_INVITE_CODE
    return config.INVITE_CODE


def target_org_key(email: str) -> str:
    if account_kind(email) == "claru":
        return config.CLARU_ORG_KEY
    return config.ORG_KEY


def _keys(orgs: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for item in orgs:
        key = item.get("resourceKey") if isinstance(item, dict) else None
        if isinstance(key, str) and key:
            out.append(key)
    return out


def pick_org_key(email: str, orgs: list[dict[str, Any]]) -> str | None:
    """Devolve a org que esta conta deve usar, ou None se ainda falta o join."""
    keys = _keys(orgs)
    wanted = target_org_key(email)
    if wanted in keys:
        return wanted
    return None


def ensure_membership(session: Any, email: str, orgs: list[dict[str, Any]]) -> str:
    """Garante a org certa e devolve o resourceKey.

    Crowtado sem Datoric entra com PE8EAR5V. Claru sem Claru entra com
    WSNEHSKC. Nunca escolhe a primeira org da lista. Levanta RuntimeError
    se a org alvo não aparecer.
    """
    chosen = pick_org_key(email, orgs)
    if chosen:
        return chosen
    invite = target_invite(email)
    wanted = target_org_key(email)
    status, _body = session.join_org(invite)
    if status not in (200, 201):
        raise RuntimeError(
            f"{email}: não entrou na org {invite} (HTTP {status})"
        )
    me = session.me() if hasattr(session, "me") else {}
    after = me.get("organizations") if isinstance(me, dict) else []
    chosen = pick_org_key(email, after if isinstance(after, list) else [])
    if chosen:
        return chosen
    raise RuntimeError(
        f"{email}: join {invite} não deixou a org {wanted} no perfil"
    )
