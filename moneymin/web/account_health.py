"""Independent service diagnostics; an accepted login is not payout clearance."""
from __future__ import annotations

from typing import Any
from datetime import datetime

SERVICES = ("minute", "crowtado")


def provider(check: dict, name: str) -> dict:
    providers = check.get("providers")
    if isinstance(providers, dict):
        return providers.get(name, {})
    # Old checks only consulted Minute. Never copy them into Crowtado.
    return check if name == "minute" else {}


def confirmed_ban(email: str, check: dict, registration: dict | None = None) -> dict | None:
    """Only current account restrictions authorize permanent local exclusion."""
    for name in SERVICES:
        current = provider(check, name)
        issue = current.get("issue", {})
        # A payout hold is not a ban on the account's access.
        if (current.get("status") == "disabled" and
                issue.get("code") == "restricted" and
                issue.get("restriction_confirmed") is True and
                current.get("restriction_kind") != "payout"):
            return {**issue, "email": email, "provider": name}
    registration = registration or {}
    for step_name, step in registration.get("steps", {}).items():
        if step.get("code") != "restricted":
            continue
        name = "minute" if step_name in {"minute_register", "validate"} else "crowtado"
        current = provider(check, name)
        try:
            cleared = (current.get("status") == "active" and
                       datetime.fromisoformat(current["checked_at"]) >=
                       datetime.fromisoformat(registration["updated_at"]))
        except (KeyError, TypeError, ValueError):
            cleared = False
        if not cleared:
            return {"email": email, "provider": name, "code": "restricted",
                    "restriction_confirmed": True, "stage": "Cadastro · " + name.title(),
                    "reason": name.title() + " confirmou banimento durante o cadastro."}
    return None


def aggregate(email: str, providers: dict[str, dict]) -> dict:
    restricted = [name for name in SERVICES
                  if providers[name].get("issue", {}).get("restriction_confirmed") is True]
    pending = [name for name in SERVICES
               if providers[name]["status"] not in ("active", "not_applicable", "disabled")]
    issues = [providers[name]["issue"] for name in SERVICES if providers[name].get("issue")]
    if restricted:
        status = "disabled"
        label = "Banimento: " + " e ".join(name.title() for name in restricted)
    elif pending:
        status = (providers[pending[0]]["status"] if len(pending) == 1 else "inconclusive")
        label = "Verificação pendente: " + " e ".join(name.title() for name in pending)
    else:
        status, label = "active", "Serviços verificados"
    minute = providers["minute"]
    result = {"email": email, "status": status, "status_label": label,
              "providers": providers, "restricted_providers": restricted,
              "pending_providers": pending, "issues": issues,
              "attempts": max(p.get("attempts", 0) for p in providers.values()),
              "checked_at": max(p["checked_at"] for p in providers.values())}
    for key in ("org_key", "expires_at"):
        if key in minute:
            result[key] = minute[key]
    if issues:
        result["issue"] = next((issue for issue in issues if issue.get("restriction_confirmed")), issues[0])
        result["error"] = "\n".join(p["error"] for p in providers.values() if p.get("error"))
    return result


def preserve_history(candidate: dict, previous: dict) -> None:
    candidate["last_success_at"] = (candidate["checked_at"] if candidate["status"] == "active"
                                    else previous.get("last_success_at"))
    if not isinstance(candidate.get("providers"), dict):
        return
    for name, current in candidate["providers"].items():
        old = provider(previous, name)
        current["last_success_at"] = (current["checked_at"] if current["status"] == "active"
                                       else old.get("last_success_at"))
        if current.get("issue", {}).get("restriction_confirmed") is True:
            current["last_restriction"] = {"checked_at": current["checked_at"], "issue": current["issue"]}
        elif current["status"] not in ("active", "not_applicable"):
            restriction = old.get("last_restriction")
            if not restriction and old.get("issue", {}).get("restriction_confirmed") is True:
                restriction = {"checked_at": old.get("checked_at", ""), "issue": old["issue"]}
            if restriction:
                current["last_restriction"] = restriction


def validate(check: Any, owner: str) -> None:
    """Reject corrupt nested records before assigning diagnoses to an identity."""
    if not isinstance(check, dict):
        raise ValueError("Invalid account diagnostic")
    if "email" in check and (not isinstance(check["email"], str) or check["email"].strip().casefold() != owner):
        raise ValueError("Invalid diagnostic owner")
    for field in ("status", "status_label", "checked_at", "error", "org_key", "restriction_kind"):
        if field in check and not isinstance(check[field], str):
            raise ValueError("Invalid diagnostic text")
    if "attempts" in check and (type(check["attempts"]) is not int or check["attempts"] < 0):
        raise ValueError("Invalid diagnostic attempts")
    if check.get("last_success_at") is not None and not isinstance(check["last_success_at"], str):
        raise ValueError("Invalid diagnostic date")
    for field in ("history_saved", "permanently_removed"):
        if field in check and type(check[field]) is not bool:
            raise ValueError("Invalid diagnostic flag")
    if "payout_available" in check and type(check["payout_available"]) is not bool:
        raise ValueError("Invalid payout availability")
    if "issues" in check and not isinstance(check["issues"], list):
        raise ValueError("Invalid diagnostic issues")
    for issue in ([check["issue"]] if "issue" in check else []) + check.get("issues", []):
        if not isinstance(issue, dict):
            raise ValueError("Invalid diagnostic issue")
        if "email" in issue and (not isinstance(issue["email"], str) or issue["email"].strip().casefold() != owner):
            raise ValueError("Invalid issue owner")
        for field in ("restriction_confirmed", "retryable"):
            if field in issue and type(issue[field]) is not bool:
                raise ValueError("Invalid issue flag")
        for field in ("code", "reason", "action", "stage", "provider"):
            if field in issue and not isinstance(issue[field], str):
                raise ValueError("Invalid issue text")
    if "providers" in check:
        providers = check["providers"]
        if not isinstance(providers, dict) or set(providers) != set(SERVICES):
            raise ValueError("Invalid diagnostic services")
        for name, current in providers.items():
            if not isinstance(current, dict) or "providers" in current:
                raise ValueError("Invalid service diagnostic")
            validate(current, owner)
            if current.get("status") not in {"active", "disabled", "inconclusive", "needs_reauth", "needs_org", "not_applicable"} or not isinstance(current.get("checked_at"), str):
                raise ValueError("Invalid service state")
            issue = current.get("issue", {})
            if issue.get("provider", name) != name or (current["status"] == "disabled") != (issue.get("restriction_confirmed") is True):
                raise ValueError("Inconsistent service restriction")
    if "last_restriction" in check:
        prior = check["last_restriction"]
        if not isinstance(prior, dict) or set(prior) != {"checked_at", "issue"}:
            raise ValueError("Invalid previous restriction")
        validate(prior, owner)
        if prior["issue"].get("restriction_confirmed") is not True:
            raise ValueError("Unconfirmed previous restriction")
