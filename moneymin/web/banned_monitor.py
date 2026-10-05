"""Read-only checks for archived identities; never restores active credentials."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from .. import config, crowtado, minute_api, org_policy, registration_proxy
from . import account_health
from .account_issues import account_issue


def check_status(email: str, password: str) -> dict:
    status, body = minute_api._request(minute_api._SIGNIN_URL, "POST", body={
        "email": email, "password": password, "returnSecureToken": True}, timeout=20)
    if status != 200:
        raise minute_api._auth_failure(status, body, "Consulta do banimento", firebase=True)
    auth = json.loads(body)
    token = auth.get("idToken")
    if not isinstance(token, str) or not token:
        raise ValueError("Resposta inválida de autenticação")
    headers = {"Authorization": f"Bearer {token}", "X-App-Version": config.APP_VERSION,
               "User-Agent": config.USER_AGENT, "Accept": "application/json"}

    def get(path):
        code, text = minute_api._request(config.BASE_URL + path, headers=headers, timeout=20)
        if code != 200:
            raise minute_api._auth_failure(code, text, "Consulta do banimento")
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError("Resposta inválida do serviço")
        return result

    profile = get("/api/v1/users/me")
    if profile.get("disabled") is True:
        return {"status": "banned", "status_label": "Continua banida"}
    organizations = profile.get("organizations")
    if not isinstance(organizations, list):
        raise ValueError("Perfil incompleto")
    key = org_policy.target_org_key(email)
    org = next((o for o in organizations if isinstance(o, dict) and o.get("resourceKey") == key), None)
    if not org:
        return {"status": "inconclusive", "status_label": "Organização ausente"}
    if org.get("disabled") is True:
        return {"status": "banned", "status_label": "Continua suspensa na organização"}
    state = get(f"/api/v1/organizations/{key}/quality-screen").get("userState")
    if state in ("on_hold", "inactive"):
        return {"status": "banned", "status_label": "Continua suspensa"}
    if profile.get("disabled") is False and state == "active":
        return {"status": "unbanned", "status_label": "Desbanida"}
    return {"status": "inconclusive", "status_label": "Verificação inconclusiva"}


def inspect_account(row: dict) -> dict:
    try:
        with registration_proxy.route(registration_proxy.assign(row["email"], "")):
            return _inspect_account(row)
    except Exception as exc:
        now = datetime.now(timezone.utc).isoformat()
        checks = {name: _failed_check(row["email"], name, exc, now) for name in account_health.SERVICES}
        result = account_health.aggregate(row["email"], checks)
        account_health.preserve_history(result, row.get("monitor") or {})
        result.update(balance=(row.get("monitor") or {}).get("balance"), balance_stale=True,
                      balance_updated_at=(row.get("monitor") or {}).get("balance_updated_at"), balance_status="error")
        return result


def _failed_check(email: str, name: str, exc: Exception, now: str) -> dict:
    issue = account_issue(email, exc, stage=f"Verificação {name.title()}")
    issue["provider"] = name
    return {"email": email, "checked_at": now, "attempts": 1,
            "status": "disabled" if issue["restriction_confirmed"] else "inconclusive",
            "status_label": "Restrição confirmada" if issue["restriction_confirmed"] else "Verificação inconclusiva",
            "issue": issue, "error": issue["reason"]}


def _inspect_account(row: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    email, password = row["email"], row.get("password")
    previous = row.get("monitor") or {}
    result = {"checked_at": now, "balance": previous.get("balance"),
              "balance_updated_at": previous.get("balance_updated_at"), "balance_stale": True}
    if not password:
        return {**result, "status": "missing_password", "status_label": "Sem senha salva",
                "detail": "A senha não estava disponível no registro do banimento."}
    checks = {}
    try:
        minute = check_status(email, password)
        if minute["status"] == "banned":
            checks["minute"] = _failed_check(email, "minute", minute_api.AuthError("Restrição", code="restricted"), now)
        else:
            checks["minute"] = {"email": email, "checked_at": now, "attempts": 1,
                "status": "active" if minute["status"] == "unbanned" else "inconclusive",
                "status_label": "Acesso verificado" if minute["status"] == "unbanned" else "Verificação inconclusiva"}
    except Exception as exc:
        checks["minute"] = _failed_check(email, "minute", exc, now)
    if org_policy.account_kind(email) == "claru":
        result.update(balance_status="not_applicable", balance_detail="Saldo Crowtado não se aplica à conta Claru.")
        checks["crowtado"] = {"email": email, "status": "not_applicable", "status_label": "Não se aplica", "attempts": 0, "checked_at": now}
    else:
        session = None
        try:
            # API only. Never restore tokens or request a withdrawal.
            session = crowtado.login(email, password)
            payload = crowtado._site_trpc(session, "payouts.summary", None, method="GET")
            balance = crowtado._summary_from_payload(payload)
            if not balance:
                raise crowtado.CrowtadoError("Saldo incompleto", code="invalid_response")
            result.update(balance=balance, balance_updated_at=now, balance_stale=False, balance_status="ok")
            eligibility = ({} if balance.get("onHoldReason") or balance.get("holdReason") else
                           crowtado._site_trpc(session, "externalMobileCapture.eligibilityStatus", None, method="GET"))
            site = crowtado._restricoes_from_summary(balance, eligibility)
            if site["restricted"]:
                check = _failed_check(email, "crowtado", crowtado.CrowtadoError("Retenção", code="restricted"), now)
                check.update(status_label="Banida · saque suspenso", restriction_kind="payout", access_status="active")
                check["issue"]["reason"] = check["error"] = site["reason"]
                checks["crowtado"] = check
            else:
                checks["crowtado"] = {"email": email, "status": "active", "status_label": "Sem restrição informada",
                    "access_status": "active", "payout_available": site["payout_available"], "checked_at": now, "attempts": 1}
        except Exception as exc:
            check = _failed_check(email, "crowtado", exc, now)
            if session is not None:
                check.update(access_status="active", status_label="Login aceito · saque não verificado")
            elif check["status"] == "disabled":
                check.update(access_status="disabled", restriction_kind="account")
            checks["crowtado"] = check
            if result.get("balance_status") != "ok":
                result.update(balance_status="error", balance_detail=check["error"])
    diagnostic = account_health.aggregate(email, checks)
    account_health.preserve_history(diagnostic, previous)
    result.update(diagnostic)
    result["status"] = {"active": "unbanned", "disabled": "banned"}.get(result["status"], result["status"])
    result["detail"] = result.get("error", "")
    return result


class BannedMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"state": "idle", "completed": 0, "total": 0}

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self, rows, save):
        with self.lock:
            if self.state["state"] == "running":
                raise RuntimeError("Uma consulta das banidas já está em andamento.")
            self.state = {"state": "running", "completed": 0, "total": len(rows)}
            try:
                threading.Thread(target=self._run, args=(rows, save), daemon=True, name="banned-monitor").start()
            except Exception as exc:
                self.state.update(state="error", error="Não foi possível iniciar a consulta. Tente novamente.")
                raise RuntimeError(self.state["error"]) from exc

    def _run(self, rows, save):
        try:
            for row in rows:
                result = inspect_account(row)
                save(row["email"], result)
                with self.lock:
                    self.state["completed"] += 1
            with self.lock:
                self.state["state"] = "completed"
        except Exception:
            with self.lock:
                self.state["state"] = "error"
                self.state["error"] = "Não foi possível concluir e salvar a consulta. Tente novamente."
