"""Read-only checks for archived identities; never restores active credentials."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

from .. import config, crowtado, minute_api, org_policy
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
    now = datetime.now(timezone.utc).isoformat()
    email, password = row["email"], row.get("password")
    previous = row.get("monitor") or {}
    result = {"checked_at": now, "balance": previous.get("balance"),
              "balance_updated_at": previous.get("balance_updated_at"), "balance_stale": True}
    if not password:
        return {**result, "status": "missing_password", "status_label": "Sem senha salva",
                "detail": "A senha não estava disponível no registro do banimento."}
    try:
        result.update(check_status(email, password))
    except Exception as exc:
        issue = account_issue(email, exc)
        result.update(status="banned" if issue["restriction_confirmed"] else "inconclusive",
                      status_label="Continua banida" if issue["restriction_confirmed"] else "Verificação inconclusiva",
                      detail=issue["reason"])
    if org_policy.account_kind(email) == "claru":
        result.update(balance_status="not_applicable", balance_detail="Saldo Crowtado não se aplica à conta Claru.")
        return result
    try:
        # API only. No browser fallback, payout request, token file or cached session.
        session = crowtado.login(email, password)
        payload = crowtado._site_trpc(session, "payouts.summary", None, method="GET")
        balance = crowtado._summary_from_payload(payload)
        if not balance:
            raise ValueError("Resposta inválida do saldo")
        result.update(balance=balance, balance_updated_at=now, balance_stale=False, balance_status="ok")
    except Exception as exc:
        result.update(balance_status="error", balance_detail=account_issue(email, exc)["reason"])
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
            threading.Thread(target=self._run, args=(rows, save), daemon=True, name="banned-monitor").start()

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
