"""Read model shared by the wallet and withdrawal preflight. No remote effects."""
from __future__ import annotations

from datetime import datetime
import time
from typing import Any

MAX_AGE_S = 24 * 3600
MONEY_FIELDS = ("availableCents", "pendingCents", "inTransitCents", "lifetimeCents")
EXTRA_MONEY_FIELDS = ("onHoldCents", "notApprovedCents")


def restriction(record: dict[str, Any], state: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    """A working authentication login does not clear a payout restriction."""
    hold = record.get("onHoldReason") or record.get("holdReason")
    eligibility = record.get("contributorEligibility")
    if state["confirmed"] and isinstance(eligibility, dict) and eligibility.get("checked") is True:
        reasons = eligibility.get("reasons", [])
        if (eligibility.get("withdrawalOverride") is not True and eligibility.get("blocked") is True
                and "account" in reasons):
            return {"code": "disabled", "label": "Conta desativada · saque retido", "confirmed": True,
                    "reason": "A Crowtado informa que a conta Minute está desativada. Fale com o suporte da Crowtado; trocar a senha não remove essa retenção."}
        if eligibility.get("withdrawalOverride") is not True and eligibility.get("blocked") is True:
            label = "VPN detectada" if "vpn" in reasons else "Dispositivo não compatível" if "device" in reasons else "Saque pausado"
            return {"code": "payout_blocked", "label": label, "confirmed": True,
                    "reason": "A Crowtado informou um bloqueio de elegibilidade de saque: " + label + ". Confira o painel e verifique novamente."}
    if state["confirmed"] and isinstance(hold, str) and hold:
        disabled = any(text in hold.casefold() for text in ("conta desativada", "account disabled", "account deactivated", "cuenta deshabilitada"))
        return {"code": "disabled" if disabled else "hold", "label": "Conta desativada · saque retido" if disabled else "Saque retido",
                "confirmed": True, "reason": hold}
    if isinstance(hold, str) and hold:
        return {"code": "historical_hold", "label": "Retenção registrada · confirmar", "confirmed": False,
                "reason": "A última leitura informou retenção: " + hold + ". A consulta atual é inconclusiva; não presume liberação."}
    issue = record.get("issue") if isinstance(record.get("issue"), dict) else {}
    health = account.get("last_check") if isinstance(account.get("last_check"), dict) else {}
    health_issue = health.get("issue") if isinstance(health.get("issue"), dict) else {}
    if issue.get("restriction_confirmed") is True or health_issue.get("restriction_confirmed") is True:
        evidence = issue if issue.get("restriction_confirmed") is True else health_issue
        return {"code": "restricted", "label": "Restrição registrada", "confirmed": True,
                "reason": str(evidence.get("reason") or "Restrição confirmada na última verificação da plataforma.")}
    if state["confirmed"] and isinstance(eligibility, dict) and eligibility.get("checked") is True and (
            eligibility.get("withdrawalOverride") is True or eligibility.get("available") is True and eligibility.get("blocked") is False):
        return {"code": "clear", "label": "Sem retenção informada", "confirmed": True,
                "reason": "A última leitura da Crowtado não informou retenção de saque. O estado será verificado novamente antes do envio."}
    return {"code": "unknown", "label": "Restrição não verificada", "confirmed": False,
            "reason": "A leitura não permite confirmar se há restrição de saque. Um login aceito não comprova elegibilidade."}


def valid_cents(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2**53 - 1


def reading(record: Any, *, now: float | None = None, max_age_s: int = MAX_AGE_S) -> dict[str, Any]:
    now = time.time() if now is None else now
    record = record if isinstance(record, dict) else {}
    if not record:
        return {"code": "unqueried", "label": "Ainda não consultado", "confirmed": False,
                "reason": "Consulte esta conta para obter os saldos."}
    if record.get("error") or record.get("stale"):
        issue = record.get("issue") if isinstance(record.get("issue"), dict) else {}
        label = {"authentication": "Reconectar Crowtado", "network": "Falha de conexão",
                 "timeout": "Tempo de consulta esgotado", "rate_limit": "Consulta limitada",
                 "service": "Crowtado indisponível", "invalid_response": "Resposta incompleta",
                 "restricted": "Restrição confirmada", "crowtado_account_missing": "Cadastro Crowtado não encontrado"}.get(issue.get("code"), "Consulta inconclusiva")
        return {"code": "error", "label": label, "confirmed": False,
                "reason": str(record.get("error") or "O saldo salvo precisa de uma nova consulta.")}
    if not all(valid_cents(record.get(field)) for field in MONEY_FIELDS):
        return {"code": "invalid", "label": "Saldo incompleto", "confirmed": False,
                "reason": "A leitura salva não contém todos os valores válidos. Consulte novamente."}
    if any(field in record and not valid_cents(record[field]) for field in EXTRA_MONEY_FIELDS):
        return {"code": "invalid", "label": "Valores inválidos", "confirmed": False,
                "reason": "Os valores adicionais da leitura são inválidos. Consulte novamente."}
    if record.get("currency", "USD") != "USD":
        return {"code": "invalid", "label": "Moeda não confirmada", "confirmed": False,
                "reason": "A leitura não confirma valores em USD. Confira a conta na Crowtado."}
    try:
        updated = datetime.fromisoformat(str(record["updated_at"]).replace("Z", "+00:00"))
        if updated.tzinfo is None:
            raise ValueError("timezone missing")
        age = now - updated.timestamp()
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return {"code": "invalid", "label": "Data não confirmada", "confirmed": False,
                "reason": "A data da leitura é inválida. Consulte novamente."}
    if age < 0 or age >= max_age_s:
        return {"code": "expired", "label": "Leitura vencida", "confirmed": False,
                "reason": "Consulte novamente: a leitura está fora da validade de 24 horas.",
                "age_seconds": max(0, int(age))}
    return {"code": "confirmed", "label": "Saldo confirmado", "confirmed": True,
            "reason": "Leitura completa confirmada nas últimas 24 horas.", "age_seconds": int(age)}


def payout(record: dict[str, Any], state: dict[str, Any], connected: bool,
           kind: str = "crowtado") -> dict[str, Any]:
    def blocked(code, label, reason):
        return {"code": code, "label": label, "reason": reason, "eligible": False}
    if kind != "crowtado":
        return blocked("not_applicable", "Não se aplica", "Saldo Crowtado não se aplica a esta conta Claru.")
    if not connected:
        return blocked("disconnected", "Conectar acesso", "Conecte o acesso Crowtado desta conta.")
    if not state["confirmed"]:
        return blocked("refresh", "Atualizar saldo", state["reason"])
    if record.get("currency", "USD") != "USD":
        return blocked("currency", "Moeda não suportada", "Confirme a moeda na Crowtado antes de solicitar saque.")
    if record.get("onHoldReason") or record.get("holdReason"):
        return blocked("hold", "Em retenção", "A Crowtado informa uma retenção. Confira os detalhes antes de sacar.")
    eligibility = record.get("contributorEligibility")
    if not isinstance(eligibility, dict) or eligibility.get("checked") is not True:
        return blocked("eligibility_unknown", "Verificar elegibilidade", "O saldo foi lido, mas a elegibilidade de saque do Minute não foi confirmada pela Crowtado. Consulte novamente.")
    if eligibility.get("withdrawalOverride") is not True and (
            eligibility.get("available") is not True or eligibility.get("blocked") is not False):
        return blocked("eligibility_blocked", "Saque pausado", "A Crowtado informa elegibilidade pendente ou bloqueada no Minute. Confira os detalhes da conta no painel Crowtado.")
    if record.get("inTransitCents", 0) > 0 and record.get("inTransitReplaceable") is not True:
        return blocked("in_transit", "Pagamento em trânsito", "Aguarde o pagamento em trânsito e consulte novamente.")
    if record["availableCents"] <= 2500:
        return blocked("minimum", "Abaixo do limite", "O saque no QMoney exige saldo aprovado superior a US$ 25,00.")
    minimum = record.get("withdrawMinimumCents")
    if valid_cents(minimum) and record["availableCents"] < minimum:
        return blocked("minimum", "Abaixo do mínimo do método", "O saldo não atingiu o mínimo informado pela Crowtado.")
    return {"code": "ready", "label": "Saldo elegível", "eligible": True,
            "reason": "Saldo elegível para solicitar saque. O método e o destino serão verificados antes do envio."}


def snapshot(accounts: list[dict[str, Any]], balances: dict[str, Any], connected: set[str],
             kinds: dict[str, str], runner: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    rows = {}
    counts = {key: 0 for key in ("accounts", "crowtado", "claru", "connected", "confirmed", "attention", "eligible")}
    totals = {field: 0 for field in (*MONEY_FIELDS, *EXTRA_MONEY_FIELDS)}
    field_coverage = {field: 0 for field in totals}
    historical = {field: 0 for field in totals}
    current_email = runner.get("current_email") if runner.get("state") == "running" else None
    withdrawable = 0
    for account in accounts:
        email = str(account["email"])
        kind = kinds.get(email, "crowtado")
        record = balances.get(email) if isinstance(balances.get(email), dict) else {}
        state = reading(record, now=now)
        account_restriction = restriction(record, state, account)
        is_connected = email in connected and kind == "crowtado"
        withdraw = payout(record, state, is_connected, kind)
        if account_restriction["code"] in {"disabled", "restricted"}:
            withdraw = {"code": "restricted", "label": account_restriction["label"],
                        "reason": account_restriction["reason"], "eligible": False}
        counts["accounts"] += 1
        counts["claru" if kind == "claru" else "crowtado"] += 1
        counts["connected"] += int(is_connected)
        confirmed = state["confirmed"] and kind == "crowtado"
        counts["confirmed"] += int(confirmed)
        counts["attention"] += int(kind == "crowtado" and (not confirmed or not is_connected
            or withdraw["code"] in {"hold", "restricted", "eligibility_blocked", "eligibility_unknown"}))
        counts["eligible"] += int(withdraw["eligible"])
        if withdraw["eligible"]:
            withdrawable += record["availableCents"]
        if kind == "crowtado":
            target = totals if confirmed else historical
            for field in target:
                value = record.get(field)
                if valid_cents(value):
                    target[field] += value
                    if confirmed:
                        field_coverage[field] += 1
        rows[email] = {"kind": kind, "connected": is_connected, "reading": state,
                       "payout": withdraw, "restriction": account_restriction, "refresh_needed": is_connected and (
                           not state["confirmed"] or withdraw["code"] == "eligibility_unknown"),
                       "can_refresh": is_connected, "refreshing": email == current_email,
                       "updated_at": record.get("updated_at"), "checked_at": record.get("checked_at"),
                       "issue": record.get("issue"),
                       "pending_is_estimate": record.get("pendingIsEstimate") is True}
    # JSON numbers above the exact integer range cannot be trusted by the desktop.
    safe_totals = {field: value if valid_cents(value) else None for field, value in totals.items()}
    return {"accounts": rows, "counts": counts, "totals": safe_totals, "historical_totals": historical,
            "field_coverage": field_coverage,
            "withdrawable_total_cents": withdrawable if valid_cents(withdrawable) else None,
            "as_of": datetime.fromtimestamp(now).astimezone().isoformat(), "max_age_seconds": MAX_AGE_S}
