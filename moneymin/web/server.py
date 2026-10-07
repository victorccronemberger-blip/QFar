"""
server.py — Servico local da interface Qt do QMoney (Flask).

Endpoints JSON consumidos exclusivamente pelo aplicativo desktop:

  Contas
    GET    /api/accounts                 lista contas (token_*.json em secrets/)
    POST   /api/accounts                 {email, password} -> login (adicionar)
    POST   /api/accounts/register        {email, password} -> cria conta no Minute
                                         (convite fixo), salva token + senha p/ Saldos
    DELETE /api/accounts/<email>         remove a conta (apaga o token)
    POST   /api/accounts/<email>/check   valida token + resolve org_key (cacheia)
    POST   /api/accounts/check-all       valida todas em paralelo e separa desativadas

  Categorias (tasks do Minute cruzadas com cenários Ego4D elegíveis)
    GET    /api/tasks?email=...          lista categorias elegíveis p/ campanha

  Preferências (data/webui_prefs.json)
    GET    /api/preferences
    PUT    /api/preferences              merge raso do body nas prefs

  Campanha (uma por vez — ver web.runner)
    POST   /api/campaigns                inicia; 400 body inválido; se ocupado,
                                         devolve a campanha existente (idempotente)
                                         body: {accounts, tasks, count,
                                         min_dur_s, max_dur_s,
                                         delay_mode (off|clip|fixed), delay_s,
                                         paralelismo automático entre contas,
                                         active_hours: [7, 18] | null}
    GET    /api/campaigns/current?since=N  estado + eventos novos (polling)
    POST   /api/campaigns/stop           parada cooperativa

  Acelerador (pré-cache retomável de HoloAssist ou Ego4D)
    GET    /api/holo-cache               cobertura local + estado do runner
                                         ?provider=holoassist|ego4d
    POST   /api/holo-cache/start         inicia download/normalização em 2º plano
    POST   /api/holo-cache/stop          para depois do clipe atual

  Armazenamento
    POST   /api/storage/cleanup           remove mídia/IMU baixada e derivados

  Histórico
    GET    /api/logs                     lista data/campaign_*.json (resumo)
    GET    /api/logs/<nome>              detalhe do log
    POST   /api/logs/<nome>/status       consulta status das sessões no backend

  Saldos (crowtado — cache em data/balances.json)
    GET    /api/balances                 saldos cacheados + estado do runner
    POST   /api/balances/refresh         {emails?} -> consulta em 2º plano (409 se ocupado)
    POST   /api/balances/payout-methods/apply-all -> configura método em todas as contas Crowtado
    POST   /api/balances/withdraw        {email} -> solicita saque pelo método preferido
    POST   /api/balances/withdraw-all    solicita saques para contas elegíveis
    PUT    /api/balances/credentials     {email, password} -> salva senha do crowtado
                                         (secrets/crowtado_passwords.json)

  Registro de enviados (data/sent_videos.json — dedup entre campanhas)
    GET    /api/sent                     resumo por cenário (clipes/envios)
    POST   /api/sent/reset               limpa o registro (body: {scenario?})
"""
from __future__ import annotations

import configparser
import datetime
from functools import wraps
import hashlib
import json
import math
import re
import os
import platform
import random
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from flask import Flask, g, jsonify, request

from .. import banned_store
from .. import (
    campaign, config, crowtado, ego4d, ego4d_library, ego_accelerator, fx, holo_accelerator, holoassist,
    hostinger_mail, identity, org_policy, readiness, sent_registry, credential_store, prepared_library, nymeria_library,
    task_catalog as library_task_catalog,
)
from ..atomic_io import JsonStateError, decode_json_state, load_json, load_json_state, save_json
from ..jsonl_history import decode_jsonl_history
from .. import account_transfer, account_bans, token_store
from ..campaign import AccountSpec, CampaignConfig, TaskSpec
from ..minute_api import AuthError, Session, login
from ..secure_store import SecureStoreError, load_secure_settings, save_secure_settings
from .account_issues import account_issue, issue_text
from . import wallet, registration_state, account_health
from .catalog_loader import CatalogLoader
from .library_runner import LibraryPreparationRunner
from .org_migration import OrgMigrationRunner
from .banned_monitor import BannedMonitor
from .runner import (
    BALANCES_RUNNER, HOLO_CACHE_RUNNER, RUNNER, friendly_campaign_error, _public_event,
)

PREFS_PATH = config.DATA_DIR / "webui_prefs.json"
BALANCES_PATH = config.DATA_DIR / "balances.json"
CROWTADO_PW_PATH = config.SECRETS_DIR / "crowtado_passwords.json"
MAX_DUR_S = 1800.0  # cap do recording-config do Minute
_PERSISTENCE_LOCK = threading.RLock()
_INTEGRATION_OPERATION_LOCK = threading.Lock()
_ACCOUNT_OPERATION_LOCK = threading.Lock()
ORG_MIGRATION = OrgMigrationRunner(config.DATA_DIR / "org_migration_report.json")
ACCOUNT_HEALTH_PATH = config.DATA_DIR / "account_health.json"
_BULK_REGISTER_LOCK = threading.Lock()
_BULK_REGISTER_STATE: dict[str, Any] = {"state": "idle"}
_HEAVY_RUNNER_LOCK = threading.Lock()
from .. import recovery, campaign_start_store
from .. import campaign_reset
from ..campaign_state import campaign_state_lease, CampaignStateLeaseError
from ..operation_lease import OperationLeaseError
RECOVERY = recovery.RecoveryRunner()
_WITHDRAW_LOCK = threading.Lock()
_PAYOUT_OPERATION_LOCK = threading.Lock()
_WALLET_COMMAND_LOCK = threading.Lock()


def _serialize_wallet_command(handler):
    """Admit one command at a time before publishing its worker state."""
    @wraps(handler)
    def command(*args, **kwargs):
        if not _WALLET_COMMAND_LOCK.acquire(blocking=False):
            return jsonify({"error": "aguarde a operação atual da Carteira terminar"}), 409
        try:
            return handler(*args, **kwargs)
        finally:
            _WALLET_COMMAND_LOCK.release()
    return command
_WITHDRAW_IN_FLIGHT: set[str] = set()
_WITHDRAW_LAST_REQUEST: dict[str, float] = {}
_WITHDRAW_COOLDOWN_S = 60.0
_WITHDRAW_BULK_LOCK = threading.Lock()
_WITHDRAW_BULK_STATE: dict[str, Any] = {"state": "idle", "total": 0, "done": 0, "results": []}
_WITHDRAW_BULK_LOADED = False
_WITHDRAW_COOLDOWN_LOADED = False
_PAYOUT_METHOD_LOCK = threading.Lock()
_PAYOUT_METHOD_STATE: dict[str, Any] = {"state": "idle", "total": 0, "done": 0, "results": []}


def _payout_method_snapshot() -> dict[str, Any]:
    with _PAYOUT_METHOD_LOCK:
        return {**_PAYOUT_METHOD_STATE, "results": list(_PAYOUT_METHOD_STATE["results"])}


def _payout_method_run(creds: dict[str, str], method: str,
                       legal_name: str, destination_email: str) -> None:
    blocked = False
    for email, password in creds.items():
        with _PAYOUT_METHOD_LOCK:
            _PAYOUT_METHOD_STATE["current"] = email
        try:
            with _PAYOUT_OPERATION_LOCK:
                if _wise_cleanup_snapshot().get("pending"):
                    raise ValueError("Conclua a limpeza Wise pendente antes de configurar métodos.")
                crowtado.configurar_metodo_saque(email, password, method,
                                                 legal_name, destination_email)
            result = {"email": email, "ok": True, "message": "configurado"}
        except Exception as exc:
            # A resposta pode repetir senha, token ou URL assinada. Só a
            # classificação conhecida entra no estado consultado pela UI.
            issue = account_issue(email, exc, stage="Configuração de saque Crowtado")
            result = {"email": email, "ok": False, "code": issue["code"],
                      "message": issue["reason"] + " " + issue["action"]}
            if method == "wise" and (getattr(exc, "account_issue_code", None) == "destination_in_use"
                                     or "already linked to anoth" in str(exc).casefold()):
                blocked = True
                result.update(code="destination_in_use",
                    message="A Crowtado informou que este destino Wise já está vinculado a outra conta.")
        with _PAYOUT_METHOD_LOCK:
            _PAYOUT_METHOD_STATE["results"].append(result)
            _PAYOUT_METHOD_STATE["done"] += 1
            _PAYOUT_METHOD_STATE["current"] = None
        if blocked:
            break
    with _PAYOUT_METHOD_LOCK:
        _PAYOUT_METHOD_STATE["state"] = "blocked" if blocked else "done"
        if blocked:
            _PAYOUT_METHOD_STATE["message"] = (
                "A Crowtado não permite vincular este mesmo destino Wise a outra conta. "
                "As contas restantes não foram alteradas.")


def _withdraw_bulk_path() -> Path:
    return config.DATA_DIR / "withdraw_bulk_state.json"


def _withdraw_cooldown_path() -> Path:
    return config.DATA_DIR / "withdraw_request_times.json"


def _wise_cleanup_path() -> Path:
    return config.DATA_DIR / "wise_cleanup_pending.json"


def _wise_cleanup_snapshot() -> dict[str, Any]:
    # Corrupção não pode ser interpretada como ausência de pendência.
    try:
        state = json.loads(_wise_cleanup_path().read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("pending"), bool):
            raise ValueError("invalid cleanup state")
        return state
    except FileNotFoundError:
        return {"pending": False}
    except (OSError, ValueError):
        return {"pending": True, "error": "Não foi possível ler a pendência Wise; saques bloqueados."}


def _finish_wise_cleanup(email: str, password: str) -> dict[str, Any]:
    result = {"wiseDestinationRemoved": False, "payoutPreferenceRestored": False}
    # Repetimos somente a limpeza, nunca a vinculação ou a solicitação de saque.
    delays = (0, 2, 4, 8, 15, 30)
    for attempt, delay in enumerate(delays, 1):
        try:
            save_json(_wise_cleanup_path(), {"pending": True, "email": email,
                "automatic": True, "attempt": attempt, "max_attempts": len(delays),
                "message": "Aguardando confirmação de Wise desvinculada e Dots restaurado."})
        except OSError:
            return {**result, "cleanupPending": True}
        if delay:
            time.sleep(delay)
        try:
            result = crowtado.finalizar_wise(email, password)
        except Exception:
            continue
        if result.get("wiseDestinationRemoved") is True and result.get("payoutPreferenceRestored") is True:
            try:
                save_json(_wise_cleanup_path(), {"pending": False})
            except OSError:
                break  # Pendência permanece: recuperação pode repetir a limpeza idempotente.
            return {**result, "cleanupPending": False}
    try:
        save_json(_wise_cleanup_path(), {"pending": True, "email": email, "automatic": False,
            "attempt": len(delays), "max_attempts": len(delays),
            "message": "A plataforma não confirmou a limpeza após as tentativas automáticas. Saques bloqueados."})
    except OSError:
        pass
    return {**result, "cleanupPending": True}


def _withdraw_wise_flow(email: str, password: str, wise: dict[str, str]) -> dict[str, Any]:
    # Gravado ANTES de qualquer mutação remota, também cobre falha parcial do vínculo.
    save_json(_wise_cleanup_path(), {"pending": True, "email": email,
                                    "started_at": time.time()})
    result = {"status": "unknown"}
    stage = "configure_wise"
    try:
        crowtado.configurar_metodo_saque(email, password, "wise", **wise)
        stage = "request_withdrawal"
        result = crowtado.solicitar_link_saque(email, password, expected_method="wise", cleanup_wise=False)
    except Exception as exc:
        # A failure while linking cannot have submitted a withdrawal. Once
        # the withdrawal function is entered, retain the conservative outcome.
        code = getattr(exc, "account_issue_code", None)
        known_codes = {"authentication", "crowtado_account_missing", "rate_limit",
                       "service", "forbidden", "invalid_response", "network", "timeout",
                       "tls", "email_verification", "destination_in_use", "payout_pending",
                       "payout_configuration"}
        result = {"status": "not_requested" if stage == "configure_wise" or getattr(exc, "withdrawal_attempted", True) is False else "unknown",
                  "failureStage": stage,
                  "failureCode": code if code in known_codes else "unexpected"}
        http_status = getattr(exc, "http_status", None)
        if type(http_status) is int and 400 <= http_status <= 599:
            result["httpStatus"] = http_status
    finally:
        # Preserve acceptance before recovery waits, including app/connection loss.
        try:
            save_json(config.DATA_DIR / "withdraw_last_result.json", {
                "email": email, "finished_at": time.time(),
                "result": {"status": result.get("status", "unknown"), "cleanupPending": True}})
        except OSError:
            pass
        cleanup = _finish_wise_cleanup(email, password)
    result = {**result, **cleanup}
    # Store only classified evidence, never raw API errors or destination data.
    try:
        diagnostic_fields = {"status", "failureStage", "failureCode", "httpStatus",
                             "wiseDestinationRemoved", "payoutPreferenceRestored", "cleanupPending"}
        save_json(config.DATA_DIR / "withdraw_last_result.json", {
            "email": email, "finished_at": time.time(),
            "result": {key: value for key, value in result.items() if key in diagnostic_fields}})
    except OSError:
        pass  # Losing diagnostics must not turn an accepted payout into failure.
    return result


def _load_withdraw_cooldowns_locked() -> None:
    global _WITHDRAW_COOLDOWN_LOADED
    saved = load_json_state(_withdraw_cooldown_path(), {})
    if any(not isinstance(email, str) or not email.strip()
           or not _finite_state_number(value) or value < 0
           for email, value in saved.items()):
        _invalid_local_state()
    now = time.time()
    # Future evidence just consulted remains authoritative even after loading.
    for email, value in saved.items():
        if value > now:
            _WITHDRAW_LAST_REQUEST[email] = max(float(value), _WITHDRAW_LAST_REQUEST.get(email, 0.0))
    if _WITHDRAW_COOLDOWN_LOADED:
        return
    for email, value in saved.items():
        # A future attempt is unresolved evidence after clock rollback.
        if now - value < _WITHDRAW_COOLDOWN_S:
            _WITHDRAW_LAST_REQUEST[email] = float(value)
    _WITHDRAW_COOLDOWN_LOADED = True


def _load_withdraw_bulk_locked() -> None:
    global _WITHDRAW_BULK_LOADED
    saved = load_json_state(_withdraw_bulk_path(), {})
    if saved and (not isinstance(saved.get("results"), list)
                  or any(not isinstance(row, dict) for row in saved["results"])
                  or not isinstance(saved.get("state", "idle"), str)
                  or saved.get("state", "idle") not in {"idle", "running", "done", "error", "interrupted"}
                  or any(type(saved.get(field, 0)) is not int or saved.get(field, 0) < 0
                         for field in ("total", "done"))):
        _invalid_local_state()
    if _WITHDRAW_BULK_LOADED:
        return
    if isinstance(saved, dict) and isinstance(saved.get("results"), list):
        try:
            total = max(0, int(saved.get("total") or 0))
            done = max(0, int(saved.get("done") or 0))
        except (TypeError, ValueError):
            total = done = 0
        state = {"state": saved.get("state", "idle"),
                 "method": saved.get("method"), "message": saved.get("message", ""),
                 "total": total, "done": done,
                 "results": saved["results"],
                 "current": saved.get("current"),
                 "started_at": saved.get("started_at")}
        if state["state"] == "running":
            state["state"] = "interrupted"
            state["message"] = (
                "O aplicativo foi fechado durante o lote. Confira o link da conta em andamento antes de solicitar novamente.")
            save_json(_withdraw_bulk_path(), state)
        _WITHDRAW_BULK_STATE.update(state)
    _WITHDRAW_BULK_LOADED = True


def _save_withdraw_bulk_locked() -> None:
    load_json_state(_withdraw_bulk_path(), {})
    save_json(_withdraw_bulk_path(), _WITHDRAW_BULK_STATE)


def _wise_withdraw_options(body: dict[str, Any]) -> dict[str, str] | None:
    method = body.get("method")
    if method is None:
        return None
    if method == "paypal":
        if body.get("paypal_confirmed") is not True:
            raise ValueError("confirme PayPal antes de solicitar o saque")
        if body.get("legal_name") or body.get("destination_email"):
            raise ValueError("PayPal usa o fluxo de pagamento da Crowtado, sem destino manual")
        return {"method": "paypal"}
    if method != "wise" or body.get("wise_confirmed") is not True:
        raise ValueError("confirme Wise antes de solicitar o saque")
    name = str(body.get("legal_name") or "").strip()
    destination = str(body.get("destination_email") or "").strip()
    if (len(name) < 2 or len(name) > 200 or len(destination) > 254
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", destination)):
        raise ValueError("informe o nome legal e um e-mail válido da Wise")
    return {"legal_name": name, "destination_email": destination}


def _withdraw_once(email: str, password: str,
                   wise: dict[str, str] | None = None) -> tuple[dict[str, Any], int]:
    # Inclui vínculo, saque e limpeza na mesma seção exclusiva entre contas.
    if not _PAYOUT_OPERATION_LOCK.acquire(blocking=False):
        return {"email": email, "ok": False, "error": "aguarde a operação de saque atual terminar"}, 409
    try:
        if _wise_cleanup_snapshot().get("pending"):
            return {"email": email, "ok": False, "error": (
                "Há uma limpeza Wise pendente. Use Concluir limpeza Wise antes de qualquer novo saque.")}, 409
        return _withdraw_once_locked(email, password, wise)
    finally:
        _PAYOUT_OPERATION_LOCK.release()


def _withdraw_paypal_flow(email: str, password: str) -> dict[str, Any]:
    stage = "configure_paypal"
    try:
        crowtado.configurar_metodo_saque(email, password, "paypal")
        stage = "request_withdrawal"
        return crowtado.solicitar_link_saque(email, password, expected_method="paypal")
    except Exception as exc:
        # Once submitted, a lost response must not encourage another POST.
        result = {"status": "not_requested" if stage == "configure_paypal" or getattr(exc, "withdrawal_attempted", True) is False else "unknown",
                  "failureStage": stage, "method": "paypal"}
        code = getattr(exc, "account_issue_code", None)
        if code in {"payout_configuration", "destination_in_use", "payout_pending"}:
            result["failureCode"] = code
        return result


def _withdraw_once_locked(email: str, password: str,
                          wise: dict[str, str] | None = None) -> tuple[dict[str, Any], int]:
    # An unreadable receipt is unresolved evidence, never permission to retry.
    load_json_state(config.DATA_DIR / "withdraw_last_result.json", {})
    _load_balances()
    now = time.time()
    with _WITHDRAW_LOCK:
        _load_withdraw_cooldowns_locked()
        for key, value in list(_WITHDRAW_LAST_REQUEST.items()):
            if now - value >= _WITHDRAW_COOLDOWN_S:
                del _WITHDRAW_LAST_REQUEST[key]
        if email in _WITHDRAW_IN_FLIGHT:
            return {"email": email, "ok": False, "error": "já há uma solicitação em andamento"}, 409
        elapsed = now - _WITHDRAW_LAST_REQUEST.get(email, 0.0)
        if email in _WITHDRAW_LAST_REQUEST and elapsed < 0:
            return {"email": email, "ok": False, "code": "clock_conflict",
                    "error": "O horário local antecede uma solicitação registrada; confira o relógio antes de solicitar novamente."}, 409
        if elapsed < _WITHDRAW_COOLDOWN_S:
            wait_s = max(1, math.ceil(_WITHDRAW_COOLDOWN_S - elapsed))
            return {"email": email, "ok": False,
                    "error": f"solicitação recente; aguarde {wait_s}s para repetir"}, 429
        # Grave a tentativa antes da chamada externa: após uma queda, evite
        # repetir imediatamente uma solicitação que pode ter sido aceita.
        _WITHDRAW_LAST_REQUEST[email] = now
        try:
            save_json(_withdraw_cooldown_path(), _WITHDRAW_LAST_REQUEST)
        except OSError:
            _WITHDRAW_LAST_REQUEST.pop(email, None)
            return {"email": email, "ok": False,
                    "error": "não foi possível proteger o histórico de solicitações"}, 503
        _WITHDRAW_IN_FLIGHT.add(email)
    try:
        if wise and wise.get("method") == "paypal":
            result = _withdraw_paypal_flow(email, password)
        elif wise:
            result = _withdraw_wise_flow(email, password, wise)
        else:
            try:
                result = crowtado.solicitar_link_saque(email, password)
            except Exception as exc:
                if getattr(exc, "withdrawal_attempted", None) is not False:
                    result = {"status": "unknown", "failureStage": "request_withdrawal"}
                else:
                    raise
        balance_saved = True
        if result.get("status") != "not_requested":
            try:
                _invalidate_balance_after_withdrawal(email, result)
            except (OSError, JsonStateError):
                # Provider acceptance must never become a failed/retryable withdrawal.
                balance_saved = False
        success = result.get("status") in {"ok", "review_required"}
        message = _withdraw_message(email, result)
        amount = result.get("amountCents")
        if type(amount) is int and amount >= 0 and result.get("currency") == "USD":
            message += f"; valor informado pela Crowtado: US$ {amount / 100:.2f}"
        if not balance_saved:
            message += "; não foi possível marcar o saldo anterior como desatualizado. Atualize o saldo antes de continuar; não repita este saque."
        if wise and wise.get("method") != "paypal" and result.get("cleanupPending") is False and result.get("status") != "ok":
            message += "; Wise desvinculada e Dots confirmado."
            if result.get("status") != "not_requested":
                message += " Confira o histórico antes de repetir o saque."
        response = {"ok": success, "email": email, "message": message, "result": result}
        if not success:
            response["error"] = message
        return response, 200 if success else 409
    except Exception as exc:
        if wise and wise.get("method") != "paypal":
            return {"email": email, "ok": False, "error": (
                "O fluxo Wise não foi concluído. Confira o vínculo e o histórico na Crowtado "
                "antes de repetir; nenhuma nova tentativa automática será feita.")}, 400
        if not isinstance(exc, crowtado.CrowtadoError):
            raise
        issue = account_issue(email, exc, stage="Solicitação de saque Crowtado")
        return {"email": email, "ok": False, "code": "withdrawal_not_requested",
                "error": issue["reason"] + " " + issue["action"], "issue": issue}, 400
    finally:
        with _WITHDRAW_LOCK:
            _WITHDRAW_IN_FLIGHT.discard(email)


def _withdraw_bulk_snapshot() -> dict[str, Any]:
    with _WITHDRAW_BULK_LOCK:
        _load_withdraw_bulk_locked()
        return {**_WITHDRAW_BULK_STATE, "results": list(_WITHDRAW_BULK_STATE["results"])}


def _start_withdraw_worker(eligible, wise):
    load_json_state(config.DATA_DIR / "withdraw_last_result.json", {})
    _load_balances()
    with _WITHDRAW_LOCK:
        _load_withdraw_cooldowns_locked()
    with _WITHDRAW_BULK_LOCK:
        _load_withdraw_bulk_locked()
        if _WITHDRAW_BULK_STATE["state"] == "running":
            return ({"error": "já há um saque em lote em andamento"}), 409
        _WITHDRAW_BULK_STATE.update(
            state="running", total=len(eligible), done=0, results=[],
            method=wise.get("method", "wise") if wise else "saved",
            current=None, message="", started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        try:
            _save_withdraw_bulk_locked()
        except OSError:
            _WITHDRAW_BULK_STATE["state"] = "error"
            return ({"error": "não foi possível salvar o histórico do lote"}), 503
    thread = threading.Thread(target=_withdraw_bulk_run, args=(eligible, wise) if wise else (eligible,),
                              daemon=True, name="moneymin-withdraw-bulk")
    try:
        thread.start()
    except RuntimeError:
        with _WITHDRAW_BULK_LOCK:
            _WITHDRAW_BULK_STATE.update(state="error", current=None,
                message="O processamento não pôde começar; nenhum link foi solicitado.")
            try:
                _save_withdraw_bulk_locked()
            except OSError:
                pass
        return ({"error": "não foi possível iniciar o saque em lote"}), 503
    return {"ok": True, "total": len(eligible), "background": True}, 202


def _withdraw_bulk_run(creds: dict[str, str], wise: dict[str, str] | None = None) -> None:
    for email, password in creds.items():
        with _WITHDRAW_BULK_LOCK:
            _WITHDRAW_BULK_STATE["current"] = email
            try:
                _save_withdraw_bulk_locked()
            except OSError:
                _WITHDRAW_BULK_STATE.update(
                    state="error", message="Não foi possível salvar o progresso; nenhuma nova conta será solicitada.")
                return
        try:
            result, _ = (_withdraw_once(email, password, wise) if wise
                         else _withdraw_once(email, password))
        except Exception:  # Sem recibo, o resultado externo pode ser inconclusivo.
            result = {"email": email, "ok": False,
                      "error": "A solicitação não produziu um resultado confirmado. Confira o histórico na Crowtado antes de repetir.",
                      "result": {"status": "unknown", "failureStage": "request_withdrawal"}}
        with _WITHDRAW_BULK_LOCK:
            _WITHDRAW_BULK_STATE["results"].append({
                "email": email, "ok": result["ok"],
                "message": result.get("message") or result.get("error") or "falha desconhecida",
            })
            _WITHDRAW_BULK_STATE["done"] += 1
            _WITHDRAW_BULK_STATE["current"] = None
            detail = result.get("result") or {}
            stop_wise = wise and wise.get("method") != "paypal" and not (
                result.get("ok") and detail.get("status") == "ok"
                and detail.get("wiseDestinationRemoved") is True
                and detail.get("payoutPreferenceRestored") is True
                and detail.get("cleanupPending") is not True)
            stop_uncertain = detail.get("status") in {
                "unknown", "dots_pending_retry", "manual_pending_retry",
                "tremendous_pending_retry", "stripe_global_pending_retry"}
            if stop_wise:
                _WITHDRAW_BULK_STATE.update(state="error", message=(
                    "Lote Wise interrompido. Confira a última conta: somente após saque aceito, "
                    "Wise desvinculada e Dots confirmado a próxima conta pode começar."))
            elif stop_uncertain:
                _WITHDRAW_BULK_STATE.update(state="error", message=(
                    "Lote interrompido: a última solicitação está pendente ou inconclusiva. "
                    "Confira o histórico na Crowtado antes de repetir."))
            try:
                _save_withdraw_bulk_locked()
            except OSError:
                _WITHDRAW_BULK_STATE.update(
                    state="error", message="O resultado não pôde ser salvo. Confira o link antes de repetir.")
                return
            if stop_wise or stop_uncertain:
                return
    with _WITHDRAW_BULK_LOCK:
        _WITHDRAW_BULK_STATE["state"] = "done"
        try:
            _save_withdraw_bulk_locked()
        except OSError:
            _WITHDRAW_BULK_STATE.update(
                state="error", message="O resultado final não pôde ser salvo. Confira os resultados exibidos.")


def _balance_refresh_needed(
    accounts: list[dict[str, Any]], balances: dict[str, Any],
    connected: set[str], *, max_age_s: int = 24 * 3600,
) -> list[str]:
    """Contas conectadas sem leitura recente e confirmada de saldo."""
    now = time.time()
    return sorted(str(account["email"]) for account in accounts
        if str(account.get("email") or "") in connected
        and org_policy.account_kind(str(account["email"])) == "crowtado"
        and not wallet.reading(balances.get(str(account["email"])), now=now,
                               max_age_s=max_age_s)["confirmed"])


def _confirmed_available_balance(record: Any) -> bool:
    """Crowtado: only confirmed approved funds strictly above US$25 qualify."""
    if not isinstance(record, dict) or record.get("error") or record.get("stale"):
        return False
    if crowtado.payout_in_transit(record):
        return False
    cents = record.get("availableCents")
    return (isinstance(cents, (int, float)) and not isinstance(cents, bool)
            and math.isfinite(cents) and cents > 2500)


def _banned_withdraw_eligibility(row: dict[str, Any]) -> tuple[bool, str]:
    """A elegibilidade usa somente a leitura Crowtado, nunca o status Minute."""
    email = str(row.get("email") or "")
    if org_policy.account_kind(email) != "crowtado":
        return False, "Saldo Crowtado não se aplica a esta conta."
    if not row.get("password"):
        return False, "A senha Crowtado não está salva no registro de banidas."
    monitor = row.get("monitor") if isinstance(row.get("monitor"), dict) else {}
    site_check = account_health.provider(monitor, "crowtado")
    if site_check and site_check.get("status") != "active":
        return False, "Crowtado: " + str(site_check.get("status_label") or "verificação inconclusiva") + ". Verifique novamente antes de sacar."
    if monitor.get("withdraw_result_status"):
        return False, str(monitor.get("withdraw_result_detail") or
                          "O saque já foi solicitado ou recusado; atualize o saldo antes de tentar novamente.")
    if monitor.get("balance_status") != "ok" or monitor.get("balance_stale") is not False:
        return False, "Consulte novamente o saldo Crowtado para confirmar o saque."
    if not _confirmed_available_balance(monitor.get("balance")):
        return False, "O saldo aprovado na Crowtado deve ser superior a US$ 25,00."
    try:
        checked = datetime.datetime.fromisoformat(str(monitor["balance_updated_at"]))
        age = (datetime.datetime.now(datetime.timezone.utc) - checked.astimezone(datetime.timezone.utc)).total_seconds()
    except (KeyError, TypeError, ValueError, OverflowError):
        return False, "Consulte novamente o saldo Crowtado para confirmar o saque."
    if not 0 <= age < 24 * 3600:
        return False, "A consulta de saldo expirou; atualize antes de solicitar o saque."
    return True, "Saldo disponível confirmado na Crowtado. O saque será revalidado antes da solicitação."


def _remember_banned_withdraw_result(
    email: str, status: str, detail: str, *, balance: dict[str, Any] | None = None,
    balance_error: bool = False,
) -> None:
    """Atualiza só o estado do saque; a restrição Minute permanece intacta."""
    with _PERSISTENCE_LOCK:
        path = config.DATA_DIR / "banned_accounts.json"
        archive = banned_store.load(path, {"accounts": []})
        for row in archive.get("accounts", []):
            if str(row.get("email") or "").strip().casefold() != email:
                continue
            monitor = dict(row.get("monitor") or {})
            monitor["withdraw_result_status"] = status
            monitor["withdraw_result_detail"] = detail
            if balance is not None:
                monitor["balance"] = balance
                monitor["balance_status"] = "ok"
                monitor["balance_stale"] = False
                monitor["balance_updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            elif balance_error:
                monitor["balance_status"] = "error"
                monitor["balance_stale"] = True
            row["monitor"] = monitor
            banned_store.save(path, archive)
            return

_STEP_LABELS = {
    "proxy": "conexão do proxy",
    "ban_check": "verificar bloqueio na Crowtado",
    "crowtado_signup": "criação Crowtado",
    "save_partial": "proteger credencial local",
    "demographics": "demografia Crowtado",
    "minute_register": "registro Minute",
    "link_minute": "vincular Minute na Crowtado",
    "validate": "validação pós-criação",
}
_STEP_ORDER = [
    "proxy", "ban_check", "save_partial", "crowtado_signup",
    "demographics", "minute_register", "link_minute", "validate",
]
_CROWTADO_SIGNUP_RETRIES = 2
_CROWTADO_SIGNUP_RETRY_DELAY_S = 5.0


def _save_registration_batch() -> None:
    # Called with the batch lock held. Contains progress only, never passwords.
    save_json(config.DATA_DIR / "account_registration_batch.json", _BULK_REGISTER_STATE)


def _restore_registration_batch() -> None:
    saved = load_json_state(config.DATA_DIR / "account_registration_batch.json", {"state": "idle"})
    if saved.get("state") not in ("idle", "running", "stopping", "done", "failed"):
        _invalid_local_state()
    for key in ("total", "completed", "created", "failed"):
        if key in saved and (type(saved[key]) is not int or saved[key] < 0):
            _invalid_local_state()
    if saved["state"] in ("running", "stopping"):
        saved.update(state="failed", current_email="", current_step="",
                     error="O serviço reiniciou durante o cadastro. Confira as contas incompletas e retome a mesma conta.")
    with _BULK_REGISTER_LOCK:
        _BULK_REGISTER_STATE.clear()
        _BULK_REGISTER_STATE.update(saved)


def _mark_bulk_account_removed(email: str) -> None:
    """Mantém o histórico do lote, mas torna sua ação de remoção idempotente."""
    normalized = email.strip().casefold()
    with _BULK_REGISTER_LOCK:
        for result in _BULK_REGISTER_STATE.get("results", []):
            if (isinstance(result, dict)
                    and str(result.get("email") or "").strip().casefold() == normalized):
                result["removed"] = True
                result["removable"] = False


def _step_ok(detail: str = "") -> dict[str, str]:
    return {"status": "ok", "detail": detail}


def _step_skip(detail: str = "") -> dict[str, str]:
    return {"status": "skip", "detail": detail}


def _step_fail(detail: str = "") -> dict[str, str]:
    return {"status": "fail", "detail": detail}


def _hostinger_is_configured() -> bool:
    """Ao menos um perfil Hostinger com rotas (domínio catch-all)?"""
    profiles = getattr(config, "HOSTINGER_MAIL_PROFILES", None) or []
    return any(
        bool(profile.get("routes"))
        for profile in profiles
        if isinstance(profile, dict) and str(profile.get("token") or "").strip()
    )


def _registration_domains() -> list[dict[str, str]]:
    domains = []
    seen: set[str] = set()
    for profile in hostinger_mail.configured_connections():
        for route in profile["routes"]:
            # Uma rota de destinatário individual não configura um catch-all.
            if not route or "@" in route or any(c.isspace() for c in route) or route in seen:
                continue
            seen.add(route)
            domains.append({"domain": route, "profile_id": profile.get("id", ""),
                            "profile_name": profile.get("name", "")})
    return domains


def _recover_hostinger_domains() -> list[str]:
    """Completa perfis antigos sem rotas usando a própria credencial salva."""
    warnings = []
    if not any(not p["routes"] for p in hostinger_mail.configured_connections()):
        return warnings
    # Serializa com salvar/remover integrações para não restaurar dados antigos.
    with _INTEGRATION_OPERATION_LOCK:
        secure = _migrate_legacy_integrations()
        profiles = _hostinger_profiles(secure.get("hostinger") or {})
        discovered = {}
        changed = False
        recovered = []
        for profile in profiles:
            if profile["routes"]:
                recovered.append(profile)
                continue
            token = profile["token"]
            if token not in discovered:
                try:
                    discovered[token] = hostinger_mail.discover_mailboxes(token)
                except Exception as exc:
                    discovered[token] = []
                    warnings.append(_integration_error(exc, "Hostinger"))
            matches = [mailbox for mailbox in discovered[token]
                       if not profile["mailbox_id"]
                       or mailbox["resource_id"] == profile["mailbox_id"]]
            usable = [mailbox for mailbox in matches if mailbox.get("domain")]
            if usable:
                for index, mailbox in enumerate(usable):
                    recovered.append({
                        **profile,
                        "id": profile["id"] if index == 0 else uuid.uuid4().hex,
                        "mailbox_id": mailbox["resource_id"],
                        "routes": [mailbox["domain"]],
                        "name": mailbox.get("address") or profile["name"],
                    })
                changed = True
            else:
                recovered.append(profile)
                if not warnings:
                    warnings.append("A Hostinger não informou o domínio da caixa salva. "
                                    "Identifique novamente a API em Integrações.")
        if changed:
            with _PERSISTENCE_LOCK:
                secure["hostinger"] = {"profiles": recovered}
                save_secure_settings(config.INTEGRATIONS_PATH, secure)
                _apply_hostinger(recovered)
    return warnings


def _preflight_checks(domain: str = "") -> dict[str, Any]:
    """Valida todas as dependências antes de iniciar a criação de contas.

    Retorna um dict com status de cada verificação e um flag 'ready' geral.
    """
    from .. import crowtado
    checks: dict[str, dict[str, Any]] = {}

    # 1. Hostinger Mail API
    domain = domain.strip().lower().lstrip("@")
    profiles = [p for p in hostinger_mail.configured_connections()
                if domain and domain in p["routes"]]
    if profiles:
        mailboxes = 0
        working_profiles = 0
        for profile in profiles:
            try:
                result = hostinger_mail.test_connection(
                    token=profile.get("token"), mailbox=profile.get("mailbox_id"))
                mailboxes += result["mailboxes"]
                working_profiles += 1
            except Exception:
                continue
        if working_profiles:
            checks["hostinger"] = {"ok": True, "detail": f"{mailboxes} caixa(s) para {domain}"}
        else:
            checks["hostinger"] = {"ok": False, "detail": "Nenhum perfil do domínio respondeu. Confira as credenciais e a conexão."}
    else:
        checks["hostinger"] = {"ok": False, "detail": "Selecione um domínio com rota Hostinger configurada"}

    # 2. Chrome/Playwright
    try:
        from .. import crowtado
        chrome_path = crowtado._chrome_exe()
        checks["chrome"] = {"ok": True, "detail": chrome_path}
    except Exception as exc:
        checks["chrome"] = {"ok": False, "detail": str(exc)}

    # 3. Crowtado Clerk API
    try:
        import urllib.request
        from .. import tls
        req = urllib.request.Request(
            f"{crowtado.CLERK_BASE}/v1/client",
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "curl/8.5.0")
        with tls.urlopen(req, timeout=10) as resp:
            if resp.status < 300:
                checks["crowtado_api"] = {"ok": True, "detail": "Clerk API respondendo"}
            else:
                checks["crowtado_api"] = {"ok": False, "detail": f"status {resp.status}"}
    except Exception as exc:
        checks["crowtado_api"] = {"ok": False, "detail": str(exc)}

    # 4. Minute API
    try:
        import urllib.request
        from .. import tls
        req = urllib.request.Request(
            f"{config.BASE_URL}/api/v1/health",
            method="GET",
        )
        req.add_header("User-Agent", "okhttp/4.12.0")
        with tls.urlopen(req, timeout=10) as resp:
            checks["minute_api"] = {"ok": True, "detail": f"status {resp.status}"}
    except Exception as exc:
        # Minute pode não ter /health — status 404 ainda indica que a API está de pé
        error_str = str(exc)
        if "404" in error_str or "Not Found" in error_str:
            checks["minute_api"] = {"ok": True, "detail": "APIrespondendo (sem /health)"}
        else:
            checks["minute_api"] = {"ok": False, "detail": error_str}

    # 5. Invite code — tentativa rápida de join (sem commit)
    checks["invite_code"] = {
        "ok": True,
        "detail": f"código {config.INVITE_CODE} configurado",
    }

    ready = all(check.get("ok", False) for check in checks.values())
    return {"ready": ready, "checks": checks}


def _validate_minute_membership(email: str) -> str:
    """Confirma a org obrigatória e migra Crowtado antiga para PE8EAR5V."""
    session = Session.from_email(email)
    profile = session.ensure_auth()
    organizations = [
        org for org in (profile.get("organizations") or [])
        if isinstance(org, dict)
    ]
    try:
        org_key = org_policy.ensure_membership(session, email, organizations)
    except RuntimeError as exc:
        if str(exc) == "Conta suspensa na organização de destino.":
            from ..minute_api import AuthError
            raise AuthError(str(exc), code="restricted") from None
        raise
    # Membership alone does not exclude account/org suspension or quality hold.
    session.ensure_auth(org_key=org_key)
    _cache_org_key(email, org_key)
    return org_key


def _full_register_account(email: str, password: str, identity_data: dict[str, Any],
                           *, on_step: Any = None, resume: bool = False) -> dict[str, Any]:
    from .. import registration_proxy
    from contextlib import ExitStack
    scope = ExitStack()
    try:
        saved = credential_store.lookup(config.SECRETS_DIR, email, strict=True)
        if saved and saved != password:
            raise ValueError("A conta já possui outra senha protegida. Reconecte o acesso antes de registrar.")
    except ValueError as exc:
        return {"steps": {"save_partial": _step_fail(str(exc))}, "error": str(exc), "partial": False}
    identity_data = dict(identity_data)
    previous = registration_state.load().get(email.strip().casefold(), {})
    steps = dict(previous.get("steps", {})) if resume or saved else {}
    try:
        proxy = registration_proxy.assign(email, identity_data.get("proxy_id", ""))
        if proxy:
            identity_data["proxy_id"] = proxy["id"]
        scope.enter_context(registration_proxy.route(proxy))
        if proxy:
            if callable(on_step):
                on_step("proxy")
            ip = registration_proxy.check_exit_ip()
            steps["proxy"] = _step_ok(f"{proxy['host']}:{proxy['port']} · IP de saída {ip}")
        else:
            steps["proxy"] = _step_skip("Conexão direta selecionada pelo usuário")
    except (OSError, ValueError, RuntimeError):
        scope.close()
        detail = "Não foi possível confirmar o proxy selecionado. Confira a conexão e tente novamente; nenhuma conta foi criada nesta tentativa e nenhuma conexão direta foi usada."
        steps["proxy"] = _step_fail(detail)
        if previous.get("state") != "complete":
            registration_state.update(email, identity=identity_data, steps=steps, state="incomplete", error=detail)
        return {"steps": steps, "error": detail,
                "partial": steps.get("save_partial", {}).get("status") == "ok"}
    try:
        registration_state.update(email, identity=identity_data, steps=steps)
        return _register_account_steps(email, password, identity_data, on_step=on_step, resume=True)
    finally:
        scope.close()


def _register_account_steps(
    email: str, password: str, identity_data: dict[str, Any],
    *, on_step: Any = None, resume: bool = False,
) -> dict[str, Any]:
    """Cria Crowtado (com OTP) e Minute; etapas do site são manuais."""
    import logging
    import time

    from .. import crowtado, account_bans as bans, registration_proxy
    from ..minute_api import register as minute_register, login as minute_login

    logger = logging.getLogger("moneymin.register")
    # Do not overwrite the checkpoint or secret of an already connected account
    # when a new attempt supplies a different password.
    try:
        saved_password = credential_store.lookup(config.SECRETS_DIR, email, strict=True)
        if saved_password and saved_password != password:
            raise ValueError("A conta já possui outra senha protegida. Reconecte o acesso com a senha correta antes de registrar.")
    except ValueError as exc:
        return {"steps": {"save_partial": _step_fail(str(exc))}, "error": str(exc), "partial": False}
    previous = registration_state.load().get(email.strip().casefold(), {}) if resume else {}
    steps: dict[str, dict[str, str]] = dict(previous.get("steps", {}))
    step_start: dict[str, float] = {}

    def confirmed(name):
        return resume and steps.get(name, {}).get("status") in ("ok", "skip")

    def failure(exc):
        result = _step_fail(str(exc))
        code = getattr(exc, "account_issue_code", None)
        if isinstance(code, str):
            result["code"] = code
        return result

    def finish(result):
        # Public diagnostics never contain the supplied secret.
        result = dict(result)
        if isinstance(result.get("error"), str):
            result["error"] = registration_proxy.redact(result["error"], password)
        registration_state.update(email, identity=identity_data, steps=steps,
                                  state="incomplete" if result.get("error") else "complete",
                                  error=result.get("error"))
        result.setdefault("partial", bool(result.get("error") and steps.get("save_partial", {}).get("status") == "ok"))
        return result

    def _notify(step_name: str) -> None:
        registration_state.update(email, identity=identity_data, steps=steps)
        step_start[step_name] = time.monotonic()
        logger.info("[register] %s — iniciando etapa: %s", email, _STEP_LABELS.get(step_name, step_name))
        if callable(on_step):
            try:
                on_step(step_name)
            except Exception:
                pass

    def _record_step(step_name: str, result: dict[str, str]) -> None:
        result = dict(result)
        result["detail"] = registration_proxy.redact(result.get("detail", ""), password)
        steps[step_name] = result
        registration_state.update(email, identity=identity_data, steps=steps)
        elapsed = time.monotonic() - step_start.get(step_name, time.monotonic())
        logger.info("[register] %s — %s: %s (%.1fs)",
                     email, step_name, result["status"], elapsed)

    # Step 0: ban check
    _notify("ban_check")
    try:
        bans.require_not_banned(email)
        _record_step("ban_check", _step_ok())
    except ValueError as exc:
        _record_step("ban_check", _step_fail(str(exc)))
        return finish({"steps": steps, "error": f"ban: {exc}"})

    # O checkpoint local vem antes de qualquer operação remota. Sem uma senha
    # exportável e relida, nenhuma conta nova será iniciada.
    _notify("save_partial")
    try:
        _save_crowtado_cred(email, password)
    except Exception:
        detail = "Não foi possível confirmar o acesso no armazenamento local. Nenhuma criação remota foi iniciada; confira o armazenamento antes de tentar novamente."
        _record_step("save_partial", _step_fail(detail))
        return finish({"steps": steps, "error": detail, "partial": False})
    _record_step("save_partial", _step_ok("credenciais salvas"))

    # Step 1: Crowtado signup (Chrome + Turnstile + email OTP) — com retry
    _notify("crowtado_signup")
    crowtado_signup_ok = confirmed("crowtado_signup")
    crowtado_login_confirmed = False
    last_error: str = ""
    for attempt in range(1, _CROWTADO_SIGNUP_RETRIES + 1):
        if crowtado_signup_ok:
            break
        try:
            if identity_data.get("use_referral") is False:
                crowtado.criar_conta(email, password, ref="")
            else:
                crowtado.criar_conta(email, password)
            _record_step("crowtado_signup", _step_ok(
                "conta criada" if attempt == 1 else f"conta criada (tentativa {attempt})"
            ))
            crowtado_signup_ok = True
            break
        except Exception as exc:
            if getattr(exc, "account_issue_code", None) == "restricted":
                _record_step("crowtado_signup", failure(exc))
                return finish({"steps": steps, "error": f"crowtado: {exc}"})
            error_msg = str(exc)
            lower = error_msg.lower()
            # Somente duplicidade explícita permite retomar uma conta. Erros
            # como "executable doesn't exist" não indicam cadastro existente.
            if any(marker in lower for marker in (
                "form_identifier_exists", "email_exists", "account already exists",
                "email already exists", "email address already exists",
                "email is already taken", "email address is already taken",
                "email address is taken", "email already in use",
                "email address is already in use", "conta já existe",
                "e-mail já existe", "email já existe", "e-mail já está em uso",
                "email já está em uso", "endereço de e-mail já está em uso",
            )):
                try:
                    crowtado.login(email, password)
                    _record_step("crowtado_signup", _step_skip("conta já existia; login OK"))
                    crowtado_signup_ok = True
                    crowtado_login_confirmed = True
                    break
                except Exception as login_exc:
                    _record_step("crowtado_signup", failure(login_exc))
                    return finish({"steps": steps, "error": f"crowtado: {login_exc}"})
            last_error = error_msg
            if attempt < _CROWTADO_SIGNUP_RETRIES:
                logger.info("[register] %s — Crowtado falhou (tentativa %d/%d)",
                             email, attempt, _CROWTADO_SIGNUP_RETRIES)
                time.sleep(_CROWTADO_SIGNUP_RETRY_DELAY_S)
    if not crowtado_signup_ok:
        _record_step("crowtado_signup", _step_fail(f"{last_error} (após {_CROWTADO_SIGNUP_RETRIES} tentativas)"))
        return finish({"steps": steps, "error": f"crowtado: {last_error}"})

    # Creating a Clerk user is not proof of usable access. Reject explicit
    # bans/locks and keep network/auth uncertainty out of the success counter.
    _notify("ban_check")
    try:
        if not crowtado_login_confirmed:
            crowtado.login(email, password)
        _record_step("ban_check", _step_ok("Login Crowtado aceito; nenhum bloqueio de autenticação informado."))
    except Exception as exc:
        _record_step("ban_check", failure(exc))
        return finish({"steps": steps, "error": f"verificação Crowtado: {exc}"})

    # Existing completed site steps are preserved; unfinished ones belong to the user.
    for name, detail in (("demographics", "Preencher idade, gênero e equipamento manualmente no site Crowtado."),
                         ("link_minute", "Abrir Tarefas e vincular o Minute manualmente no site Crowtado.")):
        if steps.get(name, {}).get("status") != "ok":
            _record_step(name, {"status": "manual", "detail": detail})

    # Step 4: Minute register (com invite code da org)
    _notify("minute_register")
    try:
        existed = False
        try:
            # O código é explícito para que nenhuma alteração de default consiga
            # cadastrar uma conta Crowtado em organização diferente.
            if confirmed("minute_register"):
                minute_login(email, password)
                existed = True
            else:
                minute_register(email, password, config.INVITE_CODE)
        except RuntimeError as exc:
            # Apenas uma resposta explícita de e-mail duplicado permite recuperação.
            detail = str(exc).casefold()
            if not any(marker in detail for marker in (
                "email_exists", "email_already_exists", "email already exists",
                "email already in use", "email-already-in-use",
            )):
                raise
            minute_login(email, password)
            existed = True
        _validate_minute_membership(email)
        _record_step("minute_register", _step_skip("já existia; acesso e organização confirmados")
                     if existed else _step_ok("registro e organização confirmados"))
    except Exception as exc:
        _record_step("minute_register", failure(exc))
        return finish({"steps": steps, "error": f"minute: {exc}"})

    _notify("validate")
    try:
        # O tombstone só é removido quando todo o fluxo remoto foi confirmado.
        # Assim uma tentativa falha nunca ressuscita uma conta excluida.
        _set_account_removed(email, False)
    except (OSError, ValueError):
        detail = "conta criada e validada, mas a ativação local não pôde ser salva"
        _record_step("validate", _step_fail(detail))
        return finish({"steps": steps, "error": f"validação: {detail}", "partial": True})
    _record_step("validate", _step_ok("Crowtado e Minute criados; etapas do site pendentes de confirmação manual"))
    _save_account_check(email, {"email": email, "status": "active", "status_label": "Acesso verificado",
                               "org_key": config.ORG_KEY, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    logger.info("[register] %s — fluxo completo com sucesso", email)

    return finish({"steps": steps, "error": None})


def _tree_size(path: Path) -> tuple[int, int]:
    """Tamanho/arquivos sem seguir links; falhas pontuais não quebram o painel."""
    total = files = 0
    if not path.exists():
        return total, files
    pending = [path]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            # DirEntry reuses Windows enumeration metadata;
                            # Path.stat issues another filesystem query per file.
                            total += entry.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def _storage_snapshot(*, include_path: bool = True) -> dict[str, Any]:
    data_bytes, data_files = _tree_size(config.MEDIA_DATA_DIR)
    ego_bytes, ego_files = _tree_size(config.MEDIA_DATA_DIR / "ego4d")
    holo_bytes, holo_files = _tree_size(config.MEDIA_DATA_DIR / "holoassist")
    try:
        usage = shutil.disk_usage(config.LIBRARY_ROOT)
        free_bytes, total_bytes = usage.free, usage.total
    except OSError:
        free_bytes = total_bytes = 0
    result: dict[str, Any] = {
        "ready": (
            (config.MEDIA_DATA_DIR / "ego4d" / "timed_narrations.jsonl").exists()
            or (config.MEDIA_DATA_DIR / "ego4d" / "clip_narrations.json").exists()
            or (config.MEDIA_DATA_DIR / "holoassist").exists()
        ),
        "data_bytes": data_bytes,
        "data_files": data_files,
        "ego4d_bytes": ego_bytes,
        "ego4d_files": ego_files,
        "holoassist_bytes": holo_bytes,
        "holoassist_files": holo_files,
        "free_bytes": free_bytes,
        "disk_bytes": total_bytes,
    }
    if include_path:
        result.update({
            "root": str(config.LIBRARY_ROOT),
            "data_dir": str(config.MEDIA_DATA_DIR),
        })
    return result


# --- preferências ------------------------------------------------------------

def _finite_state_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def _invalid_local_state() -> None:
    raise JsonStateError("Estado local inválido ou ilegível. Preserve o arquivo e restaure um backup válido antes de continuar.") from None


def _validate_local_document(value: Any) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        _invalid_local_state()


def _validate_local_prefs(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid_local_state()
    if (("org_keys" in value and (not isinstance(value["org_keys"], dict)
            or any(not isinstance(email, str) or not isinstance(org, str)
                   for email, org in value["org_keys"].items())))
            or ("selected_accounts" in value and (not isinstance(value["selected_accounts"], list)
                or any(not isinstance(email, str) for email in value["selected_accounts"])))
            or ("holoassist_enabled" in value and type(value["holoassist_enabled"]) is not bool)):
        _invalid_local_state()
    return value


def _load_prefs() -> dict[str, Any]:
    return _validate_local_prefs(load_json_state(PREFS_PATH, {}))


def _save_prefs(prefs: dict[str, Any]) -> None:
    with _PERSISTENCE_LOCK:
        _validate_local_document(prefs)
        _validate_local_prefs(prefs)
        _load_prefs()
        save_json(PREFS_PATH, prefs)


def _local_dataset_provider(value: Any) -> str:
    provider = campaign.normalize_dataset_provider(value)
    if _load_prefs().get("holoassist_enabled") is False:
        if provider == "holoassist":
            raise ValueError("HoloAssist está desativado neste PC")
        if provider == "all":
            return "ego4d"
    return provider


def _cache_org_key(email: str, org_key: str) -> None:
    """Persiste somente uma organização já confirmada pela política da conta."""
    if org_key != org_policy.target_org_key(email):
        raise ValueError("Organização recusada pela política da conta.")
    with _PERSISTENCE_LOCK:
        prefs = _load_prefs()
        prefs.setdefault("org_keys", {})[email] = org_key
        _save_prefs(prefs)


def _removed_accounts_path() -> Path:
    """Registro local que impede a migração de ressuscitar contas removidas."""
    return config.DATA_DIR / "removed_accounts.json"


def _removed_accounts() -> set[str]:
    value = load_json_state(_removed_accounts_path(), {})
    emails = value.get("emails", [])
    if not isinstance(emails, list) or any(not isinstance(email, str) or not email.strip() for email in emails):
        _invalid_local_state()
    return {
        str(email).strip().casefold()
        for email in emails
        if str(email).strip()
    } | account_bans.banned_emails()


def _set_account_removed(email: str, removed: bool) -> None:
    normalized = email.strip().casefold()
    if not normalized:
        return
    with _PERSISTENCE_LOCK:
        emails = _removed_accounts()
        if removed:
            emails.add(normalized)
        else:
            emails.discard(normalized)
        save_json(_removed_accounts_path(), {
            "schema": 1,
            "emails": sorted(emails),
        })


# --- integrações protegidas -------------------------------------------------

def _legacy_aws_credentials() -> dict[str, str]:
    """Lê credenciais já configuradas sem expô-las para a resposta HTTP."""
    access = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    if access and secret:
        return {
            "access_key_id": access,
            "secret_access_key": secret,
            "session_token": os.environ.get("AWS_SESSION_TOKEN", "").strip(),
            "region": config.EGO4D_AWS_REGION or "",
        }
    profile = config.EGO4D_AWS_PROFILE or "default"
    configured = os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "").strip()
    candidates = [Path(configured)] if configured else [
        config.EGO4D_LOCAL_AWS_CREDENTIALS,
        Path.home() / ".aws" / "credentials",
    ]
    parser = configparser.RawConfigParser()
    for path in candidates:
        if not str(path) or not path.is_file():
            continue
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error):
            continue
        if not parser.has_section(profile):
            continue
        access = parser.get(profile, "aws_access_key_id", fallback="").strip()
        secret = parser.get(profile, "aws_secret_access_key", fallback="").strip()
        if access and secret:
            return {
                "access_key_id": access,
                "secret_access_key": secret,
                "session_token": parser.get(
                    profile, "aws_session_token", fallback="").strip(),
                "region": config.EGO4D_AWS_REGION or "",
            }
    return {}


def _migrate_legacy_integrations() -> dict[str, Any]:
    """Copia configurações existentes para o DPAPI, sem apagar os originais."""
    with _PERSISTENCE_LOCK:
        secure = load_secure_settings(config.INTEGRATIONS_PATH, strict=True)
        changed = False
        host = secure.get("hostinger")
        if not isinstance(host, dict) and config.HOSTINGER_MAIL_TOKEN:
            host = {
                "token": config.HOSTINGER_MAIL_TOKEN,
                "mailbox_id": config.HOSTINGER_MAILBOX_ID,
            }
            secure["hostinger"] = host
            changed = True
        if isinstance(host, dict) and not isinstance(host.get("profiles"), list):
            token = str(host.get("token") or "").strip()
            secure["hostinger"] = {
                "profiles": ([{
                    "id": uuid.uuid4().hex,
                    "name": "Caixa principal",
                    "token": token,
                    "mailbox_id": str(host.get("mailbox_id") or "").strip(),
                    "routes": [],
                }] if token else []),
            }
            host = secure["hostinger"]
            changed = True
        if isinstance(host, dict) and isinstance(host.get("profiles"), list):
            normalized_profiles = _hostinger_profiles(host)
            if normalized_profiles != host.get("profiles"):
                secure["hostinger"] = {"profiles": normalized_profiles}
                host = secure["hostinger"]
                changed = True
        if not isinstance(secure.get("ego4d"), dict):
            legacy = _legacy_aws_credentials()
            if legacy:
                secure["ego4d"] = legacy
                changed = True
        if changed:
            secure["schema"] = 2
            save_secure_settings(config.INTEGRATIONS_PATH, secure)
        if isinstance(secure.get("hostinger"), dict):
            _apply_hostinger(_hostinger_profiles(secure["hostinger"]))
        return secure


def _apply_ego4d(values: dict[str, str]) -> None:
    access = values.get("access_key_id", "").strip()
    secret = values.get("secret_access_key", "").strip()
    session = values.get("session_token", "").strip()
    region = values.get("region", "").strip()
    os.environ["AWS_ACCESS_KEY_ID"] = access
    os.environ["AWS_SECRET_ACCESS_KEY"] = secret
    if session:
        os.environ["AWS_SESSION_TOKEN"] = session
    else:
        os.environ.pop("AWS_SESSION_TOKEN", None)
    os.environ["EGO4D_AWS_PROFILE"] = ""
    if region:
        os.environ["EGO4D_AWS_REGION"] = region
    else:
        os.environ.pop("EGO4D_AWS_REGION", None)
    config.EGO4D_AWS_PROFILE = ""
    config.EGO4D_AWS_REGION = region


def _hostinger_routes(value: Any) -> list[str]:
    if isinstance(value, str):
        values = value.replace(";", ",").split(",")
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return sorted({
        str(item).strip().lower().lstrip("@")
        for item in values
        if str(item).strip()
    })


def _hostinger_profiles(host: dict[str, Any]) -> list[dict[str, Any]]:
    raw = host.get("profiles")
    if not isinstance(raw, list):
        raw = [host] if host.get("token") else []
    profiles: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        token = str(item.get("token") or "").strip()
        if not token:
            continue
        profiles.append({
            "id": str(item.get("id") or uuid.uuid4().hex).strip(),
            "name": str(item.get("name") or f"Caixa {index + 1}").strip(),
            "token": token,
            "mailbox_id": str(item.get("mailbox_id") or "").strip(),
            "routes": _hostinger_routes(item.get("routes")),
        })
    return profiles


def _apply_hostinger(profiles: list[dict[str, Any]]) -> None:
    config.HOSTINGER_MAIL_PROFILES = [dict(item) for item in profiles]
    primary = profiles[0] if profiles else {}
    token = str(primary.get("token") or "").strip()
    mailbox = str(primary.get("mailbox_id") or "").strip()
    os.environ["HOSTINGER_MAIL_TOKEN"] = token
    config.HOSTINGER_MAIL_TOKEN = token
    if mailbox:
        os.environ["HOSTINGER_MAILBOX_ID"] = mailbox
    else:
        os.environ.pop("HOSTINGER_MAILBOX_ID", None)
    config.HOSTINGER_MAILBOX_ID = mailbox


def _integration_error(exc: Exception, service: str) -> str:
    text = str(exc).lower()
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str((response.get("Error") or {}).get("Code") or "").lower()
    if any(term in code or term in text for term in (
            "expiredtoken", "requestexpired", "expired")):
        return "A credencial expirou. Renove o acesso e salve as novas chaves."
    if any(term in code or term in text for term in (
            "invalidaccesskeyid", "signaturedoesnotmatch", "invalid token")):
        return "A credencial informada não foi reconhecida. Confira os campos."
    if "accessdenied" in code or "access denied" in text or "forbidden" in text:
        return "A credencial existe, mas não possui acesso ao conteúdo solicitado."
    if any(term in text for term in (
            "timeout", "connection", "network", "dns", "name resolution")):
        return f"Não foi possível conectar à {service}. Confira a internet e tente novamente."
    return f"A {service} não aceitou a configuração informada."


def _test_ego4d(values: dict[str, str]) -> dict[str, Any]:
    import boto3
    client = boto3.client(
        "s3",
        region_name=values.get("region") or None,
        aws_access_key_id=values["access_key_id"],
        aws_secret_access_key=values["secret_access_key"],
        aws_session_token=values.get("session_token") or None,
    )
    response = client.get_object(
        Bucket=ego4d.MANIFEST_BUCKET,
        Key=ego4d.METADATA_KEY,
        Range="bytes=0-0",
    )
    body = response.get("Body")
    if body is not None:
        body.close()
    return {"ok": True, "message": "Acesso ao catálogo Ego4D confirmado."}


def _integration_snapshot() -> dict[str, Any]:
    secure = _migrate_legacy_integrations()
    ego = secure.get("ego4d") if isinstance(secure.get("ego4d"), dict) else {}
    host = (secure.get("hostinger")
            if isinstance(secure.get("hostinger"), dict) else {})
    host_profiles = _hostinger_profiles(host)
    if not ego:
        ego = _legacy_aws_credentials()
    ego_dir = config.MEDIA_DATA_DIR / "ego4d"
    holo_dir = config.MEDIA_DATA_DIR / "holoassist"
    seed_names = (
        "video.index.json.gz", "video_compress.index.json.gz", "imu.index.json.gz",
    )
    holo_indexes = all((holoassist._INDEX_SEED_DIR / name).is_file()
                       for name in seed_names)
    holo_catalog = (
        holoassist.annotations_path().is_file()
        and (holo_dir / "data-splits-v1_2.zip").is_file()
    )
    storage = _storage_snapshot(include_path=True)
    access = str(ego.get("access_key_id") or "")
    host_token = str(host_profiles[0].get("token") if host_profiles else "")
    host_mailbox = str(host_profiles[0].get("mailbox_id") if host_profiles else "")
    return {
        "security": {
            "provider": "Windows DPAPI",
            "detail": "Segredos criptografados para este usuário do Windows.",
        },
        "ego4d": {
            "configured": bool(access and ego.get("secret_access_key")),
            "access_hint": f"••••{access[-4:]}" if len(access) >= 4 else "",
            "region": str(ego.get("region") or config.EGO4D_AWS_REGION or "automática"),
            "catalog_ready": ((ego_dir / "ego4d.json").is_file()
                              and (ego_dir / "clips.csv").is_file()),
        },
        "hostinger": {
            "configured": bool(host_profiles),
            "connection_count": len(host_profiles),
            "token_hint": f"••••{host_token[-4:]}" if len(host_token) >= 4 else "",
            "mailbox_configured": bool(host_mailbox),
            "mailbox_hint": (
                f"••••{host_mailbox[-4:]}" if len(host_mailbox) >= 4 else ""
            ),
            "profiles": [{
                "id": item["id"],
                "name": item["name"],
                "token_hint": f"••••{item['token'][-4:]}",
                "mailbox_configured": bool(item["mailbox_id"]),
                "mailbox_hint": (
                    f"••••{item['mailbox_id'][-4:]}" if item["mailbox_id"] else ""
                ),
                "routes": item["routes"],
            } for item in host_profiles],
        },
        "holoassist": {
            "catalog_ready": holo_catalog,
            "indexes_ready": holo_indexes,
        },
        "runtime": {
            "ffmpeg_ready": readiness._binary_works(readiness.ffmpeg_bin()),
            "ffprobe_ready": readiness._binary_works(readiness.ffprobe_bin()),
            "browser_ready": readiness._private_browser_present(),
        },
        "library": storage,
    }


def _read_campaign_log(path: Path) -> dict[str, Any]:
    """Um arquivo danificado não pode derrubar a lista inteira do histórico."""
    data = load_json_state(path, None)
    if not isinstance(data, dict):
        raise ValueError("log inválido")
    for key in ("items", "accounts", "issues"):
        if not isinstance(data.get(key, []), list):
            raise ValueError("log inválido")
    for item in data.get("items", []):
        if (not isinstance(item, dict) or not isinstance(item.get("accounts", []), list)
                or any(not isinstance(account, dict) for account in item.get("accounts", []))):
            raise ValueError("log inválido")
        for account in item.get("accounts", []):
            if any(type(account[key]) is not bool for key in ("ok", "finalized", "skipped", "recovered")
                   if key in account and not (key == "finalized" and account[key] is None)):
                raise ValueError("confirmação inválida no histórico")
        try:
            duration = float(item.get("duration_ms") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("duração inválida no histórico") from exc
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("duração inválida no histórico")
    return data


def _campaign_log_view(data: dict[str, Any], *, history_name: str | None = None) -> dict[str, Any]:
    """Resumo e evidências permitidas; nunca inclui respostas brutas ou segredos."""
    configured = [str(email) for email in data.get("accounts", []) if email]
    by_account: dict[str, dict[str, Any]] = {
        email: {"email": email, "success": 0, "failed": 0, "skipped": 0, "pending": 0}
        for email in configured
    }
    items: list[dict[str, Any]] = []
    from ..campaign_evidence import current_groups, current_result
    groups = current_groups() if history_name is not None else {}
    current_summary = dict.fromkeys(("confirmed", "pending", "review", "unknown"), 0)
    delivery_summary = dict(current_summary)
    total_success = total_failed = total_skipped = 0
    total_pending = 0
    for index, raw_item in enumerate(data.get("items", []), 1):
        if not isinstance(raw_item, dict):
            continue
        results: list[dict[str, Any]] = []
        for raw_result in raw_item.get("accounts", []):
            if not isinstance(raw_result, dict):
                continue
            email = str(raw_result.get("email") or "Conta")
            stats = by_account.setdefault(
                email, {"email": email, "success": 0, "failed": 0, "skipped": 0, "pending": 0}
            )
            if raw_result.get("skipped"):
                status = "skipped"
                detail = (friendly_campaign_error(raw_result.get("error"))
                          if raw_result.get("error") else
                          "Registro local indica envio anterior." if raw_result.get("reason") == "already_sent" else
                          "Conta pulada nesta execução; o histórico não informa o motivo.")
                stats["skipped"] += 1
                total_skipped += 1
            elif raw_result.get("ok") and raw_result.get("finalized") is not True:
                status = "pending"
                detail = ("Envio sem confirmação de finalização. Confira a sessão antes de reenviar."
                          if raw_result.get("finalized") is False else
                          "Registro antigo sem evidência explícita de finalização. Confira a sessão antes de reenviar.")
                stats["pending"] += 1
                total_pending += 1
            elif raw_result.get("ok"):
                status = "success"
                detail = "Finalização confirmada pelo serviço e registrada nesta execução."
                stats["success"] += 1
                total_success += 1
            else:
                status = "failed"
                detail = friendly_campaign_error(raw_result.get("error"))
                stats["failed"] += 1
                total_failed += 1
            identifier = raw_result.get("session_id")
            current = current_result(raw_item, raw_result, history_name, groups)
            if status != "skipped":
                current_summary[current["status"]] += 1
                delivery_summary[current["status"]] += 1
            results.append({"email": email, "status": status, "detail": detail,
                            "session_id": identifier if isinstance(identifier, str) else None,
                            "confirmation": ("remote_ack" if status == "success" and raw_result.get("finalized") is True
                                             else "legacy_record" if status == "pending" and raw_result.get("finalized") is None
                                             else "not_confirmed"),
                            "recovered": raw_result.get("recovered") is True,
                            "current_result": current})
        task = str(raw_item.get("task_name") or raw_item.get("task_scenario")
                   or raw_item.get("scenario") or f"Vídeo {index}")
        duration_s = max(0, int(float(raw_item.get("duration_ms") or 0) / 1000))
        items.append({
            "index": index,
            "clip_uid": raw_item.get("clip_uid") if isinstance(raw_item.get("clip_uid"), str) else None,
            "task": task,
            "duration_s": duration_s,
            "success": sum(result["status"] == "success" for result in results),
            "failed": sum(result["status"] == "failed" for result in results),
            "skipped": sum(result["status"] == "skipped" for result in results),
            "pending": sum(result["status"] == "pending" for result in results),
            "accounts": results,
        })
    return {
        "schema": 2,
        "started_at": data.get("started_at"),
        "summary": {
            "configured_accounts": len(configured),
            "videos": len(items),
            "success": total_success,
            "failed": total_failed,
            "skipped": total_skipped,
            "pending": total_pending,
        },
        "accounts": sorted(by_account.values(), key=lambda item: item["email"].lower()),
        "items": items,
        "status": data.get("status"),
        "current_summary": current_summary,
        "delivery_summary": delivery_summary,
        "issues": [event for issue in data.get("issues", [])
                   if isinstance(issue, dict)
                   and (event := _public_event(str(issue.get("kind") or ""), issue))],
    }


# --- contas -------------------------------------------------------------------

def _load_account_health_history() -> dict[str, dict[str, Any]]:
    """Validate every consulted diagnostic before assigning it to an owner."""
    history = load_json_state(ACCOUNT_HEALTH_PATH, {})
    normalized = {}
    for owner, record in history.items():
        key = owner.strip().casefold()
        if not key or key in normalized or not isinstance(record, dict):
            _invalid_local_state()
        if "email" in record and (not isinstance(record["email"], str)
                                   or record["email"].strip().casefold() != key):
            _invalid_local_state()
        try:
            account_health.validate(record, key)
        except (ValueError, TypeError, RecursionError):
            _invalid_local_state()
        for field in ("status", "status_label", "checked_at"):
            if field in record and not isinstance(record[field], str):
                _invalid_local_state()
        if "attempts" in record and (type(record["attempts"]) is not int or record["attempts"] < 0):
            _invalid_local_state()
        if "last_success_at" in record and record["last_success_at"] is not None and not isinstance(record["last_success_at"], str):
            _invalid_local_state()
        for field in ("history_saved", "permanently_removed"):
            if field in record and type(record[field]) is not bool:
                _invalid_local_state()
        if "issue" in record:
            issue = record["issue"]
            if not isinstance(issue, dict):
                _invalid_local_state()
            if "email" in issue and (not isinstance(issue["email"], str)
                                      or issue["email"].strip().casefold() != key):
                _invalid_local_state()
            for field in ("restriction_confirmed", "retryable"):
                if field in issue and type(issue[field]) is not bool:
                    _invalid_local_state()
            for field in ("code", "reason", "action", "stage"):
                if field in issue and not isinstance(issue[field], str):
                    _invalid_local_state()
        normalized[key] = record
    return normalized


def _list_accounts() -> list[dict[str, Any]]:
    """Contas = token_*.json em secrets/ (sem rede). org_key vem do cache de prefs."""
    prefs = _load_prefs()
    health = _load_account_health_history()
    org_keys = prefs.get("org_keys", {})
    if not isinstance(org_keys, dict):
        org_keys = {}
    removed = _removed_accounts()
    registrations = registration_state.load()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key, (path, data) in token_store.records(config.tokens_dir()).items():
        email = data["email"]
        if str(email).strip().casefold() in removed:
            continue
        key = str(email).strip().casefold()
        if key in registrations and registrations[key]["state"] != "complete":
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "email": email,
            "expires_at": data.get("expires_at", 0),
            "org_key": org_keys.get(email),
            "account_kind": org_policy.account_kind(email),
            "last_check": health.get(key, {}),
            "org_name": {
                config.ORG_KEY: "Datoric", config.CLARU_ORG_KEY: "Claru",
                config.HUB_ORG_KEY: "Hub antigo",
            }.get(org_keys.get(email), "Não verificada"),
        })
    return out


def _resolve_org(email: str, session: Session | None = None) -> str:
    """Resolve (e cacheia) a org_key pela política da conta, não pela 1ª org."""
    # Validar sempre, inclusive quando a organização já está no cache. O HUB
    # devolve /users/me = 200 para contas desativadas; ensure_auth inspeciona o
    # campo `disabled` e impede que a campanha comece com uma conta bloqueada.
    sess = session or Session.from_email(email)
    profile = sess.ensure_auth()
    orgs = [org for org in (profile.get("organizations") or []) if isinstance(org, dict)]
    org_key = org_policy.ensure_membership(sess, email, orgs)
    _cache_org_key(email, org_key)
    return org_key


def _check_minute_health(email: str) -> dict[str, Any]:
    """Check the target organization and quality state, not just authentication."""
    session = None
    for attempt in range(1, 3):
        try:
            session = session or Session.from_email(email)
            profile = session.ensure_auth()
            if type(profile.get("disabled")) is not bool:
                raise AuthError("Estado da conta Minute incompleto", code="invalid_response")
            organizations = [
                org for org in (profile.get("organizations") or [])
                if isinstance(org, dict)
            ]
            target = next((org for org in organizations if org.get("resourceKey") == org_policy.target_org_key(email)), {})
            if "disabled" in target and type(target["disabled"]) is not bool:
                raise AuthError("Estado da organização Minute incompleto", code="invalid_response")
            try:
                org_key = org_policy.ensure_membership(session, email, organizations)
            except RuntimeError as exc:
                if "suspensa" in str(exc).casefold():
                    raise AuthError(str(exc), code="restricted") from exc
                raise AuthError(str(exc), code="organization") from exc
            quality = session.checked_quality_state(org_key)
            if not isinstance(quality, dict) or quality.get("userState") not in ("active", "on_hold", "inactive"):
                raise AuthError("Estado de qualidade Minute incompleto", code="invalid_response")
            if quality["userState"] in ("on_hold", "inactive"):
                raise AuthError("A organização alvo restringiu o acesso Minute", code="restricted")
            result = {
                "email": email, "status": "active", "status_label": "Acesso verificado",
                "org_key": org_key, "expires_at": session.data.get("expires_at", 0),
            }
            break
        except Exception as exc:  # noqa: BLE001 — qualquer falha ambígua é inconclusiva
            issue = account_issue(email, exc, stage="Verificação Minute")
            issue["provider"] = "minute"
            if issue["retryable"] and attempt < 2:
                time.sleep(2.0 if issue["code"] == "rate_limit" else 0.5)
                continue
            status, label = {
                "restricted": ("disabled", "Banida · Minute"),
                "authentication": ("needs_reauth", "Reconectar acesso"),
                "missing_access": ("needs_reauth", "Reconectar acesso"),
                "organization": ("needs_org", "Organização pendente"),
            }.get(issue["code"], ("inconclusive", "Verificação inconclusiva"))
            result = {"email": email, "status": status, "status_label": label,
                      "error": issue_text(issue), "issue": issue}
            break
    result["attempts"] = attempt
    result["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return result


def _check_crowtado_health(email: str) -> dict[str, Any]:
    if org_policy.account_kind(email) == "claru":
        return {"email": email, "status": "not_applicable", "status_label": "Não se aplica",
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "attempts": 0}
    for attempt in range(1, 3):
        try:
            password = _saved_account_password(email)
            if not password:
                raise crowtado.CrowtadoError("Sem senha Crowtado salva", code="missing_access")
            state = crowtado.verificar_restricoes(email, password)
            if state["restricted"]:
                issue = account_issue(email, crowtado.CrowtadoError("Restrição", code="restricted"),
                                      stage="Verificação Crowtado · saques")
                issue.update(provider="crowtado", reason=state["reason"],
                             action="Confira o painel e solicite regularização ao suporte da Crowtado. A conta permanece disponível para consulta.")
                result = {"email": email, "status": "disabled", "status_label": "Banida · saque suspenso",
                          "restriction_kind": state["restriction_kind"], "access_status": "active",
                          "issue": issue, "error": issue_text(issue)}
            else:
                result = {"email": email, "status": "active", "status_label": "Sem restrição informada",
                          "access_status": "active", "payout_available": state["payout_available"]}
            break
        except Exception as exc:  # noqa: BLE001 — missing evidence never confirms clearance
            issue = account_issue(email, exc, stage="Verificação Crowtado")
            issue["provider"] = "crowtado"
            if issue["retryable"] and attempt < 2:
                time.sleep(2.0 if issue["code"] == "rate_limit" else 0.5)
                continue
            status, label = {"restricted": ("disabled", "Banida · login recusado"),
                             "authentication": ("needs_reauth", "Reconectar Crowtado"),
                             "missing_access": ("needs_reauth", "Conectar Crowtado"),
                             "crowtado_account_missing": ("needs_reauth", "Cadastro não encontrado")}.get(
                                 issue["code"], ("inconclusive", "Verificação inconclusiva"))
            result = {"email": email, "status": status, "status_label": label,
                      "error": issue_text(issue), "issue": issue}
            if status == "disabled":
                result["restriction_kind"] = "account"
                result["access_status"] = "disabled"
            elif getattr(exc, "crowtado_login_verified", False) is True:
                result["access_status"] = "active"
                result["status_label"] = "Login aceito · saque não verificado"
            break
    result.update(attempts=attempt, checked_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    return result


def _check_account_health(email: str) -> dict[str, Any]:
    """Always consult both services independently using the identity's assigned proxy."""
    from .. import registration_proxy
    providers = {}
    try:
        with registration_proxy.route(registration_proxy.assign(email, "")):
            providers["minute"] = _check_minute_health(email)
            providers["crowtado"] = _check_crowtado_health(email)
    except Exception as exc:  # local routing failure must not fall back to another IP
        for name in account_health.SERVICES:
            if name not in providers:
                issue = account_issue(email, exc, stage=f"Verificação {name.title()} · conexão")
                issue["provider"] = name
                providers[name] = {"email": email, "status": "inconclusive", "status_label": "Verificação inconclusiva",
                                   "error": issue_text(issue), "issue": issue, "attempts": 0,
                                   "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    result = account_health.aggregate(email, providers)
    _save_account_check(email, result)
    return result


def _save_account_check(email: str, result: dict[str, Any]) -> None:
    with _PERSISTENCE_LOCK:
        try:
            health = _load_account_health_history()
            previous = health.get(email.strip().casefold(), {})
            candidate = dict(result)
            # Copy nested diagnoses before adding provider-specific history.
            candidate = decode_json_state(json.dumps(candidate))
            account_health.preserve_history(candidate, previous if isinstance(previous, dict) else {})
            candidate = decode_json_state(json.dumps(candidate))
            account_health.validate(candidate, email.strip().casefold())
            health[email.strip().casefold()] = candidate
            save_json(ACCOUNT_HEALTH_PATH, health)
            result["last_success_at"] = candidate["last_success_at"]
            if "providers" in candidate:
                result["providers"] = candidate["providers"]
            # Checking one service must not erase access to the other service.
            minute = account_health.provider(result, "minute")
            if minute.get("status") == "active" and result.get("org_key"):
                prefs = _load_prefs()
                prefs.setdefault("org_keys", {})[email] = result["org_key"]
                _save_prefs(prefs)
        except (OSError, TypeError, ValueError, RecursionError):
            # Falha no histórico local não altera o diagnóstico remoto.
            result["history_saved"] = False


def _migrate_account_org(email: str) -> dict[str, Any]:
    kind = org_policy.account_kind(email)
    row = {"email": email, "account_kind": kind}
    if kind == "claru":
        return {**row, "status": "skipped", "message": "Claru: mantida sem aplicar código."}
    session = Session.from_email(email)
    profile = session.ensure_auth()
    before = org_policy.pick_org_key(email, profile.get("organizations") or [])
    target = org_policy.ensure_membership(session, email, profile.get("organizations") or [])
    _cache_org_key(email, target)
    return {
        **row, "status": "already" if before else "migrated", "org_key": target,
        "message": ("Já estava na organização nova." if before else "Organização atualizada.")
                   + f" Crowtado · {config.INVITE_CODE}",
    }


# --- saldos (crowtado) ----------------------------------------------------------

def _load_balances() -> dict[str, Any]:
    """Cache de saldos: {email: {availableCents, ..., updated_at, error?}}."""
    value = load_json_state(BALANCES_PATH, {})
    if any(not isinstance(email, str) or not isinstance(record, dict) for email, record in value.items()):
        _invalid_local_state()
    return value


def _save_balances(balances: dict[str, Any]) -> None:
    with _PERSISTENCE_LOCK:
        _validate_local_document(balances)
        if not isinstance(balances, dict) or any(not isinstance(email, str) or not isinstance(record, dict)
                                                for email, record in balances.items()):
            _invalid_local_state()
        _load_balances()
        save_json(BALANCES_PATH, balances)


def _crowtado_creds(*, include_individual: bool = True) -> dict[str, str]:
    """Reutiliza credenciais da mesma identidade, inclusive lotes de criação."""
    creds: dict[str, str] = {}
    def collect(rec):
        if not isinstance(rec, dict):
            return
        email = rec.get("email")
        password = rec.get("password") or rec.get("senha")
        if isinstance(email, str) and isinstance(password, str) and password:
            creds[email.strip().casefold()] = password

    for path in sorted(config.DATA_DIR.glob("novas_contas_*.json")):
        rows = load_json_state(path, [], expected_type=list)
        if isinstance(rows, list):
            for rec in rows:
                collect(rec)
    contas = config.DATA_DIR / "contas.jsonl"
    try:
        # Complete strict preflight before any source can be promoted. An
        # unreadable tail leaves the latest known password uncertain.
        for rec in decode_jsonl_history(contas.read_bytes(), expected_type=dict):
            collect(rec)
    except FileNotFoundError:
        pass
    except OSError:
        _invalid_local_state()
    stored = load_json_state(CROWTADO_PW_PATH, {})
    if isinstance(stored, dict):
        creds.update({str(email).strip().casefold(): password
                      for email, password in stored.items()
                      if isinstance(password, str) and password})
    # Registros individuais são a fonte primária para credenciais novas. Eles
    # não competem entre contas durante uma gravação e vencem dados legados.
    if include_individual:
        creds.update(credential_store.load_all(config.SECRETS_DIR))
    return creds


def _saved_account_password(email: str, legacy: dict[str, str] | None = None) -> str | None:
    """Consulta legado somente se faltar o registro individual válido.

    Callers com um mapa legado pronto mantêm o contrato anterior. Um registro
    primário corrompido veta o fallback; nunca consulta o merge por esse erro.
    """
    try:
        individual = credential_store.lookup(config.SECRETS_DIR, email, strict=True)
    except ValueError:
        return None
    if individual is not None:
        return individual
    sources = _crowtado_creds() if legacy is None else legacy
    return sources.get(email.strip().casefold())


def _configured_crowtado_creds() -> dict[str, str]:
    """Resolve somente acessos Crowtado ativos e promove cópias legadas.

    Contas Claru continuam cadastradas no QMoney, mas não possuem saldo
    Crowtado. Credenciais legadas válidas são copiadas para o registro
    individual atômico sem deixar uma falha de migração interromper a consulta
    que ainda pode usar a cópia antiga.
    """
    saved = {str(e).strip().casefold(): p for e, p in _crowtado_creds().items()}
    configured: dict[str, str] = {}
    for account in _list_accounts():
        email = str(account["email"])
        if org_policy.account_kind(email) != "crowtado":
            continue
        key = email.strip().casefold()
        try:
            individual = credential_store.lookup(config.SECRETS_DIR, key, strict=True)
        except ValueError:
            # Falha fechada: não autentica saldos/saques com fallback antigo
            # quando a fonte primaria existe, mas perdeu integridade.
            continue
        password = individual or saved.get(key)
        if password:
            configured[email] = password
            if individual is None:
                try:
                    with _PERSISTENCE_LOCK:
                        credential_store.save(config.SECRETS_DIR, key, password)
                except (OSError, ValueError):
                    # A cópia legada permanece utilizável. A promoção será
                    # tentada novamente sem transformar a conta em desconectada.
                    pass
    return configured


def _save_crowtado_cred(email: str, password: str) -> None:
    with _PERSISTENCE_LOCK:
        # A fonte individual protegida é relida antes de continuar. Arquivos
        # legados continuam sendo apenas fontes de leitura/migração; criar um
        # espelho em JSON anularia a proteção da senha por DPAPI.
        credential_store.save(config.SECRETS_DIR, email, password)


def _remove_account_data(email: str) -> None:
    """Remove caches editáveis ligados à conta (o cadastro histórico fica intacto)."""
    with _PERSISTENCE_LOCK:
        # Valide o espelho consultado antes de remover a fonte individual ou
        # regravar outras contas. Corrupção/ambiguidade não significa ausência.
        stored = load_json_state(CROWTADO_PW_PATH, {})
        creds = stored if isinstance(stored, dict) else {}
        matching_creds = [
            key for key in creds
            if str(key).casefold() == email.casefold()
        ]
        credential_store.delete(config.SECRETS_DIR, email)
        for key in matching_creds:
            creds.pop(key, None)
        if matching_creds:
            save_json(CROWTADO_PW_PATH, creds)

        balances = _load_balances()
        matching_balances = [
            key for key in balances
            if str(key).casefold() == email.casefold()
        ]
        for key in matching_balances:
            balances.pop(key, None)
        if matching_balances:
            _save_balances(balances)
    crowtado.clear_cached_session(email)


def _ban_accounts(issues: list[dict]) -> None:
    """Registra primeiro a restrição, depois remove o acesso local permanentemente."""
    if not issues or any(i.get("restriction_confirmed") is not True for i in issues):
        raise ValueError("A remoção exige restrição confirmada pela plataforma.")
    with _PERSISTENCE_LOCK:
        path = config.DATA_DIR / "banned_accounts.json"
        archive = banned_store.load(path, {"schema": 1, "accounts": []})
        if (not isinstance(archive, dict) or not isinstance(archive.get("accounts"), list)
                or any(not isinstance(row, dict) or not isinstance(row.get("email"), str)
                       for row in archive.get("accounts", []))):
            raise ValueError("Registro de contas banidas inválido; remoção cancelada.")
        records = {row["email"].casefold(): row for row in archive["accounts"]}
        passwords = _crowtado_creds()
        for issue in issues:
            email = account_transfer.email_key(issue.get("email"))
            previous = records.get(email, {})
            banned_at = previous.get("banned_at") or previous.get("removed_at") or time.strftime("%Y-%m-%dT%H:%M:%S%z")
            records[email] = {"email": email, "password": passwords.get(email) or previous.get("password"),
                              "banned_at": banned_at, "removed_at": previous.get("removed_at") or banned_at,
                              "reason": issue.get("reason", "Restrição confirmada pela plataforma."),
                              "stage": issue.get("stage", "Envio"), "restriction_confirmed": True}
        archive["accounts"] = list(records.values())
        banned_store.save(path, archive)
        for issue in issues:
            email = account_transfer.email_key(issue["email"])
            _set_account_removed(email, True)
            token_store.delete(config.SECRETS_DIR, email)
            prefs = _load_prefs()
            prefs["selected_accounts"] = [e for e in prefs.get("selected_accounts", []) if e.casefold() != email]
            prefs["org_keys"] = {e: key for e, key in prefs.get("org_keys", {}).items() if e.casefold() != email}
            _save_prefs(prefs)
            _remove_account_data(email)
        account_bans.purge_local_records({account_transfer.email_key(i["email"]) for i in issues})


def _preflight_fingerprint(emails: list[str], *, _include_cached_org: bool = True) -> str:
    """Vincula a prévia à identidade/configuração, não à rotação da sessão."""
    digest = hashlib.sha256()
    for email in sorted(set(emails)):
        path = config.token_path(email)
        digest.update(str(path).encode())
        found = token_store.load(config.SECRETS_DIR, email, migrate=False)
        if found is None:
            digest.update(b"missing")
            continue
        _, token = found
        stable = {"email": token_store.email_key(token["email"]),
                  "subject": token.get("localId") or token.get("user_id") or token.get("uid"),
                  "organization": {key: token[key] for key in
                      (("org_key", "organization_id", "tenantId") if _include_cached_org else
                       ("organization_id", "tenantId")) if key in token}}
        # Remover/revogar as credenciais deve invalidar a prévia; renová-las não.
        identity = {"record": stable,
                    "has_id_token": bool(token.get("idToken") or token.get("id_token")),
                    "has_refresh_token": bool(token.get("refreshToken") or token.get("refresh_token"))}
        digest.update(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode())
    removed = _removed_accounts()
    digest.update(json.dumps(sorted(email.strip().casefold() for email in emails
                                    if email.strip().casefold() in removed)).encode())
    return digest.hexdigest()


def _catalog_identity_fingerprint(emails: list[str]) -> str:
    # Resolving an organization fills org_key during this very job. That cache
    # write is not an account change and must not discard its completed result.
    return _preflight_fingerprint(emails, _include_cached_org=False)


def _invalidate_balance_after_withdrawal(email: str, result: dict[str, Any]) -> None:
    """Keep the previous reading as historical, never infer the remaining balance."""
    with _PERSISTENCE_LOCK:
        balances = _load_balances()
        previous = balances.get(email)
        rec = dict(previous) if isinstance(previous, dict) else {}
        receipt = {
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "status": result.get("status"),
            "previousAvailableCents": rec.get("availableCents"),
            "previousUpdatedAt": rec.get("updated_at"),
        }
        amount = result.get("amountCents")
        if type(amount) is int and amount >= 0:
            receipt["amountCents"] = amount
        if result.get("currency") in {"USD", "BRL", "EUR", "GBP"}:
            receipt["currency"] = result["currency"]
        rec.update(stale=True, lastWithdrawal=receipt,
                   error="Saldo anterior à tentativa de saque. Atualize para consultar o valor atual.")
        if result.get("status") in {"on_hold", "hold"} and isinstance(result.get("holdReason"), str):
            rec["holdReason"] = result["holdReason"][:500]
            rec["error"] = "A Crowtado informou uma retenção de saque: " + rec["holdReason"]
        balances[email] = rec
        _save_balances(balances)


def _on_balance_result(email: str, summary: dict | None, erro: str | Exception | None) -> None:
    with _PERSISTENCE_LOCK:
        balances = _load_balances()
        previous = balances.get(email)
        rec = dict(previous) if isinstance(previous, dict) else {}
        if summary is not None:
            try:
                summary = crowtado._summary_from_payload(summary)
                if not summary:
                    raise crowtado.CrowtadoError("Saldo incompleto", code="invalid_response")
            except crowtado.CrowtadoError as exc:
                summary, erro = None, exc
        rec["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if summary is not None:
            for field in crowtado.BALANCE_SUMMARY_FIELDS:
                rec.pop(field, None)
            rec.update(summary)
            rec["updated_at"] = rec["checked_at"]
            rec["error"] = None
            rec["issue"] = None
            rec["stale"] = False
        else:
            error = erro if isinstance(erro, Exception) else RuntimeError(erro or "Consulta inconclusiva")
            issue = account_issue(email, error, stage="Consulta de saldo Crowtado")
            rec["error"] = issue_text(issue)
            rec["issue"] = issue
            rec["stale"] = True
        balances[email] = rec
        _save_balances(balances)


def _last_withdrawal_receipt(configured: set[str]) -> dict[str, Any]:
    """Expose recorded provider acceptance separately from Wise cleanup.

    Legacy Wise diagnostics are sufficient evidence of acceptance, but never of
    settlement in the recipient's bank. Do not infer success from balance loss.
    """
    record = load_json_state(config.DATA_DIR / "withdraw_last_result.json", {})
    if not isinstance(record, dict) or record.get("email") not in configured:
        return {}
    result = record.get("result")
    if not isinstance(result, dict):
        return {}
    status = result.get("status")
    if not isinstance(status, str):
        return {}
    accepted = status == "ok"
    email = record["email"]
    when = record.get("finished_at")
    if type(when) not in (float, int) or not math.isfinite(when):
        return {}
    try:
        stamp = datetime.datetime.fromtimestamp(when, datetime.timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return {}
    pending = _wise_cleanup_snapshot()
    current_pending = bool(pending.get("pending") and
                           (pending.get("email") == email or pending.get("error")))
    if accepted:
        message = "Saque Wise aceito pela Crowtado. Não repita esta solicitação."
    elif status == "review_required":
        message = "Solicitação Wise enviada para revisão da Crowtado."
    else:
        message = _withdraw_message(email, result)
    if accepted or status == "review_required":
        if current_pending:
            message += " A limpeza Wise está pendente; novos saques permanecem bloqueados."
        elif result.get("wiseDestinationRemoved") is True and result.get("payoutPreferenceRestored") is True:
            message += " Wise desvinculada e Dots confirmado."
        elif result.get("cleanupPending") is True:
            message += " Houve pendência de limpeza; atualmente não há bloqueio de limpeza registrado."
        message += " O recebimento na Wise não foi verificado pelo QMoney."
    return {"email": email, "accepted": accepted, "status": status,
            "finished_at": stamp, "cleanup_pending": current_pending, "message": message}


def _withdraw_message(email: str, result: dict[str, Any]) -> str:
    """Mensagem curta e segura para o resultado do payouts.withdraw."""
    status = str(result.get("status") or "")
    if result.get("cleanupPending") is True:
        outcome = ("saque aceito" if status == "ok" else
                   "saque enviado para revisão" if status == "review_required" else
                   "resultado do saque: " + (status or "desconhecido"))
        return (f"{email}: {outcome}. LIMPEZA WISE PENDENTE: o fluxo não está concluído. "
                "Novos saques estão bloqueados. Use Concluir limpeza Wise; isso não repete o saque.")
    if status == "ok":
        if result.get("wiseDestinationRemoved") is False:
            restored = result.get("payoutPreferenceRestored") is True
            return (f"saque solicitado com sucesso para {email}; a desvinculação da Wise não foi confirmada. "
                    + ("Dots está como padrão. " if restored else "A volta para Dots também não foi confirmada. ")
                    + "Confira a conta na Crowtado sem repetir o saque; o lote não continuará.")
        if result.get("payoutPreferenceRestored") is False:
            return (f"saque solicitado com sucesso para {email}; não foi possível confirmar "
                    "a volta para Dots. Configure Dots novamente, sem repetir o saque.")
        if result.get("payoutPreferenceRestored") is True:
            return f"saque solicitado com sucesso para {email}; Wise desvinculada e método padrão restaurado para Dots"
        if result.get("dotsEmailDelivery") == "sent":
            return f"link de saque enviado por email para {email}"
        if result.get("dotsSmsDelivery") == "sent":
            return f"link de saque enviado por SMS para a conta {email}"
        if result.get("dotsEmailDelivery") == "already_settled":
            return f"saque solicitado para {email}; cadastro Dots já estava concluído"
        return f"solicitação de saque enviada para {email}"
    if status == "review_required":
        return f"saque de {email} enviado para revisão do Crowtado"
    if status in {"on_hold", "hold"}:
        reason = result.get("holdReason")
        return "Os saques estão retidos: " + (reason[:500] if isinstance(reason, str) and reason else "Confira a restrição no painel da Crowtado.")
    if status == "eligibility_unconfirmed":
        return "Saque não enviado: não foi possível confirmar a elegibilidade no Minute pela Crowtado. Atualize a consulta e tente novamente."
    if status.endswith("_pending_retry"):
        return (f"A Crowtado registrou uma tentativa pendente para {email} e fará nova tentativa. "
                "Não solicite outro saque; acompanhe o histórico da plataforma.")
    if status == "not_requested":
        method = "PayPal" if result.get("method") == "paypal" or result.get("failureStage") == "configure_paypal" else "Wise"
        reasons = {
            "destination_in_use": "o destino está vinculado a outra conta",
            "payout_pending": "há pagamento pendente impedindo a alteração",
            "payout_configuration": "a Crowtado ainda não confirmou a configuração",
        }
        reason = reasons.get(result.get("failureCode"), "a configuração não foi concluída")
        return f"Saque não solicitado por {method}: {reason}."
    messages = {
        "in_transit": "há um pagamento em trânsito que a Crowtado não permite substituir; aguarde a conclusão antes de solicitar outro saque",
        "not_requested": "saque não solicitado: a configuração da Wise não foi concluída",
        "unknown": "resultado do saque inconclusivo; confira o histórico na Crowtado antes de repetir",
        "below_minimum": "saldo abaixo do mínimo para saque",
        "hold": "saques estão temporariamente bloqueados para esta conta",
        "dots_not_ready": "o método Dots ainda não está disponível para esta conta",
        "manual_not_ready": "a Crowtado não confirmou o destino manual; configure Wise antes de solicitar",
        "tremendous_not_ready": "o provedor de pagamento da Crowtado não está disponível para esta conta",
        "stripe_global_not_ready": "o provedor de pagamento ainda não está pronto",
        "tremendous_failed": "o provedor de pagamento recusou a solicitação; confira o histórico na Crowtado",
        "manual_failed": "o pagamento manual falhou; confira o histórico na Crowtado antes de repetir",
        "stripe_global_failed": "o provedor de pagamento recusou a solicitação; confira o histórico na Crowtado",
        "no_balance": "não há saldo disponível para saque",
        "dots_failed": "o Dots recusou a solicitação de saque",
        "dots_pending_retry": "o Crowtado ainda tentará enviar o link novamente",
    }
    return messages.get(
        status, "Resposta de saque não reconhecida. Confira o histórico na Crowtado antes de repetir.")


# --- app -----------------------------------------------------------------------

def _validate_campaign_request(body: Any) -> None:
    """Use the same parameter checks for preview and execution."""
    if not isinstance(body, dict):
        raise ValueError("a campanha deve ser um objeto JSON")
    emails = body.get("accounts", [])
    if (not isinstance(emails, list) or any(not isinstance(e, str) or not e.strip() for e in emails)
            or len({e.strip() for e in emails}) != len(emails)):
        raise ValueError("selecione uma lista de contas válidas, sem duplicação")
    tasks = body.get("tasks", [])
    if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks):
        raise ValueError("selecione uma lista de categorias válidas")
    for name in ("target_hours", "delay_s", "account_gap_s"):
        value = body.get(name, 0)
        if isinstance(value, bool) or not math.isfinite(float(value or 0)):
            raise ValueError("parâmetro numérico inválido: " + name)
    if isinstance(body.get("count", 1), bool):
        raise ValueError("quantidade de vídeos inválida")
    if body.get("delay_mode", "off") not in {"off", "clip", "fixed"}:
        raise ValueError("delay_mode inválido (off|clip|fixed)")
    if not isinstance(body.get("cleanup_after_upload", True), bool):
        raise ValueError("cleanup_after_upload deve ser true ou false")
    if _parse_active_hours(body.get("active_hours")) is False:
        raise ValueError("active_hours inválido — use [início, fim] com 0 <= início < fim <= 24")


def _parse_duration_range(values) -> tuple[float, float]:
    """Valida o intervalo de duração solicitado pela interface."""
    bounds = []
    for name, default in (("min_dur_s", 60), ("max_dur_s", MAX_DUR_S)):
        try:
            value = float(values.get(name, default))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} inválido") from exc
        if not math.isfinite(value) or not 60 <= value <= MAX_DUR_S:
            raise ValueError(f"{name} deve estar entre 60 e 1800 segundos")
        bounds.append(value)
    minimum, maximum = bounds
    if minimum > maximum:
        raise ValueError("min_dur_s não pode exceder max_dur_s")
    return minimum, maximum


def _require_local_api_token(*, for_testing: bool = False) -> str:
    """Require a desktop session; unauthenticated clients are explicit test fixtures."""
    if type(for_testing) is not bool:
        raise TypeError("for_testing deve ser booleano.")
    local_api_token = os.environ.get("QMONEY_LOCAL_API_TOKEN", "")
    if not local_api_token and (not for_testing or getattr(sys, "frozen", False)):
        raise RuntimeError("Inicie o serviço pela interface QMoney para proteger o acesso local.")
    return local_api_token


def create_app(*, for_testing: bool = False) -> Flask:
    import hmac
    local_api_token = _require_local_api_token(for_testing=for_testing)
    if not for_testing:
        _restore_registration_batch()
    preflights: dict[str, dict] = {}
    preflight_lock = threading.RLock()
    # Original captures use their own bounded, server-owned receipts. They
    # cannot fall back to a dataset plan or an unreviewed start.
    original_preflights: dict[str, dict] = {}
    original_starts: dict[str, dict] = {}
    # Per-service close barrier: a request admitted before closing may still
    # finish its preflight, but must never start another sending worker.
    campaign_drain = {"requested": False, "requests": 0}
    banned_monitor = BannedMonitor()
    RUNNER.on_restriction = lambda email: _ban_accounts([{
        "email": email, "restriction_confirmed": True, "stage": "Envio da campanha",
        "reason": "Restrição confirmada pela plataforma durante o envio.",
    }])
    # No QMoney o Flask e apenas o servico local consumido pela interface Qt.
    # Nenhum frontend web e publicado ou usado como fallback.
    app = Flask(__name__, static_folder=None)
    app.config["QMONEY_LOCAL_API_AUTHENTICATED"] = bool(local_api_token)
    task_catalog = CatalogLoader()
    campaign_verifications = CatalogLoader(ttl_s=30, max_pending=1, timeout_s=900,
        timeout_message="A verificação demorou mais que o esperado. Nenhum envio foi iniciado. "
                        "Confira a conexão e tente verificar novamente em instantes.")
    accelerator_catalog = CatalogLoader()
    recovery_catalog = CatalogLoader(ttl_s=2)
    original_library_index = CatalogLoader(max_pending=1, timeout_s=180)
    prepared_library_inventory = CatalogLoader(ttl_s=300, max_pending=1, timeout_s=7200,
        timeout_message="A conferência dos arquivos locais demorou demais. Tente verificar o acervo novamente.")
    local_media_inventory = CatalogLoader(ttl_s=3, max_pending=1, timeout_s=300)
    nymeria_catalog = CatalogLoader(ttl_s=3, max_pending=1, timeout_s=900)
    nymeria_library_worker = LibraryPreparationRunner()
    app.extensions["nymeria_library_worker"] = nymeria_library_worker

    @app.before_request
    def authenticate_local_client():
        if local_api_token and not hmac.compare_digest(
                request.headers.get("X-QMoney-Session", "").encode("utf-8"),
                local_api_token.encode("utf-8")):
            return jsonify({"error": "Cliente local não autorizado."}), 401

    def campaign_closing_response():
        return jsonify({"error_code": "campaign_closing", "error": "O aplicativo está encerrando os envios. Nenhuma campanha nova será iniciada."}), 409

    @app.before_request
    def admit_campaign_request():
        scoped = (request.path == "/api/campaigns"
                  or request.path.startswith("/api/campaigns/")
                  or request.path in {"/api/recovery/resume", "/api/recovery/reconcile"})
        if (not scoped or request.path == "/api/campaigns/drain"
                or request.method not in {"POST", "PUT", "PATCH", "DELETE"}):
            return
        with _HEAVY_RUNNER_LOCK:
            if campaign_drain["requested"]:
                return campaign_closing_response()
            if nymeria_library_worker.running and request.path in {
                    "/api/campaigns", "/api/campaigns/preflight", "/api/campaigns/original",
                    "/api/campaigns/original/preflight", "/api/recovery/resume"}:
                return jsonify({"error_code": "library_preparation_busy",
                                "error": "Aguarde a preparação da Biblioteca terminar antes de iniciar envios."}), 409
            campaign_drain["requests"] += 1
            g.campaign_request_admitted = True

    @app.before_request
    def guard_campaign_state():
        scoped = any(request.path == prefix or request.path.startswith(prefix + "/")
                     for prefix in ("/api/campaigns", "/api/recovery", "/api/logs",
                                    "/api/sent", "/api/tasks", "/api/holo-cache", "/api/storage",
                                    "/api/library"))
        if not scoped or request.path == "/api/campaigns/drain":
            return
        lease = campaign_state_lease()
        try:
            lease.__enter__()
        except CampaignStateLeaseError as exc:
            code = getattr(exc, "code", "campaign_reset_busy")
            return jsonify({"error_code": code,
                            "error": ("A limpeza local foi interrompida. Use Reset completo novamente antes de iniciar envios."
                                      if code == "campaign_reset_incomplete" else
                                      "A limpeza local está em andamento. Aguarde terminar.")}), 409
        g.campaign_state_lease = lease
        if campaign_reset.pending() and (request.path.startswith("/api/campaigns")
                                        or request.path.startswith("/api/recovery")):
            return jsonify({"error_code": "campaign_reset_incomplete",
                            "error": "A limpeza local foi interrompida. Use Reset completo novamente antes de iniciar envios."}), 409

    @app.teardown_request
    def release_campaign_state(_exc):
        lease = g.pop("campaign_state_lease", None)
        if lease is not None:
            lease.__exit__(None, None, None)

    @app.teardown_request
    def release_campaign_request(_exc):
        if g.pop("campaign_request_admitted", False):
            with _HEAVY_RUNNER_LOCK:
                campaign_drain["requests"] -= 1

    @app.errorhandler(SecureStoreError)
    def invalid_secure_store(exc):
        return jsonify({"error": str(exc), "code": "local_vault_unreadable"}), 409

    @app.errorhandler(banned_store.BannedStoreError)
    def invalid_banned_store(exc):
        response = jsonify({"error": str(exc), "code": "archived_accounts_unreadable"})
        response.headers["Cache-Control"] = "no-store"
        return response, 409

    @app.errorhandler(token_store.TokenStoreError)
    def invalid_token_identity(exc):
        response = jsonify({"error": "O acesso salvo está inválido ou possui identidade conflitante. Preserve os arquivos e revise o acesso.",
                            "code": "local_token_identity_conflict"})
        response.headers["Cache-Control"] = "no-store"
        return response, 409

    @app.errorhandler(JsonStateError)
    def invalid_authoritative_state(_exc):
        response = jsonify({"error": "Não foi possível ler o estado local com segurança. Os arquivos foram preservados; revise ou restaure um backup válido antes de continuar.",
                            "code": "local_state_unreadable"})
        response.headers["Cache-Control"] = "no-store"
        return response, 409

    @app.before_request
    def bound_original_request():
        if request.method == "POST" and request.path in {
                "/api/campaigns/original", "/api/campaigns/original/preflight"}:
            # Bound the raw body before the generic JSON reader consumes it.
            if request.content_length is None or request.content_length > 1024 * 1024:
                return original_error("original_invalid_request")

    @app.before_request
    def validate_json_body():
        if (request.path.startswith("/api/") and request.method in ("POST", "PUT", "PATCH")
                and request.get_data(cache=True)):
            if not request.is_json or not isinstance(request.get_json(silent=True), dict):
                return jsonify({"error": "O corpo da requisição deve ser um objeto JSON válido."}), 400
        if request.method == "POST" and request.path in ("/api/campaigns", "/api/campaigns/preflight"):
            body = request.get_json(silent=True) or {}
            accounts = body.get("accounts", [])
            tasks = body.get("tasks", [])
            if (not isinstance(accounts, list)
                    or any(not isinstance(email, str) or not email.strip() for email in accounts)):
                return jsonify({"error": "accounts deve ser uma lista de contas válidas."}), 400
            normalized = [email.strip().casefold() for email in accounts]
            if len(set(normalized)) != len(normalized):
                return jsonify({"error": "A seleção contém contas duplicadas."}), 400
            if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
                return jsonify({"error": "tasks deve ser uma lista de categorias válidas."}), 400
            try:
                target = body.get("target_hours", 0)
                if isinstance(target, bool) or not math.isfinite(float(target)):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                return jsonify({"error": "target_hours deve ser um número finito."}), 400

    @app.before_request
    def guard_account_operations():
        if request.path.startswith("/api/integrations/") and request.method in ("PUT", "DELETE"):
            _INTEGRATION_OPERATION_LOCK.acquire()
            g.integration_operation_locked = True
            with _BULK_REGISTER_LOCK:
                if _BULK_REGISTER_STATE.get("state") in ("running", "stopping"):
                    return jsonify({"error": "Aguarde o cadastro terminar antes de alterar a integração de e-mail."}), 409
        account_write = request.path.startswith("/api/accounts") and request.method != "GET"
        campaign_start = request.path in ("/api/campaigns", "/api/campaigns/preflight",
                                         "/api/campaigns/original", "/api/campaigns/original/preflight") and request.method == "POST"
        synchronous_tasks = ((request.path == "/api/tasks" and request.args.get("async") != "1")
                             or request.path == "/api/campaigns/original/tasks")
        if account_write or campaign_start or synchronous_tasks:
            if not _ACCOUNT_OPERATION_LOCK.acquire(blocking=not account_write):
                return jsonify({"error": "Aguarde a operação atual de contas terminar."}), 409
            g.account_operation_locked = True
            with _BULK_REGISTER_LOCK:
                registration_command = request.method == "POST" and (
                    request.path == "/api/accounts/bulk-register"
                    or request.path == "/api/accounts/register" and request.args.get("async") == "1"
                    or request.path.startswith("/api/accounts/") and request.path.endswith("/resume"))
                body = request.get_json(silent=True) or {}
                request_id = body.get("request_id")
                if (registration_command and isinstance(request_id, str) and request_id
                        and _BULK_REGISTER_STATE.get("request_id") == request_id):
                    fingerprint = hashlib.sha256((request.path + json.dumps(body, sort_keys=True)).encode()).hexdigest()
                    if _BULK_REGISTER_STATE.get("request_fingerprint") != fingerprint:
                        return jsonify({"error": "Este identificador já foi usado com outros dados de cadastro."}), 409
                    return jsonify({"ok": True, "request_id": request_id, "existing": True,
                                    "total": _BULK_REGISTER_STATE.get("total", 0),
                                    "domain": _BULK_REGISTER_STATE.get("domain", "")})
                if (_BULK_REGISTER_STATE.get("state") in ("running", "stopping")
                        and request.path != "/api/accounts/bulk-register/stop"):
                    return jsonify({"error": "Aguarde o cadastro em lote terminar."}), 409
            if ORG_MIGRATION.running:
                return jsonify({"error": "Aguarde a migração das organizações terminar."}), 409

    @app.teardown_request
    def release_account_operation(_error):
        if g.pop("integration_operation_locked", False):
            _INTEGRATION_OPERATION_LOCK.release()
        if g.pop("account_operation_locked", False):
            _ACCOUNT_OPERATION_LOCK.release()

    @app.get("/")
    def index():
        return jsonify({"service": "qmoney", "ui": "qt", "ok": True})

    # -- contas ---------------------------------------------------------------
    @app.get("/api/accounts")
    def get_accounts():
        accounts = _list_accounts()
        registrations = registration_state.load()
        removed = _removed_accounts()
        health = _load_account_health_history()
        known = {row["email"].strip().casefold() for row in accounts}
        for email, registration in registrations.items():
            if (email not in known and email not in removed
                    and credential_store.record_path(config.SECRETS_DIR, email).exists()):
                accounts.append({"email": email, "expires_at": 0, "org_key": None,
                                 "org_name": "Não verificada", "account_kind": "crowtado",
                                 "last_check": health.get(email, {}), "has_minute_access": config.token_path(email).exists()})
        # Each row performs a strict primary lookup below. Do not decrypt the
        # entire primary store first, then decrypt those same records again.
        passwords = _crowtado_creds(include_individual=False)
        balances = _load_balances()
        for account in accounts:
            account.setdefault("has_minute_access", True)
            registration = registrations.get(account["email"].strip().casefold())
            if registration:
                account["registration"] = registration
            account["has_password"] = bool(_saved_account_password(account["email"], passwords))
            record = balances.get(account["email"], {})
            account["restriction"] = wallet.restriction(record, wallet.reading(record), account)
            registration_bans = []
            if registration:
                for name, step in registration["steps"].items():
                    if step.get("code") != "restricted":
                        continue
                    service = "minute" if name == "minute_register" else "crowtado"
                    check = account_health.provider(account["last_check"], service)
                    try:
                        newer = (check.get("status") == "active" and
                            datetime.datetime.fromisoformat(check["checked_at"]) >= datetime.datetime.fromisoformat(registration["updated_at"]))
                    except (KeyError, TypeError, ValueError):
                        newer = False
                    if not newer:
                        registration_bans.append(service.title())
            if registration_bans:
                sources = " e ".join(dict.fromkeys(registration_bans))
                account["restriction"] = {"code": "restricted", "confirmed": True,
                    "label": "Banida · " + sources, "reason": sources + " confirmou banimento durante o cadastro. Verifique os serviços novamente ou solicite regularização ao suporte."}
        return jsonify({"accounts": accounts})

    @app.post("/api/accounts/password")
    def account_password():
        body = request.get_json(silent=True) or {}
        email = str(body.get("email") or "").strip().casefold()
        if not email:
            return jsonify({"error": "informe a conta"}), 400
        with _PERSISTENCE_LOCK:
            active = any(str(account["email"]).strip().casefold() == email for account in _list_accounts())
            if not active and (email in _removed_accounts() or email not in registration_state.load()):
                return jsonify({"error": "conta não encontrada"}), 404
            password = _saved_account_password(email)
        if not password:
            return jsonify({"error": "esta conta não possui senha salva"}), 404
        response = jsonify({"email": email, "password": password})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/accounts/migration")
    def get_org_migration():
        response = jsonify(ORG_MIGRATION.snapshot())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/accounts/migration")
    def start_org_migration():
        if RUNNER.running or RECOVERY.running or BALANCES_RUNNER.running:
            return jsonify({"error": "Aguarde a campanha ou consulta de saldos terminar antes de migrar."}), 409
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Seleção de contas inválida."}), 400
        known = {item["email"].strip().casefold(): item["email"] for item in _list_accounts()}
        selected = body.get("emails", list(known))
        if not isinstance(selected, list) or not selected or any(
                not isinstance(email, str) or email.strip().casefold() not in known for email in selected):
            return jsonify({"error": "Selecione contas cadastradas no QMoney."}), 400
        emails = list(dict.fromkeys(known[email.strip().casefold()] for email in selected))
        try:
            result = ORG_MIGRATION.start(emails, _migrate_account_org)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        except OSError:
            return jsonify({"error": "Não foi possível salvar o relatório da migração."}), 500
        return jsonify(result), 202

    @app.get("/api/accounts/banned/monitor")
    def banned_monitor_snapshot():
        archive = banned_store.load(config.DATA_DIR / "banned_accounts.json", {"accounts": []})
        passwords = _crowtado_creds()
        rows = []
        for row in archive.get("accounts", []):
            eligible, reason = _banned_withdraw_eligibility(row)
            email = str(row.get("email") or "").strip().casefold()
            rows.append({"email": row["email"], "banned_at": row.get("banned_at") or row.get("removed_at"),
                         "has_password": bool(row.get("password") or _saved_account_password(email, passwords)),
                         "monitor": row.get("monitor", {}),
                         "withdraw_eligible": eligible, "withdraw_reason": reason})
        return jsonify({"accounts": rows, "runner": banned_monitor.snapshot()})

    @app.post("/api/accounts/banned/password")
    def banned_account_password():
        """Revela sob demanda a credencial local de uma conta arquivada."""
        body = request.get_json(silent=True) or {}
        email = str(body.get("email") or "").strip().casefold()
        if not email:
            return jsonify({"error": "informe a conta banida"}), 400
        with _PERSISTENCE_LOCK:
            archive = banned_store.load(config.DATA_DIR / "banned_accounts.json", {"accounts": []})
            row = next((item for item in archive.get("accounts", [])
                        if str(item.get("email") or "").strip().casefold() == email), None)
            if row is None:
                return jsonify({"error": "conta não encontrada no registro de banidas"}), 404
            password = row.get("password") or _saved_account_password(email)
        if not password:
            return jsonify({"error": "esta conta não possui senha salva"}), 404
        response = jsonify({"email": email, "password": password})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/accounts/banned/withdraw")
    def request_banned_withdraw():
        """Solicita saque Crowtado de conta arquivada, sem autenticar no Minute."""
        body = request.get_json(silent=True) or {}
        email = str(body.get("email") or "").strip().casefold()
        if not email:
            return jsonify({"error": "informe a conta banida"}), 400
        with _PERSISTENCE_LOCK:
            archive = banned_store.load(config.DATA_DIR / "banned_accounts.json", {"accounts": []})
            row = next((item for item in archive.get("accounts", [])
                        if str(item.get("email") or "").strip().casefold() == email), None)
            row = dict(row) if row is not None else None
        if row is None:
            return jsonify({"error": "conta não está no registro de banidas"}), 404
        eligible, reason = _banned_withdraw_eligibility(row)
        if not eligible:
            return jsonify({"error": reason}), 400
        if banned_monitor.snapshot().get("state") == "running" or BALANCES_RUNNER.running:
            return jsonify({"error": "aguarde a consulta de saldos terminar"}), 409
        if _withdraw_bulk_snapshot()["state"] == "running":
            return jsonify({"error": "aguarde o saque em lote terminar"}), 409
        try:
            balance = crowtado.consultar_saldo_api(email, row["password"])
        except (crowtado.CrowtadoError, OSError, TimeoutError, ValueError) as exc:
            reason = account_issue(email, exc, stage="Consulta de saldo Crowtado")["reason"]
            message = f"não foi possível confirmar o saldo na Crowtado: {reason}"
            try:
                _remember_banned_withdraw_result(email, "balance_error", message, balance_error=True)
            except (OSError, banned_store.BannedStoreError):
                pass
            return jsonify({"error": message}), 400
        if not _confirmed_available_balance(balance):
            try:
                _remember_banned_withdraw_result(
                    email, "no_balance", "O saldo aprovado deve ser superior a US$ 25,00 para saque na Crowtado.", balance=balance)
            except (OSError, banned_store.BannedStoreError):
                pass
            return jsonify({"error": "o saldo aprovado deve ser superior a US$ 25,00 para saque na Crowtado"}), 400
        result, status = _withdraw_once(email, row["password"])
        provider_status = str((result.get("result") or {}).get("status") or "")
        if provider_status or not result.get("ok"):
            try:
                _remember_banned_withdraw_result(
                    email, provider_status or "request_failed",
                    result.get("message") if provider_status else
                    "Não foi possível solicitar o saque; atualize o saldo antes de tentar novamente.",
                    balance=balance)
            except (OSError, banned_store.BannedStoreError):
                pass  # A solicitação externa já ocorreu; não a repetir por falha local.
        return jsonify(result), status

    @app.post("/api/accounts/banned/refresh")
    def refresh_banned_monitor():
        with _PERSISTENCE_LOCK:
            archive = banned_store.load(config.DATA_DIR / "banned_accounts.json", {"accounts": []})
            rows = archive.get("accounts", [])
            if not rows:
                return jsonify({"error": "Não há contas banidas registradas."}), 400
            def save(email, result):
                with _PERSISTENCE_LOCK:
                    path = config.DATA_DIR / "banned_accounts.json"
                    current = banned_store.load(path, {"accounts": []})
                    for row in current.get("accounts", []):
                        if row.get("email", "").casefold() == email.casefold():
                            row["monitor"] = result
                    banned_store.save(path, current)
            try:
                banned_monitor.start(rows, save)
            except RuntimeError as exc:
                return jsonify({"error": str(exc)}), 409
        return jsonify(banned_monitor.snapshot()), 202

    @app.get("/api/accounts/banned")
    def banned_accounts():
        with _PERSISTENCE_LOCK:
            path = config.DATA_DIR / "banned_accounts.json"
            archive = banned_store.load(path, {"schema": 1, "accounts": []})
            passwords = _crowtado_creds()
            changed = False
            for row in archive.get("accounts", []):
                email = str(row.get("email", "")).strip().casefold()
                for key, value in (("password", row.get("password") or _saved_account_password(email, passwords)),
                                   ("banned_at", row.get("banned_at") or row.get("removed_at"))):
                    if key not in row or row[key] != value:
                        row[key] = value
                        changed = True
            if changed:
                banned_store.save(path, archive)
            response = jsonify(archive)
            response.headers["Cache-Control"] = "no-store"
            return response

    @app.post("/api/accounts/import")
    def import_accounts():
        if request.content_length and request.content_length > account_transfer.MAX_BYTES * 2:
            return jsonify({"error": "Arquivo muito grande; limite de 10 MB."}), 413
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("content"), str) or type(body.get("apply", False)) is not bool:
            return jsonify({"error": "Informe o conteúdo JSON e uma opção de importação válida."}), 400
        if body.get("apply") and (RUNNER.running or BALANCES_RUNNER.running):
            return jsonify({"error": "Aguarde a campanha ou consulta de saldos terminar antes de importar."}), 409
        try:
            with _PERSISTENCE_LOCK:
                result = account_transfer.import_accounts(body["content"], apply=body.get("apply", False),
                    passwords_path=CROWTADO_PW_PATH, removed_path=_removed_accounts_path())
            return jsonify(result)
        except (JsonStateError, token_store.TokenStoreError):
            raise
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except OSError:
            return jsonify({"error": "Não foi possível acessar os arquivos locais das contas."}), 500

    @app.post("/api/accounts/export")
    def export_accounts():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or ("emails" in body and not isinstance(body["emails"], list)):
            return jsonify({"error": "Seleção de contas inválida."}), 400
        try:
            with _PERSISTENCE_LOCK:
                accounts = _list_accounts()
                selected = ({str(email).strip().casefold() for email in body.get("emails", [])}
                            if body.get("emails") is not None else
                            {str(account["email"]).strip().casefold() for account in accounts})
                credentials = _crowtado_creds()
                corrupt = []
                for account in accounts:
                    account_email = str(account["email"]).strip()
                    key = account_email.casefold()
                    if key not in selected or org_policy.account_kind(account_email) == "claru":
                        continue
                    try:
                        individual = credential_store.lookup(
                            config.SECRETS_DIR, account_email, strict=True)
                    except ValueError:
                        corrupt.append(account_email)
                        continue
                    if individual:
                        credentials[key] = individual
                if corrupt:
                    return jsonify({
                        "error": "Exportação cancelada: há credencial local corrompida; os arquivos foram preservados para recuperação.",
                        "accounts": sorted(corrupt),
                    }), 409
                missing = sorted(
                    str(account["email"]).strip() for account in accounts
                    if str(account["email"]).strip().casefold() in selected
                    and org_policy.account_kind(str(account["email"])) != "claru"
                    and not credentials.get(str(account["email"]).strip().casefold())
                )
                if missing:
                    return jsonify({
                        "error": "Exportação cancelada: há conta Crowtado sem senha local; nenhum backup incompleto foi criado.",
                        "accounts": missing,
                    }), 409
                result = account_transfer.export_accounts(body.get("emails"), credentials, _removed_accounts())
            response = jsonify(result)
            response.headers["Cache-Control"] = "no-store"
            return response
        except (JsonStateError, token_store.TokenStoreError):
            raise
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except OSError:
            return jsonify({"error": "Não foi possível ler as contas para exportação."}), 500

    @app.post("/api/accounts")
    def add_account():
        body = request.get_json(silent=True) or {}
        email = body.get("email", "")
        password = body.get("password", "")
        if not isinstance(email, str) or not isinstance(password, str):
            return jsonify({"error": "email e senha devem ser texto", "code": "invalid_credentials"}), 400
        email = email.strip()
        if not email or not password:
            return jsonify({"error": "informe email e senha"}), 400
        try:
            account_bans.require_not_banned(email)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            login(email, password)
        except (RuntimeError, OSError, AuthError) as exc:
            issue = account_issue(email, exc, stage="Login Minute")
            return jsonify({"error": issue["reason"] + " " + issue["action"],
                            "code": "account_login_failed", "issue": issue}), 400
        try:
            # Reconectar também é um gate de organização: uma conta Crowtado
            # antiga só volta a ficar ativa depois de confirmar PE8EAR5V.
            _resolve_org(email)
        except (RuntimeError, OSError, AuthError) as exc:
            issue = account_issue(email, exc, stage="Confirmação de organização Minute")
            return jsonify({"error": issue["reason"] + " " + issue["action"],
                            "code": "account_organization_unconfirmed", "issue": issue}), 400
        try:
            _save_crowtado_cred(email, password)
            _set_account_removed(email, False)
        except (OSError, ValueError):
            return jsonify({"ok": False, "partial": True,
                            "error": "O acesso foi validado, mas o salvamento local não foi concluído. Confira o armazenamento e o backup das credenciais antes de tentar novamente."}), 500
        return jsonify({"ok": True, "email": email})

    @app.post("/api/accounts/register")
    def register_account():
        """Cria Crowtado com e-mail verificado e Minute; site fica manual."""
        if request.args.get("async") == "1":
            return bulk_register_accounts()
        body = request.get_json(silent=True) or {}
        email = body.get("email", "")
        password = body.get("password", "")
        if not isinstance(email, str) or not isinstance(password, str):
            return jsonify({"error": "email e senha devem ser texto", "code": "invalid_credentials"}), 400
        email = email.strip()
        if not email or not password:
            return jsonify({"error": "informe email e senha"}), 400
        if not _hostinger_is_configured():
            return jsonify({"error": "Configure a integração Hostinger (domínio catch-all) antes de criar contas."}), 409
        identity_data: dict[str, Any] = {
            "nome": email.split("@")[0], "sobrenome": "",
            "email": email, "senha": password,
            "use_referral": body.get("use_referral", True),
            "proxy_id": body.get("proxy_id", ""),
        }
        if type(identity_data["use_referral"]) is not bool:
            return jsonify({"error": "use_referral deve ser booleano."}), 400
        result = _full_register_account(email, password, identity_data)
        ok = result["error"] is None
        return jsonify({"ok": ok, "email": email, "partial": result.get("partial", False), "steps": result["steps"],
                         "error": result["error"]}), 200 if ok else 400

    @app.get("/api/accounts/proxies")
    def list_registration_proxies():
        from .. import registration_proxy
        return jsonify({"proxies": registration_proxy.public_rows()})

    @app.post("/api/accounts/proxies/import")
    def import_registration_proxies():
        from .. import registration_proxy
        if RUNNER.running or RECOVERY.running or BALANCES_RUNNER.running:
            return jsonify({"error": "Pare a operação antes de importar proxies."}), 409
        try:
            result = registration_proxy.import_text((request.get_json(silent=True) or {}).get("text"))
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, **result})

    @app.get("/api/accounts/domains")
    def list_account_domains():
        """Domínios disponíveis para o criador de contas (perfis Hostinger)."""
        warnings = _recover_hostinger_domains()
        domains = _registration_domains()
        return jsonify({
            "domains": domains,
            "webmail_url": "https://webmail.hostinger.com" if domains else "",
            "hostinger_configured": _hostinger_is_configured(),
            "warning": " ".join(warnings),
        })

    @app.get("/api/accounts/bulk-register/preflight")
    def bulk_register_preflight():
        """Valida todas as dependências antes de iniciar a criação em lote."""
        from .. import registration_proxy
        selection = request.args.get("proxy_id", "")
        try:
            registration_proxy.validate_selection(selection)
            proxy = registration_proxy.selected(selection)
            with registration_proxy.route(proxy):
                ip = registration_proxy.check_exit_ip() if proxy else None
                result = _preflight_checks(request.args.get("domain", ""))
                if proxy:
                    result["checks"]["proxy"] = {"ok": True, "detail": f"IP de saída {ip}"}
                return jsonify(result)
        except (OSError, ValueError, RuntimeError):
            return jsonify({"ready": False, "checks": {"proxy": {"ok": False, "detail": "Proxy indisponível; confira a seleção e a conexão."}}})

    @app.get("/api/accounts/bulk-register/status")
    def bulk_register_status():
        with _BULK_REGISTER_LOCK:
            response = jsonify(json.loads(json.dumps(_BULK_REGISTER_STATE)))
            response.headers["Cache-Control"] = "no-store"
            return response

    @app.post("/api/accounts/bulk-register/stop")
    def stop_bulk_register():
        with _BULK_REGISTER_LOCK:
            if _BULK_REGISTER_STATE.get("state") == "running":
                _BULK_REGISTER_STATE["state"] = "stopping"
                _save_registration_batch()
            return jsonify({"ok": True, "state": _BULK_REGISTER_STATE.get("state", "idle")})

    @app.post("/api/accounts/<email>/resume")
    def resume_account(email: str):
        try:
            key = credential_store.email_key(email)
            row = registration_state.load().get(key)
            password = _saved_account_password(key)
        except ValueError:
            return jsonify({"error": "Conta inválida."}), 400
        if not row or not password or key in _removed_accounts():
            return jsonify({"error": "Cadastro ou credencial de retomada indisponível."}), 404
        if row["state"] == "complete":
            return jsonify({"error": "Esta conta já possui cadastro completo."}), 409
        return bulk_register_accounts(single={"email": key, "password": password,
                                             **row["identity"], "resume": True,
                                             "request_id": (request.get_json(silent=True) or {}).get("request_id", "")})

    @app.post("/api/accounts/bulk-register")
    def bulk_register_accounts(single=None):
        """Cria contas Crowtado + Minute com proxy selecionado e progresso durável."""
        with _BULK_REGISTER_LOCK:
            if _BULK_REGISTER_STATE.get("state") in ("running", "stopping"):
                return jsonify({"error": "Já existe uma criação em andamento."}), 409

        body = single or request.get_json(silent=True) or {}
        from .. import registration_proxy
        proxy_id = body.get("proxy_id", "")
        try:
            registration_proxy.validate_selection(proxy_id)
        except (OSError, ValueError):
            return jsonify({"error": "Selecione um proxy disponível ou importe o arquivo TXT."}), 400
        use_referral = body.get("use_referral", True)
        if type(use_referral) is not bool:
            return jsonify({"error": "use_referral deve ser booleano."}), 400
        request_body = request.get_json(silent=True) or {}
        request_id = request_body.get("request_id", "")
        if not isinstance(request_id, str) or len(request_id) > 80:
            return jsonify({"error": "Identificador de cadastro inválido."}), 400
        request_fingerprint = hashlib.sha256((request.path + json.dumps(request_body, sort_keys=True)).encode()).hexdigest()
        manual = "email" in body
        if manual:
            try:
                email = credential_store.email_key(body.get("email"))
                password = body.get("password")
                if not isinstance(password, str) or not password or len(password) > 4096:
                    raise ValueError("Informe uma senha válida.")
                month, year = body.get("birth_month"), body.get("birth_year")
                if (("birth_month" in body and (type(month) is not int or not 1 <= month <= 12))
                        or ("birth_year" in body and (type(year) is not int or not 1900 <= year <= datetime.date.today().year))
                        or ("gender" in body and body.get("gender") not in crowtado.GENDERS)):
                    raise ValueError("Informe mês, ano de nascimento e gênero válidos para o cadastro.")
                identity_data = {"email": email, "senha": password,
                                 "nome": body.get("nome", email.split("@")[0]),
                                 "sobrenome": body.get("sobrenome", ""),
                                  **{key: body[key] for key in ("birth_month", "birth_year", "gender") if key in body}}
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
        try:
            count = 1 if manual else body.get("count", 0)
            if type(count) is not int:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "count inválido"}), 400
        domain = email.split("@", 1)[1] if manual else str(body.get("domain", "")).strip().lower().lstrip("@")
        if count < 1 or count > 50:
            return jsonify({"error": "count deve estar entre 1 e 50"}), 400
        if not domain:
            return jsonify({"error": "domain é obrigatório"}), 400
        if RUNNER.running or RECOVERY.running or BALANCES_RUNNER.running:
            return jsonify({"error": "pare a campanha antes de criar contas"}), 409
        if not _hostinger_is_configured():
            return jsonify({"error": "Configure a integração Hostinger (domínio catch-all) antes de criar contas."}), 409

        if domain not in {row["domain"] for row in _registration_domains()}:
            return jsonify({"error": "Selecione um domínio catch-all configurado nas integrações."}), 400

        existing_emails = {
            account["email"].lower()
            for account in _list_accounts()
        }

        with _BULK_REGISTER_LOCK:
            _BULK_REGISTER_STATE.clear()
            _BULK_REGISTER_STATE.update({
                "state": "running",
                "total": count,
                "completed": 0,
                "created": 0,
                "failed": 0,
                "results": [],
                "current_email": "",
                "current_step": "",
                "domain": domain,
                "stopped": False,
                "request_id": request_id,
                "request_fingerprint": request_fingerprint,
            })
            try:
                _save_registration_batch()
            except OSError:
                # No remote work has started. Do not leave the UI locked in a
                # running state when the initial durable checkpoint failed.
                _BULK_REGISTER_STATE.update(
                    state="failed", error="Não foi possível salvar o progresso local. Nenhuma conta foi criada."
                )
                return jsonify({"error": _BULK_REGISTER_STATE["error"]}), 503

        def _run_batch() -> bool:
            successes = 0
            failures = 0
            results: list[dict[str, Any]] = []
            used_emails: set[str] = set(existing_emails)
            for index in range(count):
                with _BULK_REGISTER_LOCK:
                    if _BULK_REGISTER_STATE.get("state") == "stopping":
                        _BULK_REGISTER_STATE["stopped"] = True
                        break
                try:
                    account_identity = dict(identity_data) if manual else identity.gerar_identidade(
                        domain=domain,
                        existentes=used_emails,
                    )
                except RuntimeError as exc:
                    with _BULK_REGISTER_LOCK:
                        _BULK_REGISTER_STATE["state"] = "failed"
                        _BULK_REGISTER_STATE["error"] = str(exc)
                    return False
                email = account_identity["email"]
                password = account_identity["senha"]
                if not manual:
                    for key in ("gender", "birth_month", "birth_year"):
                        account_identity.pop(key, None)
                account_identity["use_referral"] = use_referral
                account_identity["proxy_id"] = proxy_id
                used_emails.add(email.lower())

                def _on_step(step_name: str) -> None:
                    with _BULK_REGISTER_LOCK:
                        _BULK_REGISTER_STATE["current_email"] = email
                        _BULK_REGISTER_STATE["current_step"] = _STEP_LABELS.get(step_name, step_name)
                        _save_registration_batch()

                result = _full_register_account(email, password, account_identity, on_step=_on_step,
                                                **({"resume": True} if body.get("resume") is True else {}))
                ok = result["error"] is None
                removable = (
                    config.token_path(email).exists()
                    or credential_store.record_path(config.SECRETS_DIR, email).exists()
                )
                if ok:
                    successes += 1
                else:
                    failures += 1
                results.append({
                    "email": email,
                    "nome": account_identity["nome"],
                    "sobrenome": account_identity["sobrenome"],
                    "gender": account_identity.get("gender"),
                    "birth_month": account_identity.get("birth_month"),
                    "birth_year": account_identity.get("birth_year"),
                    "created": ok,
                    "partial": result.get("partial", False),
                    "removable": removable,
                    "removed": False,
                    "error": result["error"],
                    "steps": result["steps"],
                })
                with _BULK_REGISTER_LOCK:
                    _BULK_REGISTER_STATE["completed"] = index + 1
                    _BULK_REGISTER_STATE["created"] = successes
                    _BULK_REGISTER_STATE["failed"] = failures
                    _BULK_REGISTER_STATE["results"] = list(results)
                    _save_registration_batch()
            return True
        def _worker() -> None:
            terminal = {"state": "failed", "error": "Cadastro interrompido."}
            try:
                if _run_batch():
                    terminal = {"state": "done"}
            except Exception:
                terminal = {
                    "state": "failed",
                    "error": "Não foi possível concluir o cadastro. Confira as contas e credenciais salvas antes de tentar novamente.",
                }
            finally:
                with _BULK_REGISTER_LOCK:
                    _BULK_REGISTER_STATE.update(terminal)
                    _BULK_REGISTER_STATE["current_email"] = ""
                    _BULK_REGISTER_STATE["current_step"] = ""
                    try:
                        _save_registration_batch()
                    except OSError:
                        _BULK_REGISTER_STATE.update(state="failed", error="Não foi possível salvar o progresso local. Confira as credenciais e os cadastros antes de tentar novamente.")

        try:
            thread = threading.Thread(target=_worker, name="moneymin-bulk-register", daemon=True)
            thread.start()
        except Exception:
            with _BULK_REGISTER_LOCK:
                _BULK_REGISTER_STATE.update(state="failed", error="Não foi possível iniciar o cadastro.")
            return jsonify({"error": "Não foi possível iniciar o cadastro."}), 500
        return jsonify({"ok": True, "total": count, "domain": domain, "request_id": request_id})

    @app.delete("/api/accounts/<email>")
    def remove_account(email: str):
        if RUNNER.running or RECOVERY.running:
            return jsonify({"error": "pare a campanha antes de remover uma conta"}), 409
        if BALANCES_RUNNER.running:
            return jsonify({"error": "aguarde a consulta de saldos terminar"}), 409
        try:
            normalized = account_transfer.email_key(email)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        found = token_store.load(config.SECRETS_DIR, normalized, migrate=False)
        credential_path = credential_store.record_path(config.SECRETS_DIR, normalized)
        if found is None and not credential_path.exists():
            return jsonify({"error": f"conta não encontrada: {email}"}), 404
        # Grave primeiro a intenção. Mesmo se o Windows interromper a remoção
        # física, a conta já não reaparece nem volta para uma campanha.
        _set_account_removed(email, True)
        try:
            token_store.delete(config.SECRETS_DIR, normalized)
            with _PERSISTENCE_LOCK:
                prefs = _load_prefs()
                normalized = email.casefold()
                org_keys = prefs.get("org_keys", {})
                if isinstance(org_keys, dict):
                    for key in list(org_keys):
                        if str(key).casefold() == normalized:
                            org_keys.pop(key, None)
                selected = prefs.get("selected_accounts")
                if isinstance(selected, list):
                    prefs["selected_accounts"] = [
                        value for value in selected
                        if str(value).casefold() != normalized
                    ]
                _save_prefs(prefs)
            _remove_account_data(email)
        except OSError as exc:
            _mark_bulk_account_removed(normalized)
            return jsonify({
                "ok": True,
                "warning": f"a conta não voltará, mas alguns dados locais aguardam nova tentativa de limpeza: {exc}",
            })
        _mark_bulk_account_removed(normalized)
        return jsonify({"ok": True})

    @app.post("/api/accounts/<email>/check")
    def check_account(email: str):
        result = _check_account_health(email)
        ok = result["status"] == "active"
        return jsonify({"ok": ok, **result}), 200 if ok else 400

    @app.post("/api/accounts/check-all")
    def check_all_accounts():
        if RUNNER.running or RECOVERY.running:
            return jsonify({
                "error": "aguarde ou pare a campanha antes de verificar todas as contas",
            }), 409
        emails = [account["email"] for account in _list_accounts()]
        results_by_email: dict[str, dict[str, Any]] = {}
        if emails:
            workers = min(3, len(emails))
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="moneymin-account-check") as pool:
                futures = {pool.submit(_check_account_health, email): email
                           for email in emails}
                for future in as_completed(futures):
                    email = futures[future]
                    try:
                        results_by_email[email] = future.result()
                    except Exception as exc:  # noqa: BLE001 — uma conta não mata o lote
                        issue = account_issue(email, exc)
                        results_by_email[email] = {
                            "email": email, "status": "inconclusive", "status_label": "Verificação inconclusiva",
                            "error": issue_text(issue), "issue": issue,
                        }
        results = [results_by_email[email] for email in emails]
        return jsonify({
            "ok": True,
            "total": len(results),
            "active": sum(result["status"] == "active" for result in results),
            "disabled": [result for result in results
                         if result["status"] == "disabled"],
            "errors": [result for result in results if result["status"] not in ("active", "disabled")],
            "inconclusive": sum(result["status"] == "inconclusive" for result in results),
            "results": results,
        })

    # -- categorias --------------------------------------------------------------
    @app.get("/api/tasks")
    def get_tasks():
        email = str(request.args.get("email", "")).strip()
        if not email:
            return jsonify({"error": "informe ?email=<conta>"}), 400
        try:
            dataset_provider = _local_dataset_provider(
                request.args.get("dataset")
            )
            content_mode = campaign.normalize_content_mode(
                request.args.get("content_mode")
            )
        except ValueError:
            return jsonify({"error": "Dataset ou modo de conteúdo inválido.",
                            "code": "invalid_task_selection"}), 400
        try:
            min_dur_s, max_dur_s = _parse_duration_range(request.args)
        except ValueError:
            return jsonify({"error": "Informe um intervalo de duração válido.",
                            "code": "invalid_task_duration"}), 400
        if request.args.get("async") == "1":
            key = (email, dataset_provider, content_mode, min_dur_s, max_dur_s,
                   _catalog_identity_fingerprint([email]))
            job_id = str(request.args.get("job_id", "")).strip()
            if job_id:
                result, status = task_catalog.poll(job_id, key=key, scope="campaign-tasks")
                return jsonify(result), status

            def load(progress):
                try:
                    progress("Conferindo acesso e categorias da conta…")
                    with _ACCOUNT_OPERATION_LOCK:
                        if ORG_MIGRATION.running or _BULK_REGISTER_STATE.get("state") == "running":
                            return {"error": "Aguarde a operação de contas em andamento."}, 409
                        sess = Session.from_email(email)
                        org_key = _resolve_org(email, session=sess)
                        remote_tasks = sess.all_tasks(org_key)
                    # Local indexing does not hold account authentication or
                    # mutation locks. Polling requests never wait on this work.
                    progress("Preparando catálogo para a duração e o conteúdo selecionados…")
                    tasks = campaign.available_tasks(
                        email, org_key, min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                        include_unavailable=True, dataset_provider=dataset_provider,
                        content_mode=content_mode, remote_tasks=remote_tasks)
                    return {"email": email, "org_key": org_key, "tasks": tasks,
                            "dataset": dataset_provider, "content_mode": content_mode,
                            "scenarios_pt": campaign.SCENARIO_PT}, 200
                except Exception as exc:
                    issue = account_issue(email, exc, stage="Carregamento de categorias")
                    return {"error": issue["reason"] + " " + issue["action"], "issue": issue}, 400

            result, status = task_catalog.get(
                key, load, scope="campaign-tasks",
                refresh=request.args.get("refresh") == "1")
            return jsonify(result), status
        try:
            # Uma Session só: _resolve_org + catálogo. Dois refresh seguidos
            # no Firebase invalidam o refreshToken e o GET vira 400.
            sess = Session.from_email(email)
            org_key = _resolve_org(email, session=sess)
            tasks = campaign.available_tasks(
                email, org_key, min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                include_unavailable=True, dataset_provider=dataset_provider,
                content_mode=content_mode,
                session=sess)
        except json.JSONDecodeError:
            return jsonify({
                "error": "a API devolveu resposta vazia (não-JSON). Tente de novo.",
                "code": "task_response_invalid",
            }), 400
        except AuthError:
            app.logger.warning("GET /api/tasks: task_auth_failed")
            return jsonify({"error": "Não foi possível validar o acesso da conta. Reconecte a conta e tente novamente.",
                            "code": "task_auth_failed"}), 400
        except (RuntimeError, OSError):
            app.logger.warning("GET /api/tasks: task_catalog_unavailable")
            return jsonify({"error": "Não foi possível carregar as categorias. Confira o acesso da conta e a biblioteca local.",
                            "code": "task_catalog_unavailable"}), 400
        return jsonify({"email": email, "org_key": org_key,
                        "dataset": dataset_provider, "content_mode": content_mode,
                        "tasks": tasks,
                        "scenarios_pt": campaign.SCENARIO_PT})

    # -- preferências -------------------------------------------------------------
    @app.get("/api/preferences")
    def get_prefs():
        return jsonify(_load_prefs())

    @app.put("/api/preferences")
    def put_prefs():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "body JSON inválido"}), 400
        with _PERSISTENCE_LOCK:
            prefs = _load_prefs()
            prefs.update(body)
            _save_prefs(prefs)
        return jsonify(prefs)

    @app.get("/api/integrations")
    def get_integrations():
        """Estados e dicas somente; nunca devolve um segredo salvo."""
        try:
            return jsonify(_integration_snapshot())
        except SecureStoreError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({"error": _integration_error(exc, "configuração local")}), 400

    @app.put("/api/integrations/ego4d")
    def put_ego4d_integration():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "preencha as credenciais do Ego4D"}), 400
        secure = _migrate_legacy_integrations()
        current = (secure.get("ego4d")
                   if isinstance(secure.get("ego4d"), dict)
                   else _legacy_aws_credentials())
        values = {
            "access_key_id": str(body.get("access_key_id") or current.get("access_key_id") or "").strip(),
            "secret_access_key": str(body.get("secret_access_key") or current.get("secret_access_key") or "").strip(),
            "session_token": str(
                body.get("session_token") if "session_token" in body
                else current.get("session_token") or "").strip(),
            "region": str(
                body.get("region") if "region" in body
                else current.get("region") or "").strip(),
        }
        if len(values["access_key_id"]) < 12 or len(values["secret_access_key"]) < 20:
            return jsonify({
                "error": "informe o Access Key ID e o Secret Access Key recebidos do Ego4D",
            }), 400
        try:
            tested = _test_ego4d(values)
        except Exception as exc:  # noqa: BLE001 — traduz resposta de boto/AWS
            return jsonify({"error": _integration_error(exc, "AWS do Ego4D")}), 400
        with _PERSISTENCE_LOCK:
            secure["schema"] = 1
            secure["ego4d"] = values
            save_secure_settings(config.INTEGRATIONS_PATH, secure)
            _apply_ego4d(values)
        return jsonify({"ok": True, "test": tested,
                        "integrations": _integration_snapshot()})

    @app.post("/api/integrations/ego4d/test")
    def test_ego4d_integration():
        secure = _migrate_legacy_integrations()
        current = (secure.get("ego4d")
                   if isinstance(secure.get("ego4d"), dict)
                   else _legacy_aws_credentials())
        body = request.get_json(silent=True) or {}
        values = {
            "access_key_id": str(body.get("access_key_id") or current.get("access_key_id") or "").strip(),
            "secret_access_key": str(body.get("secret_access_key") or current.get("secret_access_key") or "").strip(),
            "session_token": str(body.get("session_token") or current.get("session_token") or "").strip(),
            "region": str(body.get("region") or current.get("region") or "").strip(),
        }
        if not values or not values.get("access_key_id") or not values.get("secret_access_key"):
            return jsonify({"error": "configure as credenciais do Ego4D primeiro"}), 400
        try:
            tested = _test_ego4d(values)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _integration_error(exc, "AWS do Ego4D")}), 400
        return jsonify(tested)

    @app.post("/api/integrations/ego4d/catalog")
    def prepare_ego4d_catalog():
        secure = _migrate_legacy_integrations()
        values = (secure.get("ego4d")
                  if isinstance(secure.get("ego4d"), dict)
                  else _legacy_aws_credentials())
        if not values or not values.get("access_key_id") or not values.get("secret_access_key"):
            return jsonify({"error": "configure as credenciais do Ego4D primeiro"}), 400
        _apply_ego4d(values)
        try:
            meta, clips = ego4d.sync_meta()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _integration_error(exc, "AWS do Ego4D")}), 400
        return jsonify({
            "ok": True,
            "message": "Catálogo básico do Ego4D preparado.",
            "metadata_bytes": meta.stat().st_size,
            "clips_bytes": clips.stat().st_size,
        })

    @app.put("/api/integrations/hostinger")
    def put_hostinger_integration():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "informe o token da Hostinger"}), 400
        secure = _migrate_legacy_integrations()
        host = (secure.get("hostinger")
                if isinstance(secure.get("hostinger"), dict) else {})
        profiles = _hostinger_profiles(host)
        if body.get("auto_detect"):
            token = str(body.get("token") or "").strip()
            if len(token) < 16:
                return jsonify({
                    "error": "cole um token válido da API Mail da Hostinger",
                }), 400
            duplicates = [
                item["name"] for item in profiles
                if item["token"] == token and item.get("mailbox_id")
            ]
            if duplicates and all(item["routes"] for item in profiles if item["token"] == token):
                return jsonify({
                    "error": (
                        "esta API Hostinger já está conectada"
                        + (f" ({', '.join(duplicates)})" if duplicates else "")
                    ),
                    "code": "hostinger_already_connected",
                }), 409
            try:
                detected = hostinger_mail.discover_mailboxes(token)
            except Exception as exc:  # noqa: BLE001
                return jsonify({"error": _integration_error(exc, "Hostinger")}), 400
            detected_ids = {item["resource_id"] for item in detected}
            existing_by_mailbox = {
                item["mailbox_id"]: item
                for item in profiles if item.get("mailbox_id")
            }
            # Uma conexão antiga sem ID de caixa é substituída quando o mesmo
            # token é identificado. As demais APIs permanecem intactas.
            profiles = [
                item for item in profiles
                if item.get("mailbox_id") not in detected_ids
                and not (item["token"] == token and not item.get("mailbox_id"))
            ]
            imported = []
            for index, mailbox in enumerate(detected):
                previous = existing_by_mailbox.get(mailbox["resource_id"], {})
                address = mailbox.get("address") or ""
                domain = mailbox.get("domain") or ""
                item = {
                    "id": previous.get("id") or uuid.uuid4().hex,
                    "name": address or f"Caixa Hostinger {index + 1}",
                    "token": token,
                    "mailbox_id": mailbox["resource_id"],
                    "routes": [domain] if domain else [],
                }
                profiles.append(item)
                imported.append({
                    "name": item["name"],
                    "domain": domain,
                })
            with _PERSISTENCE_LOCK:
                secure["schema"] = 2
                secure["hostinger"] = {"profiles": profiles}
                save_secure_settings(config.INTEGRATIONS_PATH, secure)
                _apply_hostinger(profiles)
            return jsonify({
                "ok": True,
                "detected_count": len(imported),
                "detected": imported,
                "integrations": _integration_snapshot(),
            })
        profile_id = str(body.get("profile_id") or "").strip()
        current = next(
            (item for item in profiles if item["id"] == profile_id), {})
        # Compatibilidade com a tela antiga: quando havia apenas uma conexão,
        # um PUT sem id significava editar aquela conexão, não criar outra.
        if not current and not body.get("create") and len(profiles) == 1:
            current = profiles[0]
            profile_id = current["id"]
        values = {
            "id": profile_id or uuid.uuid4().hex,
            "name": str(body.get("name") or current.get("name")
                        or f"Caixa {len(profiles) + 1}").strip(),
            "token": str(body.get("token") or current.get("token") or "").strip(),
            "mailbox_id": str(
                body.get("mailbox_id") if "mailbox_id" in body
                else current.get("mailbox_id") or "").strip(),
            "routes": _hostinger_routes(
                body.get("routes") if "routes" in body
                else current.get("routes") or []),
        }
        if len(values["token"]) < 16:
            return jsonify({"error": "informe um token válido da API Mail da Hostinger"}), 400
        if any(item["token"] == values["token"] and item["id"] != values["id"]
               for item in profiles):
            return jsonify({
                "error": "esta API Hostinger já está conectada",
                "code": "hostinger_already_connected",
            }), 409
        try:
            tested = hostinger_mail.test_connection(
                token=values["token"], mailbox=values["mailbox_id"] or None)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _integration_error(exc, "Hostinger")}), 400
        replaced = False
        for index, item in enumerate(profiles):
            if item["id"] == values["id"]:
                profiles[index] = values
                replaced = True
                break
        if not replaced:
            profiles.append(values)
        with _PERSISTENCE_LOCK:
            secure["schema"] = 2
            secure["hostinger"] = {"profiles": profiles}
            save_secure_settings(config.INTEGRATIONS_PATH, secure)
            _apply_hostinger(profiles)
        return jsonify({"ok": True, "profile_id": values["id"], "test": tested,
                        "integrations": _integration_snapshot()})

    @app.delete("/api/integrations/hostinger/<profile_id>")
    def delete_hostinger_integration(profile_id: str):
        secure = _migrate_legacy_integrations()
        host = (secure.get("hostinger")
                if isinstance(secure.get("hostinger"), dict) else {})
        profiles = _hostinger_profiles(host)
        remaining = [item for item in profiles if item["id"] != profile_id]
        if len(remaining) == len(profiles):
            return jsonify({"error": "conexão Hostinger não encontrada"}), 404
        with _PERSISTENCE_LOCK:
            secure["schema"] = 2
            secure["hostinger"] = {"profiles": remaining}
            save_secure_settings(config.INTEGRATIONS_PATH, secure)
            _apply_hostinger(remaining)
        return jsonify({"ok": True, "integrations": _integration_snapshot()})

    @app.post("/api/integrations/hostinger/test")
    def test_hostinger_integration():
        secure = _migrate_legacy_integrations()
        host = (secure.get("hostinger")
                if isinstance(secure.get("hostinger"), dict) else {})
        profiles = _hostinger_profiles(host)
        body = request.get_json(silent=True) or {}
        profile_id = str(body.get("profile_id") or "").strip()
        current = next(
            (item for item in profiles if item["id"] == profile_id), {})
        if not current and not body.get("create") and len(profiles) == 1:
            current = profiles[0]
        token = str(body.get("token") or current.get("token")
                    or config.HOSTINGER_MAIL_TOKEN or "").strip()
        mailbox = str(body.get("mailbox_id") or current.get("mailbox_id")
                      or config.HOSTINGER_MAILBOX_ID or "").strip()
        if not token:
            return jsonify({"error": "configure o token da Hostinger primeiro"}), 400
        try:
            return jsonify(hostinger_mail.test_connection(
                token=token, mailbox=mailbox or None))
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": _integration_error(exc, "Hostinger")}), 400

    # -- prontidão, biblioteca e diagnóstico -----------------------------------
    @app.get("/api/readiness")
    def get_readiness():
        try:
            provider = _local_dataset_provider(
                request.args.get("dataset")
            )
        except ValueError:
            return jsonify({"error": "Dataset inválido.", "code": "invalid_dataset"}), 400
        try:
            result = readiness.campaign_readiness(provider)
        except (ValueError, OSError, RuntimeError):
            return jsonify({"error": "Não foi possível verificar a prontidão. Confira a biblioteca local e as ferramentas configuradas.",
                            "code": "readiness_unavailable"}), 400
        result["storage"] = _storage_snapshot(include_path=False)
        return jsonify(result)

    @app.get("/api/storage/library")
    def get_storage_library():
        return jsonify(_storage_snapshot(include_path=True))

    @app.get("/api/storage/library/items")
    def browse_local_media_library():
        root = config.MEDIA_DATA_DIR
        try:
            query = request.args.get("q", "")
            provider = request.args.get("provider", "all")
            kind = request.args.get("kind", "all")
            limit = int(request.args.get("limit", 50))
            offset = int(request.args.get("offset", 0))
            refresh = request.args.get("refresh", "")
            if (len(query) > 200 or provider not in {"all", "ego4d", "holoassist", "nymeria", "local"}
                    or kind not in {"all", "video", "sensor", "sidecar", "catalog", "derivative"}
                    or not 1 <= limit <= 100 or not 0 <= offset <= 100000 or len(refresh) > 80):
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            return jsonify({"error": "Filtros inválidos para os arquivos da Biblioteca."}), 400

        def read_local_files(progress):
            progress("Lendo os arquivos da Biblioteca local…")
            try:
                result = prepared_library.list_local_media(
                    root, query, provider=provider, kind=kind, limit=limit, offset=offset)
                disk_path = Path(root)
                while not disk_path.exists() and disk_path != disk_path.parent:
                    disk_path = disk_path.parent
                result["disk_free_bytes"] = shutil.disk_usage(disk_path).free
                return result, 200
            except (OSError, ValueError, RuntimeError):
                return {"error": "Não foi possível ler os arquivos da Biblioteca. Tente atualizar a lista."}, 503

        if request.args.get("async") != "1":
            result, status = read_local_files(lambda message: None)
        else:
            # File enumeration belongs to the worker, including with a cold
            # library. A nonce is idempotent while the request is being polled.
            result, status = local_media_inventory.get(
                (str(root), query, provider, kind, limit, offset, refresh),
                read_local_files, scope="local-media-library")
        return jsonify(result), status

    def original_library_paths():
        root = config.MEDIA_DATA_DIR / "ego4d"
        return root, root / "library.sqlite3"

    @app.get("/api/library/ego4d/prepared")
    def browse_prepared_library():
        root, _ = original_library_paths()
        try:
            query = request.args.get("q", "")
            state = request.args.get("state", "all")
            minimum = float(request.args.get("min_s", 0))
            raw_maximum = request.args.get("max_s")
            maximum = float(raw_maximum) if raw_maximum else None
            limit = int(request.args.get("limit", 50))
            offset = int(request.args.get("offset", 0))
            refresh = request.args.get("refresh", "")
            if (len(query) > 200 or state not in {"all", "ready", "partial", "missing", "stale"}
                    or not math.isfinite(minimum) or minimum < 0
                    or maximum is not None and (not math.isfinite(maximum) or maximum < minimum)
                    or not 1 <= limit <= 100 or not 0 <= offset <= 100000 or len(refresh) > 80):
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            return jsonify({"error": "Filtros inválidos para os recortes locais."}), 400

        def read_inventory(progress):
            progress("Conferindo os recortes e arquivos locais…")
            try:
                result = prepared_library.list_prepared_clips(
                    root, query, state=state, minimum_s=minimum, maximum_s=maximum,
                    limit=limit, offset=offset, force_refresh=bool(refresh))
                return result, 200
            except (OSError, ValueError, RuntimeError):
                return {"error": "Não foi possível conferir os recortes locais. Atualize a Biblioteca."}, 503

        if request.args.get("async") != "1":
            result, status = read_inventory(lambda message: None)
        else:
            try:
                signature = prepared_library.inventory_signature(root)
            except (OSError, ValueError, RuntimeError):
                return jsonify({"error": "Não foi possível ler a Biblioteca local."}), 503
            result, status = prepared_library_inventory.get(
                (str(root), signature, query, state, minimum, maximum, limit, offset, refresh),
                read_inventory, scope="prepared-library")
        return jsonify(result), status

    @app.get("/api/library/ego4d")
    def get_original_library_summary():
        root, index = original_library_paths()
        try:
            return jsonify(ego4d_library.library_summary(index, root))
        except (OSError, ValueError, sqlite3.Error):
            return jsonify({"state": "corrupt", "needs_index": True})

    @app.post("/api/library/ego4d/index")
    def index_original_library():
        root, index = original_library_paths()
        if not all((root / name).is_file() for name in ("ego4d.json", "clips.csv")):
            return jsonify({"error": "Prepare o catálogo Ego4D em Integrações primeiro."}), 400
        try:
            current = ego4d_library.library_summary(index, root)
            if not current.get("needs_index"):
                return jsonify(current)
        except (OSError, ValueError, sqlite3.Error):
            pass
        try:
            sources = tuple((name, (root / name).stat().st_size, (root / name).stat().st_mtime_ns)
                            for name in ("ego4d.json", "clips.csv", "timed_narrations.jsonl")
                            if (root / name).is_file())
        except OSError:
            return jsonify({"error": "Não foi possível ler o catálogo local."}), 503
        def build(progress):
            try:
                ego4d_library.index_library(root, index, progress=progress)
                return ego4d_library.library_summary(index, root), 200
            except (OSError, ValueError, sqlite3.Error):
                return {"error": "Falha ao indexar os metadados; fontes e índice anterior preservados."}, 500
        result, status = original_library_index.get((str(root), sources), build, scope="original-library")
        return jsonify(result), status

    @app.get("/api/library/ego4d/videos")
    def browse_original_library():
        root, index = original_library_paths()
        try:
            state = ego4d_library.library_summary(index, root)
            if state.get("needs_index"):
                return jsonify({"error": "Atualize o índice da biblioteca primeiro.", **state}), 409
            minimum = float(request.args.get("min_s", 0))
            maximum = request.args.get("max_s")
            imu_filter = request.args.get("imu", "all")
            if imu_filter not in {"all", "declared"}:
                raise ValueError("Filtro de sensores inválido.")
            result = ego4d_library.browse_library(index, request.args.get("q", ""), minimum_s=minimum,
                maximum_s=float(maximum) if maximum else None, imu_only=imu_filter == "declared",
                limit=int(request.args.get("limit", 50)), offset=int(request.args.get("offset", 0)))
            return jsonify(result)
        except (ValueError, sqlite3.OperationalError):
            return jsonify({"error": "Filtros inválidos ou índice indisponível; atualize a biblioteca."}), 400
        except (OSError, sqlite3.Error):
            return jsonify({"error": "Não foi possível consultar a biblioteca local."}), 503

    @app.get("/api/library/nymeria")
    def get_nymeria_library():
        try:
            result = nymeria_library.inventory(limit=1)
            result["summary"] = nymeria_library.summary()
            result["task_names"] = library_task_catalog.library_task_names()
            result["task_catalog_source"] = "local_snapshot_requires_campaign_preflight"
        except (OSError, ValueError, RuntimeError):
            return jsonify({"error": "Não foi possível ler o catálogo Nymeria."}), 503
        return jsonify({**result, "worker": nymeria_library_worker.snapshot()})

    @app.get("/api/library/nymeria/sequences")
    def browse_nymeria_library():
        try:
            query = request.args.get("q", "")
            state = request.args.get("state", "all")
            limit = int(request.args.get("limit", 50))
            offset = int(request.args.get("offset", 0))
            if len(query) > 200 or not 1 <= limit <= 100 or not 0 <= offset <= 100000:
                raise ValueError
            result = nymeria_library.inventory(query=query, state=state, limit=limit, offset=offset)
            result["summary"] = nymeria_library.summary()
            result["task_names"] = library_task_catalog.library_task_names()
            result["task_catalog_source"] = "local_snapshot_requires_campaign_preflight"
        except (OSError, ValueError, RuntimeError):
            return jsonify({"error": "Filtros inválidos ou catálogo Nymeria indisponível."}), 400
        return jsonify({**result, "worker": nymeria_library_worker.snapshot()})

    @app.get("/api/library/nymeria/operation")
    def nymeria_library_operation():
        return jsonify(nymeria_library_worker.snapshot())

    @app.post("/api/library/nymeria/stop")
    def stop_nymeria_library_operation():
        nymeria_library_worker.stop()
        return jsonify({"ok": True, "worker": nymeria_library_worker.snapshot()})

    def start_nymeria_library_work(operation, work):
        with _HEAVY_RUNNER_LOCK:
            if campaign_drain["requested"]:
                return campaign_closing_response()
            if RUNNER.running or RECOVERY.running or HOLO_CACHE_RUNNER.running or nymeria_library_worker.running:
                return jsonify({"error": "Aguarde a campanha, recuperação ou preparação atual terminar."}), 409
            if campaign_drain["requests"] or campaign_verifications.busy or nymeria_catalog.busy:
                return jsonify({"error": "Aguarde a verificação ou o planejamento terminar antes de alterar a Biblioteca."}), 409
            worker = nymeria_library_worker.start(operation, work, root=str(nymeria_library.data_root()))
        return jsonify({"ok": True, "worker": worker}), 202

    @app.post("/api/library/nymeria/import")
    def import_nymeria_library_manifest():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("path"), str) or not body["path"].strip():
            return jsonify({"error": "Escolha o JSON de URLs do Nymeria."}), 400
        path = Path(body["path"])
        try:
            valid_path = path.is_file() and path.stat().st_size <= 32 * 1024 * 1024
        except OSError:
            valid_path = False
        if not valid_path:
            return jsonify({"error": "Manifesto Nymeria ausente ou acima de 32 MiB."}), 400
        return start_nymeria_library_work("import", lambda progress, stopped:
            nymeria_library.import_manifest(path))

    @app.post("/api/library/nymeria/sync")
    def sync_nymeria_library_catalog():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body:
            return jsonify({"error": "A atualização do catálogo não aceita filtros."}), 400
        return start_nymeria_library_work("sync", lambda progress, stopped:
            nymeria_library.sync_catalog(progress=progress, should_stop=stopped))

    @app.post("/api/library/nymeria/plan")
    def plan_nymeria_library_expansion():
        with _HEAVY_RUNNER_LOCK:
            if nymeria_library_worker.running:
                return jsonify({"error": "Aguarde a preparação terminar antes de planejar outras fontes."}), 409
        body = request.get_json(silent=True)
        try:
            if not isinstance(body, dict):
                raise ValueError
            tasks = body.get("task_names")
            if (not isinstance(tasks, list) or not tasks or len(tasks) > 100
                    or any(not isinstance(name, str) or not name.strip() for name in tasks)):
                raise ValueError
            if any(isinstance(body.get(key), bool) for key in
                   ("min_dur_s", "max_dur_s", "target_seconds", "min_free_gb")):
                raise ValueError
            minimum = float(body.get("min_dur_s", 300))
            maximum = float(body.get("max_dur_s", 1800))
            target = float(body.get("target_seconds", 28800))
            reserve_gb = float(body.get("min_free_gb", 50))
            if (any(not math.isfinite(v) for v in (minimum, maximum, target, reserve_gb))
                    or not 60 <= minimum <= maximum <= 1800 or not 0 < target <= 43200
                    or not 5 <= reserve_gb <= 1000):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return jsonify({"error": "Selecione tarefas, duração entre 60 e 1800 s e meta de até 12 h."}), 400
        def plan(progress):
            progress("Procurando conteúdo Nymeria nas narrações do catálogo completo…")
            try:
                return nymeria_library.plan_expansion(tasks, min_dur_s=minimum,
                    max_dur_s=maximum, target_seconds=target, progress=progress,
                    min_free_bytes=int(reserve_gb * 1024 ** 3)), 200
            except (OSError, ValueError, RuntimeError):
                return {"error": "Não foi possível planejar o conteúdo. Atualize o catálogo Nymeria."}, 503
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with _HEAVY_RUNNER_LOCK:
            if nymeria_library_worker.running:
                return jsonify({"error": "Aguarde a preparação terminar antes de planejar outras fontes."}), 409
            generation = nymeria_library_worker.snapshot().get("run_id")
            result, status = nymeria_catalog.get((str(nymeria_library.data_root()), generation, digest), plan,
                                                scope="nymeria-expansion")
        return jsonify(result), status

    @app.post("/api/library/nymeria/download")
    def acquire_nymeria_library_sequences():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "Selecione as sequências do plano Nymeria."}), 400
        seq_ids = body.get("seq_ids")
        if (not isinstance(seq_ids, list) or not seq_ids or len(seq_ids) > 1100
                or any(not isinstance(value, str) or not value for value in seq_ids)):
            return jsonify({"error": "Selecione as sequências do plano Nymeria."}), 400
        try:
            reserve_gb = body.get("min_free_gb", 50)
            if isinstance(reserve_gb, bool):
                raise ValueError
            reserve_gb = float(reserve_gb)
            if not math.isfinite(reserve_gb) or not 5 <= reserve_gb <= 1000:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return jsonify({"error": "A reserva de espaço deve estar entre 5 e 1000 GiB."}), 400
        # IDs are checked against the imported catalog inside the worker. A
        # receipt supplied by the browser never becomes an arbitrary URL/path.
        return start_nymeria_library_work("download", lambda progress, stopped:
            nymeria_library.acquire_sequences(seq_ids, progress=progress, should_stop=stopped,
                                              min_free_bytes=int(reserve_gb * 1024 ** 3)))

    @app.get("/api/health")
    def get_health():
        """Handshake leve: nunca abrir ferramentas, ler contas ou percorrer mídia."""
        return jsonify({
            "ok": True,
            "service": {
                "app_version": os.environ.get("QMONEY_APP_VERSION", "unknown"),
            },
        })

    @app.get("/api/runtime")
    def get_runtime_readiness():
        return jsonify(readiness.runtime_readiness())

    @app.get("/api/diagnostics")
    def get_diagnostics():
        """Relatório deliberadamente sem emails, tokens, senhas ou URLs privadas."""
        try:
            ready = readiness.campaign_readiness("all")
        except Exception:  # noqa: BLE001 — diagnóstico precisa continuar sem exportar a exceção
            ready = {
                "ready": False,
                "provider": "all",
                "checks": [{
                    "name": "Diagnóstico de prontidão",
                    "status": "error",
                    "detail": "Não foi possível concluir a verificação local.",
                }],
            }
        safe_check_names = {
            "FFmpeg/FFprobe", "Navegador privado", "Contas Minute", "Catálogo HoloAssist",
            "Índices HoloAssist", "Acelerador HoloAssist", "Catálogo Ego4D", "Credenciais Ego4D",
            "Acelerador Ego4D", "Espaço livre", "Criador de contas", "Diagnóstico de prontidão",
        }
        # Details may contain exception messages, private paths or signed URLs.
        # Export structured status only; the local readiness screen keeps its guidance.
        ready = {"ready": ready.get("ready") is True, "provider": "all",
                 "details_omitted": True,
                 "checks": [{"name": check.get("name") if check.get("name") in safe_check_names else "Verificação local",
                             "status": check.get("status") if check.get("status") in {"ok", "warning", "error"} else "error"}
                            for check in ready.get("checks", []) if isinstance(check, dict)]}
        campaign_state = RUNNER.snapshot()
        return jsonify({
            "schema": 1,
            "generated_at": int(time.time()),
            "service": {
                "app_version": os.environ.get("QMONEY_APP_VERSION", "unknown"),
                "python": platform.python_version(),
                "platform": platform.platform(),
                "frozen": bool(getattr(sys, "frozen", False)),
            },
            "accounts": {"configured": len(_list_accounts())},
            "readiness": ready,
            "storage": _storage_snapshot(include_path=False),
            "runners": {
                "campaign": {
                    "state": campaign_state.get("state"),
                    "totals": campaign_state.get("totals"),
                    "has_error": bool(campaign_state.get("error")),
                },
                "balances": {"state": BALANCES_RUNNER.state},
                "holo_cache": {"state": HOLO_CACHE_RUNNER.state},
            },
            "history": {"campaign_logs": len(campaign.list_campaign_logs())},
        })

    # -- acelerador (HoloAssist ou Ego4D) --------------------------------------
    def _accelerator_provider(value: Any) -> str | None:
        provider = str(value or "holoassist").strip().lower()
        if provider not in {"holoassist", "ego4d"}:
            return None
        return provider

    def _accelerator_tasks(provider: str) -> list[str]:
        if provider == "ego4d":
            return ego_accelerator.task_names()
        return sorted(holoassist.MINUTE_TASK_TYPES)

    def _accelerator_module(provider: str):
        return ego_accelerator if provider == "ego4d" else holo_accelerator

    def _accelerator_budget_gb(provider: str, raw: Any, *, default: int | None) -> int | None:
        if provider != "ego4d":
            return None
        if raw in (None, ""):
            raw = default
        if raw in (None, ""):
            return None
        try:
            ego_accelerator.budget_bytes(raw)
            budget_gb = int(raw)
        except (TypeError, ValueError, OverflowError):
            raise ValueError
        if not 0 <= budget_gb <= ego_accelerator.MAX_BUDGET_GB:
            raise ValueError
        return budget_gb

    def _accelerator_durations(values) -> tuple[float, float]:
        raw_minimum = values.get("min_dur_s", 60)
        raw_maximum = values.get("max_dur_s", 1800)
        if isinstance(raw_minimum, bool) or isinstance(raw_maximum, bool):
            raise ValueError
        minimum, maximum = float(raw_minimum), float(raw_maximum)
        if (not math.isfinite(minimum) or not math.isfinite(maximum)
                or not 60 <= minimum <= maximum <= MAX_DUR_S):
            raise ValueError
        return minimum, maximum

    @app.get("/api/holo-cache")
    def holo_cache_status():
        provider = _accelerator_provider(request.args.get("provider"))
        if provider is None:
            return jsonify({"error": "provedor inválido (holoassist|ego4d)"}), 400
        module = _accelerator_module(provider)
        tasks = _accelerator_tasks(provider)
        task = str(request.args.get("task") or module.DEFAULT_TASK)
        if task not in tasks:
            return jsonify({"error": "tarefa inválida para o provedor"}), 400
        raw_limit = request.args.get("limit")
        try:
            limit = None if raw_limit in (None, "", "0") else int(raw_limit)
            if limit is not None and not 1 <= limit <= 1000:
                raise ValueError
            budget_gb = _accelerator_budget_gb(
                provider, request.args.get("budget_gb"),
                default=ego_accelerator.configured_budget_gb(ego_accelerator.DEFAULT_BUDGET_GB))
            min_free_gb = float(request.args.get("min_free_gb", 50))
            if not math.isfinite(min_free_gb) or not 5 <= min_free_gb <= 1000:
                raise ValueError
            min_dur_s, max_dur_s = _accelerator_durations(request.args)
        except (TypeError, ValueError):
            return jsonify({
                "error": "use cache em GB inteiros (0 para desativar), limite de 1 a 1000, reserva de 5 a 1000 GiB e duração entre 60 e 1800 s",
            }), 400
        if request.args.get("live") == "1":
            # O plano completo percorre o catálogo e verifica cada arquivo.
            # Durante a execução basta consultar o runner e o estado salvo.
            return jsonify({
                "provider": provider,
                "live": True,
                "runner": HOLO_CACHE_RUNNER.snapshot(),
                "last_run": load_json(module.state_path(), {}),
            })
        def read_cache(progress):
            progress("Conferindo conteúdo e arquivos preparados…")
            try:
                if provider == "ego4d":
                    cache = module.cache_status(task, min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                                                limit=limit, budget_gb=budget_gb,
                                                min_free_gb=min_free_gb)
                else:
                    cache = module.cache_status(task, min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                                                limit=limit)
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                cache = {
                    "provider": provider,
                    "task": task,
                    "total": 0,
                    "ready": 0,
                    "partial": 0,
                    "pending": 0,
                    "last_run": {},
                    "catalog_error": str(exc),
                }
                if budget_gb is not None:
                    try:
                        cache.update(ego_accelerator.storage_limits(
                            budget_gb, min_free_gb=min_free_gb))
                    except OSError:
                        cache.update({"budget_gb": 0, "max_budget_gb": 0})
            return {
                "provider": provider,
                "default_task": module.DEFAULT_TASK,
                "configured_budget_gb": ego_accelerator.configured_budget_gb() if provider == "ego4d" else None,
                "cache": cache,
                "runner": HOLO_CACHE_RUNNER.snapshot(),
                "tasks": tasks,
            }, 200
        runner = HOLO_CACHE_RUNNER.snapshot()
        if request.args.get("async") == "1":
            result, status = accelerator_catalog.get(
                (provider, task, min_dur_s, max_dur_s, limit, budget_gb, min_free_gb,
                 runner.get("run_id", 0), runner.get("state")), read_cache,
                scope="accelerator")
        else:
            result, status = read_cache(lambda message: None)
        # Catalog data may be cached; execution state must always be current.
        result = dict(result)
        result["runner"] = HOLO_CACHE_RUNNER.snapshot()
        if isinstance(result.get("cache"), dict):
            result["cache"] = dict(result["cache"])
            result["cache"]["last_run"] = load_json(module.state_path(), {})
        return jsonify(result), status

    @app.post("/api/holo-cache/start")
    def holo_cache_start():
        body = request.get_json(silent=True) or {}
        provider = _accelerator_provider(body.get("provider"))
        if provider is None:
            return jsonify({"error": "provedor inválido (holoassist|ego4d)"}), 400
        if provider == "holoassist" and _load_prefs().get("holoassist_enabled") is False:
            return jsonify({"error": "HoloAssist está desativado neste PC"}), 400
        module = _accelerator_module(provider)
        tasks = _accelerator_tasks(provider)
        task = str(body.get("task") or module.DEFAULT_TASK)
        if task not in tasks:
            return jsonify({"error": "tarefa inválida para o provedor"}), 400
        try:
            raw_limit = body.get("limit")
            limit = None if raw_limit in (None, "", 0, "0") else int(raw_limit)
            if limit is not None and not 1 <= limit <= 1000:
                raise ValueError
            min_free_gb = float(body.get("min_free_gb", 50))
            if not math.isfinite(min_free_gb) or not 5 <= min_free_gb <= 1000:
                raise ValueError
            budget_gb = _accelerator_budget_gb(
                provider, body.get("budget_gb"), default=ego_accelerator.DEFAULT_BUDGET_GB)
            min_dur_s, max_dur_s = _accelerator_durations(body)
        except (TypeError, ValueError):
            return jsonify({
                "error": "use limite entre 1 e 1000, reserva entre 5 e 1000 GiB, cache em GB inteiros (0 para desativar) e duração entre 60 e 1800 s",
            }), 400

        if provider == "ego4d" and budget_gb == 0:
            with _HEAVY_RUNNER_LOCK:
                if HOLO_CACHE_RUNNER.running:
                    return jsonify({
                        "error": "pare o acelerador antes de desligar o cache",
                    }), 409
                ego_accelerator.remember_budget(0)
            return jsonify({
                "ok": True,
                "cache_disabled": True,
                "runner": HOLO_CACHE_RUNNER.snapshot(),
            })

        # Reserve the runner before expensive planning, so start and stop remain
        # responsive even with a cold catalog. warm_cache validates the plan.
        with _HEAVY_RUNNER_LOCK:
            if RUNNER.running or RECOVERY.running:
                return jsonify({"error": "pare a campanha antes de iniciar o acelerador"}), 409
            if nymeria_library_worker.running:
                return jsonify({"error": "Aguarde a preparação da Biblioteca terminar."}), 409
            if HOLO_CACHE_RUNNER.running:
                return jsonify({"ok": True, "already_running": True,
                                "runner": HOLO_CACHE_RUNNER.snapshot()})
            catalog = {}
            try:
                if provider == "ego4d":
                    if not module.catalog_installed():
                        return jsonify({"error": "catálogo Ego4D ainda não está instalado"}), 400
                    catalog = module.storage_limits(budget_gb, min_free_gb=min_free_gb)
                    budget_gb = catalog["budget_gb"]
                    if budget_gb == 0:
                        return jsonify({"error": "não há espaço disponível para cache acima da reserva de disco"}), 400
                HOLO_CACHE_RUNNER.start(
                    provider=provider, task=task, min_dur_s=min_dur_s, max_dur_s=max_dur_s, limit=limit,
                    budget_gb=budget_gb, min_free_gb=min_free_gb)
            except (OSError, ValueError) as exc:
                return jsonify({"error": str(exc)}), 400
            except RuntimeError as exc:
                return jsonify({"error": str(exc)}), 409
        return jsonify({
            "ok": True,
            "provider": provider,
            "cache": catalog,
            "runner": HOLO_CACHE_RUNNER.snapshot(),
        })

    @app.post("/api/holo-cache/stop")
    def holo_cache_stop():
        HOLO_CACHE_RUNNER.stop()
        return jsonify({"ok": True, "runner": HOLO_CACHE_RUNNER.snapshot()})

    @app.post("/api/storage/cleanup")
    def storage_cleanup():
        """Limpeza manual confinada aos caches de mídia conhecidos."""
        body = request.get_json(silent=True) or {}
        provider = str(body.get("provider") or "all").strip().lower()
        if provider not in {"ego4d", "holoassist", "all"}:
            return jsonify({"error": "provedor inválido (ego4d|holoassist|all)"}), 400
        with _HEAVY_RUNNER_LOCK:
            if RUNNER.running or RECOVERY.running or HOLO_CACHE_RUNNER.running or nymeria_library_worker.running:
                return jsonify({
                    "error": "pare a campanha e o acelerador antes de limpar a mídia",
                }), 409
            try:
                result = campaign.cleanup_media_cache(config.MEDIA_DATA_DIR / "ego4d", provider=provider)
            except (ValueError,OSError):
                return jsonify({"error_code":"cleanup_state_unreadable",
                                "error":"Os registros de envio precisam ser revisados antes de limpar a mídia."}),409
        return jsonify({"ok": not result["errors"], "provider": provider, **result})

    # -- campanha ---------------------------------------------------------------
    class OriginalAdmissionError(ValueError):
        def __init__(self, code, status=400):
            self.code, self.status = code, status

    def original_error(code, status=400):
        # Neither OS/auth exceptions nor paths, metadata or provider bodies
        # are suitable public explanations for an admission failure.
        return jsonify({"ok": False, "error_code": code,
                        "error": "A captura original não foi admitida. Revise os arquivos, a conta e a verificação antes de continuar."}), status

    @app.get("/api/campaigns/starts/<start_id>")
    def campaign_start_status(start_id):
        try:
            data=campaign_start_store.public_status(start_id)
            row=campaign_start_store.lookup(start_id)
            if row is not None:
                from ..start_execution import execution_evidence
                snap=RUNNER.snapshot()
                running_id=snap.get('start_request_id') if isinstance(snap,dict) else None
                data.update(execution_evidence(row,running_id=running_id,thread_running=RUNNER.running is True))
            else:
                data.update(execution_state='review',terminal=False,log_name=None,delivery_confirmed=False)
            return jsonify(data)
        except (ValueError, OSError):
            return jsonify({"ok":False,"error_code":"start_state_unreadable",
                            "error":"Preserve o estado local e revise o registro de início."}), 409

    def original_body(*, starting=False):
        try:
            body = decode_json_state(request.get_data(cache=True))
            allowed = {"account_email", "task_id", "file_pairs", "expected_chunk_count",
                       "evaluate", "finalize", "start_request_id"}
            if starting:
                allowed.add("preflight_id")
            if set(body) - allowed:
                raise ValueError
            email = token_store.email_key(body.get("account_email"))
            task_id, start_id = body.get("task_id"), body.get("start_request_id")
            if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", task_id)
                    or not isinstance(start_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", start_id)
                    or body.get("evaluate") is not True or body.get("finalize") is not True):
                raise ValueError
            pairs = body.get("file_pairs")
            if not isinstance(pairs, list) or not 1 <= len(pairs) <= 64:
                raise ValueError
            for pair in pairs:
                if (not isinstance(pair, dict) or set(pair) != {"video_path", "sidecar_path"}
                        or any(not isinstance(value, str) or not value.strip() or len(value) > 4096
                               for value in pair.values())):
                    raise ValueError
            if "expected_chunk_count" in body and (type(body["expected_chunk_count"]) is not int
                                                    or body["expected_chunk_count"] != len(pairs)):
                raise ValueError
            result = dict(body, account_email=email)
            receipt_id = result.pop("preflight_id", None)
            if starting and (not isinstance(receipt_id, str) or not receipt_id
                             or len(receipt_id) > 160):
                raise OriginalAdmissionError("original_preflight_missing")
            return result, receipt_id
        except OriginalAdmissionError:
            raise
        except (ValueError, TypeError, token_store.TokenStoreError):
            raise OriginalAdmissionError("original_invalid_request") from None

    def original_pairs(body):
        return [(Path(pair["video_path"]), Path(pair["sidecar_path"])) for pair in body["file_pairs"]]

    def original_local_inspection(body):
        from ..original_capture import inspect_original_capture_group
        try:
            return inspect_original_capture_group(original_pairs(body), account_email=body["account_email"],
                                                  task_id=body["task_id"], expected_chunk_count=body.get("expected_chunk_count"),
                                                  now=time.time())
        except (ValueError, OSError, RuntimeError):
            raise OriginalAdmissionError("original_capture_invalid") from None

    def original_session(email):
        try:
            if email not in {token_store.email_key(row["email"]) for row in _list_accounts()}:
                raise OriginalAdmissionError("original_account_unavailable")
            session = Session.from_email(email)
            if token_store.email_key(session.email) != email:
                raise OriginalAdmissionError("original_account_unavailable")
            org_key = _resolve_org(email, session=session)
            session.ensure_auth(org_key=org_key)
            return session, org_key
        except OriginalAdmissionError:
            raise
        except (AuthError, ValueError, OSError, RuntimeError):
            raise OriginalAdmissionError("original_policy_unavailable") from None

    def original_tasks(session, org_key):
        try:
            source = session.all_tasks(org_key)
            if not isinstance(source, list) or len(source) > 4096:
                raise ValueError
            result, ids = [], set()
            for row in source:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", row["id"]):
                    raise ValueError
                if row["id"] in ids:
                    raise ValueError
                ids.add(row["id"])
                if any(key in row and not isinstance(row[key], str) for key in ("name", "description")):
                    raise ValueError
                result.append({"id": row["id"], "name": (row.get("name", "").strip() or row["id"])[:256],
                               "description": row.get("description", "")[:2000]})
            return result
        except (AuthError, ValueError, OSError, RuntimeError):
            raise OriginalAdmissionError("original_catalog_unavailable") from None

    def original_task(session, org_key, task_id):
        for task in original_tasks(session, org_key):
            if task["id"] == task_id:
                return task
        raise OriginalAdmissionError("original_task_unavailable")

    def original_policy(plan, session):
        from ..original_capture import validate_original_capture_session_policy
        from ..service_policy import RecordingPolicy
        try:
            # Shared with direct delivery: exact original CREATE bodies and
            # real policy reads, without CREATE, PUT or journal effects.
            validate_original_capture_session_policy(plan, session)
            if not isinstance(session.recording_policy, RecordingPolicy):
                raise ValueError
            return session.recording_policy.limits()
        except (AuthError, ValueError, OSError, RuntimeError):
            raise OriginalAdmissionError("original_policy_unavailable") from None

    def original_prepare(body, session, org_key):
        from ..original_capture import prepare_original_capture_plan
        from ..service_policy import RecordingPolicy
        try:
            if not isinstance(session.recording_policy, RecordingPolicy):
                raise OriginalAdmissionError("original_policy_unavailable")
            plan = prepare_original_capture_plan(original_pairs(body), account_email=body["account_email"],
                                                 org_key=org_key, task_id=body["task_id"],
                                                 expected_chunk_count=body.get("expected_chunk_count"), now=time.time(),
                                                 limits=session.recording_policy.limits())
            limits = original_policy(plan, session)
            # warmup may have changed recording-config while checking policy.
            from ..original_capture import verify_original_capture_plan
            verify_original_capture_plan(plan, account_email=body["account_email"], org_key=org_key,
                                         task_id=body["task_id"], now=time.time(), limits=limits)
            return plan, limits
        except OriginalAdmissionError:
            raise
        except (ValueError, OSError, RuntimeError):
            raise OriginalAdmissionError("original_capture_invalid") from None

    def original_summary(plan, task):
        return {"source_mode": "original", "completion_policy": "explicit_finalize", "group_count": 1,
                "chunks": len(plan.captures), "expected_chunk_count": len(plan.captures),
                "total_duration_ms": sum(plan.durations_ms), "plan_digest": plan.digest,
                "content_digest": plan.content_digest,
                "task_binding": {"task_id": task["id"], "name": task["name"], "binding": plan.task_binding},
                "owner_binding": plan.owner_binding, "org_binding": plan.org_binding,
                "completeness_binding": plan.completeness_binding,
                "source_chunk_count_verified": plan.source_chunk_count_verified,
                "physical_provenance_verified": False, "provider_acceptance_verified": False,
                "media_probe_verified": True, "csv_temporal_consistency_verified": True,
                "clock_domains_preserved": True, "cross_clock_conversion_verified": False,
                "files": [{"media": {"bytes": capture.media.bytes, "sha256": capture.media.sha256},
                           "sidecar": {"bytes": capture.sidecar.bytes, "sha256": capture.sidecar.sha256}}
                          for capture in plan.captures]}

    def original_not_busy(email):
        if RUNNER.running or RECOVERY.running or HOLO_CACHE_RUNNER.running or nymeria_library_worker.running:
            raise OriginalAdmissionError("original_busy", 409)
        try:
            items = recovery.snapshot()["items"]
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError
            if any(item.get("email") == email and item.get("blocks_campaign", True) for item in items):
                raise OriginalAdmissionError("original_recovery_pending", 409)
            return items
        except OriginalAdmissionError:
            raise
        except (ValueError, OSError, RuntimeError):
            raise OriginalAdmissionError("original_recovery_pending", 409) from None

    def original_receipt_current(receipt_id, receipt, body):
        if original_preflights.get(receipt_id) is not receipt:
            raise OriginalAdmissionError("original_preflight_missing", 409)
        if receipt["body"] != body:
            raise OriginalAdmissionError("original_request_changed", 409)
        if receipt["expires"] <= time.monotonic():
            raise OriginalAdmissionError("original_preflight_expired", 409)
        if receipt["fingerprint"] != _preflight_fingerprint([body["account_email"]]):
            raise OriginalAdmissionError("original_identity_changed", 409)

    def original_verify(receipt, body, *, limits=None):
        from ..original_capture import verify_original_capture_plan
        try:
            verify_original_capture_plan(receipt["plan"], account_email=body["account_email"],
                                         org_key=receipt["org_key"], task_id=body["task_id"], now=time.time(),
                                         limits=receipt["limits"] if limits is None else limits)
        except (ValueError, OSError, RuntimeError):
            raise OriginalAdmissionError("original_source_changed", 409) from None

    @app.get("/api/campaigns/original/tasks")
    def original_campaign_tasks():
        try:
            email = token_store.email_key(request.args.get("account_email"))
            session, org_key = original_session(email)
            return jsonify({"ok": True, "account_email": email, "tasks": original_tasks(session, org_key),
                            "physical_provenance_verified": False})
        except OriginalAdmissionError as exc:
            return original_error(exc.code, exc.status)
        except (ValueError, token_store.TokenStoreError):
            return original_error("original_invalid_request")

    @app.post("/api/campaigns/original/preflight")
    def original_campaign_preflight():
        try:
            body, _ = original_body()
            original_local_inspection(body)  # All local invariants precede auth.
            session, org_key = original_session(body["account_email"])
            task = original_task(session, org_key, body["task_id"])
            plan, limits = original_prepare(body, session, org_key)
            summary = original_summary(plan, task)
            with _HEAVY_RUNNER_LOCK:
                if campaign_drain["requested"]:
                    return campaign_closing_response()
                original_not_busy(body["account_email"])
                with preflight_lock:
                    now = time.monotonic()
                    for key in list(original_preflights):
                        if original_preflights[key]["expires"] <= now:
                            original_preflights.pop(key)
                    if len(original_preflights) >= 32 or len(original_starts) >= 256:
                        raise OriginalAdmissionError("original_busy", 409)
                    receipt_id = uuid.uuid4().hex
                    original_preflights[receipt_id] = {"body": body, "plan": plan, "task": task,
                        "org_key": org_key, "limits": limits, "summary": summary,
                        "fingerprint": _preflight_fingerprint([body["account_email"]]), "expires": now + 600}
            return jsonify({"ok": True, "preflight_id": receipt_id, "receipt": receipt_id,
                            "account_email": body["account_email"], "task_id": body["task_id"],
                            "start_request_id": body["start_request_id"], "original_summary": summary,
                            "completion_policy": "explicit_finalize", "readiness": {"ready": True}})
        except OriginalAdmissionError as exc:
            return original_error(exc.code, exc.status)

    @app.post("/api/campaigns/original")
    def start_original_campaign():
        try:
            body, receipt_id = original_body(starting=True)
            with preflight_lock:
                prior = campaign_start_store.lookup(body["start_request_id"], kind="original",
                                                    receipt_id=receipt_id, body=body)
                if prior is not None:
                    if prior["phase"] == "claimed":
                        return original_error("original_start_outcome_unknown", 500)
                    return jsonify({**prior["reply"], "already_running": True})
                receipt = original_preflights.get(receipt_id)
                if receipt is None:
                    raise OriginalAdmissionError("original_preflight_missing", 409)
                original_receipt_current(receipt_id, receipt, body)
            original_verify(receipt, body)  # Changed local files fail before auth.
            session, org_key = original_session(body["account_email"])
            if org_key != receipt["org_key"]:
                raise OriginalAdmissionError("original_identity_changed", 409)
            original_task(session, org_key, body["task_id"])
            original_policy(receipt["plan"], session)
            with _HEAVY_RUNNER_LOCK:
                if campaign_drain["requested"]:
                    return campaign_closing_response()
                items = original_not_busy(body["account_email"])
                with preflight_lock:
                    original_receipt_current(receipt_id, receipt, body)
                    # Recheck identity, organization, policy and source bytes at
                    # admission, after waiting for the shared runner barrier.
                    if (token_store.email_key(session.email) != body["account_email"]
                            or _resolve_org(body["account_email"], session=session) != org_key):
                        raise OriginalAdmissionError("original_identity_changed", 409)
                    # Availability can change while waiting; the reviewed
                    # label and plan remain frozen after this membership read.
                    original_task(session, org_key, body["task_id"])
                    limits = original_policy(receipt["plan"], session)
                    original_verify(receipt, body, limits=limits)
                    plan, task = receipt["plan"], receipt["task"]
                    cfg = CampaignConfig(accounts=[AccountSpec(body["account_email"], org_key)],
                        tasks=[TaskSpec(task_id=task["id"], scenario="original", min_dur_s=sum(plan.durations_ms)/1000,
                                        max_dur_s=sum(plan.durations_ms)/1000, task_name=task["name"],
                                        task_label=task["name"], task_description=task["description"], count=1)],
                        work_dir=config.MEDIA_DATA_DIR / "original", evaluate=True, finalize=True, account_workers=1,
                        account_max_attempts=1, shuffle_schedule=False, share_clips=False, unique_video=False,
                        allow_new_accounts=False, cleanup_after_upload=False, realistic_timeline=False,
                        dataset_provider="original", start_request_id=body["start_request_id"],
                        original_capture_plan=plan, recovery_exclusions=recovery.campaign_exclusions(items))
                    reply = {"ok": True, "already_running": False, "start_request_id": body["start_request_id"],
                             "preflight_id": receipt_id, "receipt": receipt_id, "account_email": body["account_email"],
                             "task_id": body["task_id"], "total_sends": 1, "accounts": [body["account_email"]],
                             "selected_tasks": [{"task_id": task["id"]}], "original_summary": receipt["summary"]}
                    # Claim the UUID before invoking a collaborator that may
                    # accept work and then fail to return a response.
                    # Hashing/probing and policy reads can be slow. Recheck the
                    # monotonic TTL and owner fingerprint immediately at start.
                    original_receipt_current(receipt_id, receipt, body)
                    claimed, prior = campaign_start_store.claim(body["start_request_id"], kind="original",
                        receipt_id=receipt_id, body=body,
                        bindings={"accounts":[{"email":body["account_email"],"org_key":org_key}],
                                  "tasks":[task["id"]],"session_id":plan.session_id,
                                  "plan_digest":plan.digest,"content_digest":plan.content_digest,
                                  "expected_chunk_count":len(plan.captures)},
                        protected_assets=[{"path":str(asset.path),"sha256":asset.sha256}
                                          for capture in plan.captures for asset in (capture.media,capture.sidecar)])
                    if not claimed:
                        if prior["phase"] == "claimed":
                            return original_error("original_start_outcome_unknown",500)
                        return jsonify({**prior["reply"],"already_running":True})
                    try:
                        RUNNER.start(cfg)
                    except Exception:
                        # A lost/exceptional start outcome is not proof of
                        # rejection. The caller must poll this UUID, never repost.
                        return original_error("original_start_outcome_unknown", 500)
                    original_preflights.pop(receipt_id, None)
                    campaign_start_store.acknowledge(body["start_request_id"],reply)
            return jsonify(reply)
        except campaign_start_store.StartConflictError:
            return original_error("original_start_conflict",409)
        except (campaign_start_store.StartStoreError, OperationLeaseError, OSError):
            return original_error("original_start_outcome_unknown",500)
        except OriginalAdmissionError as exc:
            return original_error(exc.code, exc.status)

    @app.post("/api/campaigns/preflight")
    def campaign_preflight():
        """Valida a operação inteira sem baixar, preparar ou enviar mídia."""
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        try:
            _validate_campaign_request(body)
        except (ValueError, TypeError, OverflowError) as exc:
            return jsonify({"error": f"parâmetros inválidos: {exc}"}), 400
        if request.args.get("async") == "1":
            request_id = request.args.get("request_id", "")
            if not re.fullmatch(r"[a-f0-9]{32}", request_id):
                return jsonify({"error": "Identificador de verificação inválido."}), 400
            body_digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            result, status = campaign_verifications.get(
                (request_id, body_digest), lambda progress: background_campaign_preflight(body, progress))
        else:
            result, status = run_campaign_preflight(body)
        return jsonify(result), status

    def background_campaign_preflight(body, progress):
        # Updates and shutdown must also wait for authentication/token writes
        # performed by the worker after its initiating HTTP request has ended.
        with _HEAVY_RUNNER_LOCK:
            if campaign_drain["requested"]:
                return {"error_code": "campaign_closing", "error": "O aplicativo está encerrando a operação."}, 409
            campaign_drain["requests"] += 1
        try:
            return run_campaign_preflight(body, progress)
        finally:
            with _HEAVY_RUNNER_LOCK:
                campaign_drain["requests"] -= 1

    def run_campaign_preflight(body, progress=lambda message: None):
        """Same admission checks for legacy callers and background verification."""
        blockers: list[str] = []
        warnings: list[str] = []
        if RUNNER.running:
            blockers.append("já existe uma campanha em andamento")
        if HOLO_CACHE_RUNNER.running:
            blockers.append("o acelerador está em execução")
        try:
            provider = _local_dataset_provider(body.get("dataset"))
            content_mode = campaign.normalize_content_mode(body.get("content_mode"))
            requested_workers = body.get("account_workers", campaign.max_account_workers())
            if (isinstance(requested_workers, bool) or not isinstance(requested_workers, int)
                    or not 1 <= requested_workers <= 15):
                raise ValueError("envios simultâneos devem estar entre 1 e 15")
            min_dur_s, max_dur_s = _parse_duration_range(body)
            count = max(1, min(int(body.get("count", 1)), 200))
            target_hours = max(0.0, min(float(body.get("target_hours") or 0), 12.0))
        except (TypeError, ValueError, OverflowError) as exc:
            return {"error": f"parâmetros inválidos: {exc}"}, 400

        emails = [str(e).strip() for e in body.get("accounts", []) if str(e).strip()]
        raw_tasks = body.get("tasks", [])
        if not emails:
            blockers.append("selecione ao menos uma conta")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            blockers.append("selecione ao menos uma categoria")
        try:
            known = {account["email"] for account in _list_accounts()}
        except banned_store.BannedStoreError:
            raise
        except ValueError:
            return {"error": "Registro de contas inválido. Restaure os dados antes de continuar."}, 400
        missing_accounts = [email for email in emails if email not in known]
        accounts: list[AccountSpec] = []
        account_errors: list[str] = []
        account_issues: list[dict[str, Any]] = []
        for email in missing_accounts:
            account_issues.append(account_issue(email, AuthError("sem token salvo")))
        if emails:
            progress("Conferindo acesso e organização das contas…")
            resolved: list[AccountSpec | None] = [None] * len(emails)
            with ThreadPoolExecutor(max_workers=max(1, min(6, len(emails)))) as pool:
                futures = {pool.submit(_resolve_org, email): i
                           for i, email in enumerate(emails) if email in known}
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        resolved[index] = AccountSpec(emails[index], future.result())
                    except (AuthError, RuntimeError, OSError) as exc:
                        account_issues.append(account_issue(emails[index], exc))
            accounts = [account for account in resolved if account is not None]

        selected: list[dict[str, Any]] = []
        unavailable: list[str] = []
        catalog_loaded = False
        if accounts and raw_tasks:
            progress("Conferindo categorias e conteúdo selecionado…")
            catalog = []
            catalog_owner = None
            for account in accounts:
                try:
                    catalog = campaign.available_tasks(
                        account.email, account.org_key,
                        min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                        include_unavailable=True, dataset_provider=provider,
                        content_mode=content_mode)
                    if not isinstance(catalog, list) or any(not isinstance(item, dict) for item in catalog):
                        raise RuntimeError("resposta de categorias inválida")
                    catalog_loaded = True
                    catalog_owner = account.email
                    break
                except (AuthError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                    issue = account_issue(account.email, exc, stage="Carregamento das categorias")
                    account_issues.append(issue)
                    catalog = []
                    # A confirmed restriction removes only this account. Its
                    # healthy peers reuse this verification; transient catalog
                    # failures still need attention rather than being hidden.
                    if issue.get("restriction_confirmed") is not True:
                        break
            by_id = {str(item.get("id")): item for item in catalog if item.get("id")}
            seen: set[str] = set()
            for raw in raw_tasks if catalog_loaded else []:
                task_id = str(raw.get("task_id", "")).strip() if isinstance(raw, dict) else ""
                item = by_id.get(task_id)
                if not task_id or item is None:
                    blockers.append("uma categoria selecionada não está mais disponível")
                    continue
                if task_id in seen:
                    continue
                seen.add(task_id)
                label = str(item.get("name_pt") or item.get("name") or task_id)
                if item.get("available_for_duration") is False:
                    unavailable.append(label)
                    continue
                selected.append(item)

            selected_ids = {str(item.get("id")) for item in selected}
            def _check_account_tasks(account: AccountSpec) -> tuple[str, set[str] | Exception]:
                try:
                    sess = Session.from_email(account.email)
                    sess.ensure_auth()
                    account_tasks = sess.all_tasks(account.org_key)
                    if not isinstance(account_tasks, list) or any(
                            not isinstance(item, dict) for item in account_tasks):
                        raise RuntimeError("resposta de categorias inválida")
                    account_ids = {
                        str(item.get("id")) for item in account_tasks
                        if item.get("id")
                    }
                except (AuthError, RuntimeError, OSError) as exc:
                    return account.email, exc
                return account.email, selected_ids - account_ids

            restricted = {item["email"] for item in account_issues
                          if item.get("restriction_confirmed") is True}
            others = ([account for account in accounts
                       if account.email != catalog_owner and account.email not in restricted]
                      if catalog_loaded else [])
            progress("Conferindo categorias nas demais contas…")
            with ThreadPoolExecutor(max_workers=max(1, min(4, len(others)))) as pool:
                checks = pool.map(_check_account_tasks, others)
                for email, result in checks:
                    if isinstance(result, Exception):
                        account_issues.append(account_issue(
                            email, result, stage="Consulta das categorias"))
                        continue
                    if result:
                        blockers.append(
                            f"{email} não possui {len(result)} categoria(s) selecionada(s)"
                        )

        if unavailable:
            warnings.append(f"{len(unavailable)} categoria(s) sem clipe na duração escolhida")
        if raw_tasks and catalog_loaded and not selected:
            blockers.append("nenhuma categoria selecionada possui clipe compatível")

        account_issues.sort(key=lambda item: (emails.index(item["email"]), item["stage"]))
        # Restrição confirmada sai da seleção na hora. Timeout, senha e rede
        # continuam pendências e não autorizam arquivo na lista de banidas.
        confirmed = [item for item in account_issues if item.get("restriction_confirmed") is True]
        removed_now: list[str] = []
        if confirmed:
            try:
                _ban_accounts(confirmed)
            except (OSError, ValueError):
                blockers.append(
                    "Não foi possível mover as contas restritas para Banidas. "
                    "A campanha não foi alterada.")
            else:
                removed_now = [str(item["email"]) for item in confirmed]
                removed_emails = set(removed_now)
                accounts = [account for account in accounts if account.email not in removed_emails]
                account_issues = [
                    item for item in account_issues if item.get("restriction_confirmed") is not True]
                listed = ", ".join(removed_now)
                warnings.append(
                    f"{listed} foi movida para Banidas e não entra nesta campanha."
                    if len(removed_now) == 1 else
                    f"{len(removed_now)} contas com restrição confirmada foram movidas "
                    f"para Banidas e não entram nesta campanha: {listed}.")
        account_errors = [issue_text(item) for item in account_issues]
        # Compatibilidade: clientes antigos leem apenas blockers. Não esconder
        # a conta nem classificar falhas de rede como credenciais inválidas.
        blockers.extend(account_errors)
        if emails and not accounts and not account_issues:
            blockers.append(
                "Nenhuma conta selecionada permanece disponível. "
                "As contas com restrição confirmada foram movidas para Banidas.")

        clip_count = sum(int(item.get("clip_count") or 0) for item in selected)
        if target_hours > 0:
            estimated_sends = 0  # The reviewed pool determines this estimate.
        else:
            estimated_sends = len(selected) * count * len(accounts)
        progress("Conferindo requisitos da instalação e pendências de envio…")
        try:
            ready = readiness.campaign_readiness(provider, content_mode=content_mode)
        except Exception:  # Os outros checks continuam sem exportar dados da exceção.
            ready = {"ready": False, "checks": [], "error": "Não foi possível verificar a prontidão local.",
                     "code": "readiness_unavailable"}
        readiness_errors = [
            item for item in ready.get("checks", [])
            if item.get("status") == "error"
        ]
        for item in readiness_errors:
            message = f"{item.get('name')}: {item.get('detail')}"
            if message not in blockers:
                blockers.append(message)
        if not ready.get("ready") and not readiness_errors and ready.get("error"):
            blockers.append(f"prontidão indisponível: {ready['error']}")
        storage = _storage_snapshot(include_path=False)
        if storage.get("free_bytes", 0) < 10 * 1024 ** 3:
            warnings.append("há menos de 10 GiB livres na unidade da biblioteca")

        removable = {i["email"] for i in account_issues if i.get("restriction_confirmed") is True}
        survivors = [a for a in accounts if a.email not in removable]
        recovery_exclusions = {}
        recovery_error = None
        try:
            unresolved = recovery.snapshot()["items"]
            survivor_emails = {account.email for account in survivors}
            selected_recovery = [item for item in unresolved if item["email"] in survivor_emails]
            unidentified = sorted({item["email"] for item in selected_recovery if item.get("blocks_campaign", True)})
            if unidentified:
                blockers.append("Pendências sem clipe identificado em: " + ", ".join(unidentified)
                                + ". Abra Pendências e recuperação para revisar essas sessões.")
            recovery_exclusions = recovery.campaign_exclusions(selected_recovery)
            if recovery_exclusions:
                warnings.append(f"{len(recovery_exclusions)} clipe(s) reservado(s) por envios anteriores. "
                                "Esses clipes não serão reenviados nem contarão como progresso novo; a campanha usará outros conteúdos.")
        except (ValueError, OSError) as exc:
            from ..recovery_errors import error_response
            failure = error_response(exc)
            recovery_error = failure["recovery_error"]
            blockers.append(failure["error"] + " Abra Recuperação de envios.")
        reusable = (bool(survivors) and bool(selected) and catalog_loaded and ready.get("ready") is True
                    and len(blockers) == len(account_errors)
                    and all(i.get("restriction_confirmed") is True for i in account_issues))
        receipt_id = None
        candidate_plan = None
        clip_review = []
        sent_fingerprint = None
        capacity_summary = None
        if reusable and (body.get("include_clip_plan") is True or target_hours > 0):
            progress("Preparando a prévia dos clipes…")
            from .. import campaign_plan
            review_tasks = [TaskSpec(
                task_id=str(item["id"]), scenario=str(item["scenario"]),
                task_name=str(item.get("name") or item["scenario"]),
                task_label=str(item.get("name_pt") or item.get("name") or item["scenario"]),
                min_dur_s=min_dur_s, max_dur_s=max_dur_s, count=count,
            ) for item in selected]
            try:
                candidate_plan, clip_review, sent_fingerprint = campaign_plan.build(CampaignConfig(
                    accounts=survivors, tasks=review_tasks, dataset_provider=provider,
                    content_mode=content_mode, recovery_exclusions=recovery_exclusions))
                clip_count = len(clip_review)
                capacity_summary = campaign_plan.capacity(
                    clip_review, [a.email for a in survivors], target_seconds=target_hours * 3600,
                    count_per_task=None if target_hours > 0 else count)
                estimated_sends = capacity_summary["estimated_sends"]
                refined_count = sum(bool(row.get("imu_refined_from")) for row in clip_review)
                if refined_count:
                    warnings.append(f"{refined_count} trecho(s) recortado(s) para evitar lacunas nos sensores. "
                                    "A prévia já usa essas durações; partes de vídeos já recebidos continuam excluídas.")
                if target_hours > 0:
                    if not capacity_summary["can_reach_goal"]:
                        blockers.append(campaign_plan.capacity_error(capacity_summary))
                        reusable = False
                if not any(row["eligible_accounts"] for row in clip_review):
                    blockers.append("Nenhum clipe candidato está disponível para as contas selecionadas. Revise o conteúdo e a lista de vídeos usados.")
                    reusable = False
            except Exception:
                blockers.append("Não foi possível preparar a prévia dos clipes. Recarregue o catálogo e tente novamente.")
                reusable = False
        if reusable:
            with preflight_lock:
                now = time.monotonic()
                for key in list(preflights):
                    if preflights[key]["expires"] <= now:
                        preflights.pop(key)
                if len(preflights) >= 32:
                    preflights.pop(next(iter(preflights)))
                receipt_id = uuid.uuid4().hex
                preflights[receipt_id] = {"body": body, "accounts": survivors, "catalog": catalog,
                                          "candidate_plan": candidate_plan, "sent_fingerprint": sent_fingerprint,
                                          "issues": account_issues, "expires": now + 600,
                                          "fingerprint": _preflight_fingerprint(emails)}

        return {
            "ok": not blockers,
            "preflight_id": receipt_id,
            "clip_plan": clip_review,
            "can_remove_and_continue": reusable and bool(removable),
            "removable_accounts": sorted(removable),
            "removed_accounts": removed_now,
            "provider": provider,
            "accounts": {"selected": len(emails), "validated": len(accounts)},
            "tasks": {"selected": len(raw_tasks), "compatible": len(selected)},
            "clips": clip_count,
            "estimated_sends": estimated_sends,
            "account_workers": campaign.clamp_account_workers(requested_workers, len(accounts)),
            "target_hours": target_hours,
            "capacity": capacity_summary,
            "blockers": blockers,
            "recovery_error": recovery_error,
            "warnings": warnings,
            "account_errors": account_errors,
            "account_issues": account_issues,
            "readiness": ready,
            "storage": storage,
        }, 200

    @app.post("/api/campaigns")
    def start_campaign():
        # Legacy callers have no durable correlation key, but a broken
        # authoritative ledger still cannot authorize a fresh operation.
        try:
            campaign_start_store.lookup('local-state-integrity-check')
        except (ValueError,OSError):
            return jsonify({"error_code":"start_state_unreadable","error":"Preserve o registro local de início para revisão."}),409
        retry_body = request.get_json(silent=True)
        if isinstance(retry_body, dict) and retry_body.get("preflight_id"):
            retry_id = retry_body["preflight_id"]
            frozen_request = {k:v for k,v in retry_body.items() if k not in {"preflight_id", "remove_restricted"}}
            try:
                prior = campaign_start_store.lookup(retry_id, kind="dataset", receipt_id=retry_id, body=frozen_request)
            except campaign_start_store.StartConflictError:
                return jsonify({"error_code":"start_request_conflict","error":"O identificador de início pertence a outra operação."}),409
            except (ValueError,OSError):
                return jsonify({"error_code":"start_state_unreadable","error":"Preserve o registro local de início para revisão."}),409
            if prior is not None:
                if prior["phase"] == "claimed":
                    return jsonify({"error_code":"start_outcome_unknown","error":"O início exige consulta e revisão; não reenvie a operação."}),500
                return jsonify({**prior["reply"],"already_running":True})
        if RUNNER.running:
            return jsonify({
                "ok": True,
                "already_running": True,
                "state": RUNNER.state,
                "total_sends": RUNNER.total_sends,
            })
        if HOLO_CACHE_RUNNER.running:
            return jsonify({
                "error": "pare o acelerador antes de iniciar a campanha",
            }), 409
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        try:
            _validate_campaign_request(body)
        except (ValueError, TypeError, OverflowError) as exc:
            return jsonify({"error": f"parâmetros inválidos: {exc}"}), 400
        receipt = None
        receipt_id = body.get("preflight_id")
        if receipt_id:
            with preflight_lock:
                receipt = preflights.get(str(receipt_id))
            original = {k: v for k, v in body.items() if k not in {"preflight_id", "remove_restricted"}}
            invalid = None
            if receipt is None:
                invalid = ("preflight_missing", "A prévia não está mais disponível. Revise a campanha novamente.")
            elif receipt["expires"] <= time.monotonic():
                invalid = ("preflight_expired", "A prévia passou de 10 minutos. Atualize e revise antes de iniciar.")
            elif original != receipt["body"]:
                invalid = ("preflight_request_changed", "Os parâmetros da campanha mudaram. Revise a nova prévia.")
            elif receipt["fingerprint"] != _preflight_fingerprint(original.get("accounts", [])):
                invalid = ("preflight_accounts_changed", "O acesso ou a identidade de uma conta mudou. Revise a nova verificação.")
            if invalid:
                return jsonify({"error_code": invalid[0], "error": invalid[1]}), 409
            if receipt["issues"] and body.get("remove_restricted") is not True:
                return jsonify({"error": "Confirme a remoção das contas com restrição para continuar."}), 400
        elif body.get("remove_restricted"):
            return jsonify({"error": "Execute o preflight antes de remover contas e continuar."}), 400
        cleanup_after_upload = body.get("cleanup_after_upload", True)
        if not isinstance(cleanup_after_upload, bool):
            return jsonify({
                "error": "cleanup_after_upload deve ser true ou false",
            }), 400
        try:
            dataset_provider = _local_dataset_provider(
                body.get("dataset")
            )
            content_mode = campaign.normalize_content_mode(body.get("content_mode"))
            requested_workers = body.get("account_workers", campaign.max_account_workers())
            if (isinstance(requested_workers, bool) or not isinstance(requested_workers, int)
                    or not 1 <= requested_workers <= 15):
                raise ValueError("envios simultâneos devem estar entre 1 e 15")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        emails = [str(e).strip() for e in body.get("accounts", []) if str(e).strip()]
        if receipt:
            emails = [a.email for a in receipt["accounts"]]
        raw_tasks = body.get("tasks", [])
        if not emails:
            return jsonify({"error": "selecione ao menos uma conta"}), 400
        if not raw_tasks:
            return jsonify({"error": "selecione ao menos uma categoria"}), 400
        try:
            count = max(1, min(int(body.get("count", 1)), 200))
            if "target_hours" in body:
                target_hours = max(0.0, min(float(body.get("target_hours") or 0), 12.0))
            else:
                target_hours = 0.0
            min_dur_s, max_dur_s = _parse_duration_range(body)
            raw_delay_s = float(body.get("delay_s", 0))
            # Campo antigo ainda é validado para clientes desatualizados.
            raw_account_gap_s = float(body.get("account_gap_s", 300))
            if not all(math.isfinite(v) for v in (
                    raw_delay_s, raw_account_gap_s, target_hours)):
                raise ValueError("valor não finito")
            delay_s = max(0.0, min(raw_delay_s, 3600.0))
        except (TypeError, ValueError, OverflowError) as exc:
            return jsonify({"error": f"parâmetros numéricos inválidos: {exc}"}), 400
        delay_mode = str(body.get("delay_mode", "off"))
        if delay_mode not in ("off", "clip", "fixed"):
            return jsonify({"error": "delay_mode inválido (off|clip|fixed)"}), 400
        active_hours = _parse_active_hours(body.get("active_hours"))
        if active_hours is False:
            return jsonify({"error": "active_hours inválido — use [início, fim] "
                            "com 0 <= início < fim <= 24"}), 400

        try:
            known = {a["email"] for a in _list_accounts()}
        except ValueError:
            return jsonify({"error": "Registro de contas banidas inválido. A campanha não iniciou."}), 500
        missing = [e for e in emails if e not in known]
        if missing:
            return jsonify({"error": "conta(s) sem token: " + ", ".join(missing)}), 400

        # O POST público continua seguro mesmo se um cliente antigo pular o
        # preflight. Sem ferramentas, índices ou credenciais da origem, iniciar
        # uma thread apenas produziria uma falha tardia depois do download.
        # Recheck mutable local prerequisites even with an unexpired receipt.
        # The reviewed server-owned catalog and candidate plan stay frozen.
        try:
            environment = readiness.campaign_readiness(
                dataset_provider, content_mode=content_mode)
        except (ValueError, OSError, RuntimeError) as exc:
            return jsonify({"error": f"não foi possível validar a prontidão: {exc}"}), 400
        if not isinstance(environment, dict) or environment.get("ready") is not True:
            return jsonify({"error": "ambiente não está pronto; execute o preflight novamente."}), 400
        environment_errors = [
            item for item in environment.get("checks", [])
            if item.get("status") == "error"
        ]
        if environment_errors:
            details = "; ".join(
                f"{item.get('name')}: {item.get('detail')}"
                for item in environment_errors
            )
            return jsonify({"error": "ambiente não está pronto: " + details}), 400
        if receipt:
            accounts = receipt["accounts"]
            available = receipt["catalog"]
            skipped = []
        else:
            resolved: list[AccountSpec | None] = [None] * len(emails)
            skipped: list[str] = []
            workers = max(1, min(8, len(emails)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(_resolve_org, email): i
                        for i, email in enumerate(emails)}
                for fut in as_completed(futs):
                    i = futs[fut]
                    try:
                        resolved[i] = AccountSpec(emails[i], fut.result())
                    except (AuthError, RuntimeError, OSError) as exc:
                        skipped.append(f"{emails[i]}: {exc}")
            accounts = [acc for acc in resolved if acc is not None]
            if skipped:
                return jsonify({
                    "error": "campanha bloqueada: não foi possível validar o acesso "
                             "e a organização de todas as contas. " + "; ".join(skipped),
                }), 400

            # Nunca aceite do browser a associação task_id -> cenário. Uma aba
            # antiga ou uma troca rápida de conta podia mandar um par inconsistente
            # e selecionar vídeos de outra categoria. O catálogo atual da conta é a
            # fonte de verdade.
            try:
                available = campaign.available_tasks(
                    accounts[0].email, accounts[0].org_key,
                    min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                    include_unavailable=True, dataset_provider=dataset_provider,
                    content_mode=content_mode)
                if not isinstance(available, list) or any(not isinstance(item, dict) for item in available):
                    raise RuntimeError("resposta de categorias inválida")
            except json.JSONDecodeError:
                return jsonify({
                    "error": "a API devolveu resposta vazia (não-JSON). Tente de novo.",
                }), 400
            except (AuthError, RuntimeError, OSError) as exc:
                msg = str(exc)
                if "Expecting value" in msg:
                    msg = "a API devolveu resposta vazia (não-JSON). Tente de novo."
                return jsonify({"error": msg}), 400
        by_id = {str(t.get("id")): t for t in available if t.get("id")}

        tasks: list[TaskSpec] = []
        seen_ids: set[str] = set()
        for t in raw_tasks:
            if not isinstance(t, dict):
                return jsonify({"error": "categoria inválida"}), 400
            task_id = str(t.get("task_id", "")).strip()
            authoritative = by_id.get(task_id)
            if not task_id or authoritative is None:
                return jsonify({"error": f"categoria indisponível: {task_id or '(sem id)'}"}), 400
            if task_id in seen_ids:
                continue
            seen_ids.add(task_id)
            # Sem footage compatível (higiene / duração): ignora, não aborta o lote.
            if authoritative.get("available_for_duration") is False:
                skipped.append(str(authoritative.get("name_pt")
                                   or authoritative.get("name")
                                   or task_id))
                continue
            scenario = str(authoritative["scenario"])
            task_name = str(authoritative.get("name") or scenario)
            task_label = str(authoritative.get("name_pt") or task_name)
            task_description = str(authoritative.get("description") or "")
            tasks.append(TaskSpec(task_id=task_id, scenario=scenario,
                                  min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                                  task_name=task_name, task_label=task_label,
                                  task_description=task_description,
                                  count=count))

        if not tasks:
            return jsonify({
                "error": "nenhuma categoria selecionada tem clipe compatível "
                         "(sentado/celular/título). " + (
                             ("Fora: " + ", ".join(skipped)) if skipped else ""),
            }), 400

        # Preflight estrito nas DEMAIS contas. A primeira já foi autenticada e
        # forneceu o catálogo autoritativo acima; todas as outras precisam ter
        # as mesmas tasks antes de qualquer download/upload começar.
        selected_ids = {task.task_id for task in tasks}

        def _preflight(account: AccountSpec) -> tuple[str, set[str] | str]:
            try:
                sess = Session.from_email(account.email)
                sess.ensure_auth()
                account_tasks = sess.all_tasks(account.org_key)
                if not isinstance(account_tasks, list) or any(
                        not isinstance(task, dict) for task in account_tasks):
                    raise RuntimeError("resposta de categorias inválida")
                account_task_ids = {
                    str(task.get("id")) for task in account_tasks
                    if task.get("id")
                }
            except (AuthError, RuntimeError, OSError) as exc:
                return account.email, f"preflight falhou para {account.email}: {exc}"
            missing_tasks = selected_ids - account_task_ids
            return account.email, missing_tasks

        others = [] if receipt else accounts[1:]
        if others:
            by_email = {acc.email: acc for acc in accounts}
            viable = [accounts[0]]
            workers = max(1, min(8, len(others)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for email, result in pool.map(_preflight, others):
                    if isinstance(result, str):
                        return jsonify({
                            "error": result + ". A campanha não foi iniciada.",
                        }), 400
                    if result:
                        labels = [task.task_label or task.task_name or task.task_id
                                  for task in tasks if task.task_id in result]
                        return jsonify({
                            "error": (f"{email} não possui as categorias "
                                      f"selecionadas: {', '.join(labels)}. "
                                      "A campanha não foi iniciada.")
                        }), 400
                    viable.append(by_email[email])
            accounts = viable
            if not accounts:
                return jsonify({
                    "error": "nenhuma conta passou no preflight. "
                             + "; ".join(skipped),
                }), 400

        # O MP4 é normalizado uma vez antes do lote; o usuário controla
        # quantos uploads compartilham o link de saída.
        account_workers = campaign.clamp_account_workers(requested_workers, len(accounts))

        cfg = CampaignConfig(accounts=accounts, tasks=tasks,
                             work_dir=config.MEDIA_DATA_DIR / "ego4d",
                             timeout_blob=campaign.DEFAULT_TIMEOUT_BLOB,
                             evaluate=True, finalize=True,
                             delay_mode=delay_mode, delay_s=delay_s,
                             account_gap_s=campaign.DEFAULT_ACCOUNT_STAGGER_S,
                             account_workers=account_workers,
                             account_max_attempts=5,
                             account_retry_s=15,
                             require_all_accounts=False,
                             share_clips=True,
                             unique_video=False,
                             allow_new_accounts=False,
                             target_hours_per_account=target_hours,
                             dataset_provider=dataset_provider,
                             content_mode=content_mode,
                             cleanup_after_upload=cleanup_after_upload,
                             realistic_timeline=True,
                             start_request_id=str(receipt_id) if receipt else None,
                             candidate_plan=receipt.get("candidate_plan") if receipt else None,
                             active_hours=active_hours)
        with _HEAVY_RUNNER_LOCK:
            if campaign_drain["requested"]:
                return campaign_closing_response()
            if receipt and receipt.get("sent_fingerprint") is not None:
                from .. import campaign_plan
                if receipt["sent_fingerprint"] != campaign_plan.registry_fingerprint():
                    return jsonify({"error_code": "preflight_history_changed", "error": "A lista de vídeos usados mudou. Revise a campanha novamente."}), 409
            if RECOVERY.running:
                return jsonify({"error": "Aguarde a recuperação dos envios terminar."}), 409
            if nymeria_library_worker.running:
                return jsonify({"error": "Aguarde a preparação da Biblioteca terminar."}), 409
            try:
                unresolved = recovery.snapshot()["items"]
            except (ValueError, OSError):
                return jsonify({"error": "Revise os registros de recuperação antes de iniciar outra campanha."}), 409
            affected = sorted({item["email"] for item in unresolved if item.get("blocks_campaign", True)}
                              & {account.email for account in accounts})
            if affected:
                return jsonify({"error": "Há sessões sem clipe identificado. Revise Pendências e recuperação para estas contas: " + ", ".join(affected),
                                "error_code": "recovery_unidentified", "recovery_accounts": affected}), 409
            cfg.recovery_exclusions = recovery.campaign_exclusions(unresolved)
            if HOLO_CACHE_RUNNER.running:
                return jsonify({
                    "error": "pare o acelerador antes de iniciar a campanha",
                }), 409
            capacity_summary = None
            if target_hours > 0:
                # Re-read history/reservations even for a frozen review. This
                # admission happens before claiming a start or writing a new
                # campaign/journal; legacy callers cannot bypass it.
                from .. import campaign_plan
                try:
                    candidate_plan, clip_review, _ = campaign_plan.build(cfg)
                    capacity_summary = campaign_plan.capacity(
                        clip_review, [a.email for a in accounts], target_seconds=target_hours * 3600)
                except (ValueError, OSError, RuntimeError):
                    return jsonify({"error_code": "campaign_capacity_unavailable",
                                    "error": "Não foi possível verificar o conteúdo novo para a meta. Revise a campanha novamente.",
                                    "capacity": None}), 409
                if not capacity_summary["can_reach_goal"]:
                    return jsonify({"error_code": "campaign_capacity_insufficient",
                                    "error": campaign_plan.capacity_error(capacity_summary),
                                    "capacity": capacity_summary}), 409
                if cfg.candidate_plan is None:
                    cfg.candidate_plan = candidate_plan
            # Readiness and recovery may take time. Validate the same reviewed
            # operation at admission, before its first local effect. Keep its
            # store entry protected until successful start consumes it.
            with preflight_lock:
                if receipt:
                    if preflights.get(str(receipt_id)) is not receipt:
                        return jsonify({"error_code": "preflight_missing", "error": "A prévia não está mais disponível. Revise a campanha novamente."}), 409
                    if receipt["fingerprint"] != _preflight_fingerprint(receipt["body"].get("accounts", [])):
                        return jsonify({"error_code": "preflight_accounts_changed", "error": "O acesso ou a identidade de uma conta mudou. Revise a nova verificação."}), 409
                    if receipt["expires"] <= time.monotonic():
                        return jsonify({"error_code": "preflight_expired", "error": "A prévia passou de 10 minutos. Atualize e revise antes de iniciar."}), 409
                if receipt and receipt["issues"]:
                    if BALANCES_RUNNER.running:
                        return jsonify({"error": "Aguarde a consulta de saldos terminar."}), 409
                    try:
                        _ban_accounts(receipt["issues"])
                    except (OSError, ValueError) as exc:
                        return jsonify({"error": "Não foi possível registrar e remover as contas. A campanha não iniciou."}), 500
                try:
                    if receipt_id:
                        claimed,prior = campaign_start_store.claim(str(receipt_id),kind="dataset",receipt_id=str(receipt_id),
                            body=receipt["body"], bindings={"accounts":[{"email":a.email,"org_key":a.org_key} for a in accounts],
                                                          "tasks":[t.task_id for t in tasks]})
                        if not claimed:
                            if prior["phase"] == "claimed":
                                return jsonify({"error_code":"start_outcome_unknown","error":"O início exige consulta e revisão; não reenvie a operação."}),500
                            return jsonify({**prior["reply"],"already_running":True})
                    RUNNER.start(cfg)
                    if receipt_id:
                        preflights.pop(str(receipt_id), None)
                except RuntimeError as exc:
                    if receipt_id:
                        return jsonify({"error_code":"start_outcome_unknown","error":"O início exige consulta e revisão; não reenvie a operação."}),500
                    # Existing running-operation retries never start again.
                    if RUNNER.running:
                        return jsonify({
                            "ok": True,
                            "already_running": True,
                            "state": RUNNER.state,
                            "total_sends": RUNNER.total_sends,
                        })
                    return jsonify({"error": str(exc)}), 409
                except (campaign_start_store.StartStoreError,OperationLeaseError,OSError):
                    return jsonify({"error_code":"start_state_unreadable","error":"Preserve o registro local de início para revisão."}),409
                except Exception:
                    if receipt_id:
                        return jsonify({"error_code":"start_outcome_unknown","error":"O início exige consulta e revisão; não reenvie a operação."}),500
                    raise
        payload = {
            "ok": True,
            "total_sends": RUNNER.total_sends,
            "selected_tasks": [
                {"task_id": t.task_id, "scenario": t.scenario} for t in tasks
            ],
            "accounts": [acc.email for acc in accounts],
            "removed_accounts": sorted({i["email"] for i in receipt["issues"]}) if receipt else [],
            "dataset": dataset_provider,
        }
        if skipped:
            payload["skipped_accounts"] = skipped
        if receipt_id:
            payload["start_request_id"] = str(receipt_id)
            payload["preflight_id"] = str(receipt_id)
            try:
                campaign_start_store.acknowledge(str(receipt_id),payload)
            except (ValueError,OSError):
                return jsonify({"error_code":"start_outcome_unknown","error":"O início exige consulta e revisão; não reenvie a operação."}),500
        return jsonify(payload)

    @app.get("/api/campaigns/current")
    def campaign_current():
        try:
            since = int(request.args.get("since", 0))
        except ValueError:
            since = 0
        return jsonify(RUNNER.snapshot(since=since))

    @app.post("/api/campaigns/pause")
    def pause_campaign():
        try:
            RUNNER.pause()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"ok": True, "pause_requested": True})

    @app.get("/api/recovery")
    def recovery_snapshot():
        from .. import recovery
        if request.args.get("async") == "1":
            worker = RECOVERY.snapshot()
            def read(progress):
                progress("Lendo os registros de recuperação desta instalação…")
                try:
                    return recovery.snapshot(), 200
                except (ValueError, OSError) as exc:
                    from ..recovery_errors import error_response
                    return error_response(exc), 409
            result, status = recovery_catalog.get((worker.get("state"), worker.get("email")), read,
                                                  refresh=request.args.get("refresh") == "1")
            return jsonify({**result, "wise_cleanup": _wise_cleanup_snapshot(), "worker": worker}), status
        try:
            return jsonify({**recovery.snapshot(), "wise_cleanup": _wise_cleanup_snapshot(), "worker": RECOVERY.snapshot()})
        except (ValueError, OSError) as exc:
            from ..recovery_errors import error_response
            return jsonify(error_response(exc)), 409

    @app.post("/api/recovery/reconcile")
    def reconcile_recovery():
        from .. import recovery
        with _HEAVY_RUNNER_LOCK:
            if campaign_drain["requested"]:
                return campaign_closing_response()
            if RUNNER.running or RECOVERY.running:
                return jsonify({"error": "Aguarde a campanha encerrar antes de reconciliar seus registros."}), 409
            try:
                return jsonify(recovery.reconcile_confirmed())
            except (ValueError, OSError):
                return jsonify({"error": "A reconciliação não foi concluída. Preserve os registros e tente novamente."}), 409

    @app.post("/api/recovery/resume")
    def resume_recovery():
        body = request.get_json(silent=True) or {}
        email = body.get("email")
        if body.get("confirmed") is not True or not isinstance(email, str) or not email:
            return jsonify({"error": "Confirme a conta e a retomada dos envios existentes."}), 400
        with _HEAVY_RUNNER_LOCK:
            if campaign_drain["requested"]:
                return campaign_closing_response()
            if RUNNER.running or RECOVERY.running or HOLO_CACHE_RUNNER.running or nymeria_library_worker.running:
                return jsonify({"error": "Aguarde a operação em andamento terminar."}), 409
            try:
                if email not in {account["email"] for account in _list_accounts()}:
                    return jsonify({"error": "Conecte novamente essa conta antes de retomar."}), 400
                if not any(item["email"] == email and item["can_resume"] for item in recovery.snapshot()["items"]):
                    return jsonify({"error": "Nenhuma sessão desta conta permite retomada automática; revise o histórico."}), 409
                RECOVERY.start(email, _resolve_org)
            except (ValueError, OSError, RuntimeError):
                return jsonify({"error": "Não foi possível iniciar a recuperação. Os registros foram preservados."}), 409
        return jsonify({"worker": RECOVERY.snapshot()}), 202

    @app.post("/api/campaigns/resume")
    def resume_campaign():
        try:
            RUNNER.resume()
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"ok": True, "pause_requested": False})

    @app.post("/api/campaigns/stop")
    def campaign_stop():
        RUNNER.stop()
        return jsonify({"ok": True, "state": RUNNER.state})

    @app.post("/api/campaigns/drain")
    def campaign_close_drain():
        # The HTTP acknowledgement is not a worker-completion receipt. Keep
        # the owned service alive until engine cleanup and local commits return.
        with _HEAVY_RUNNER_LOCK:
            campaign_drain["requested"] = True
            RUNNER.stop()
            nymeria_library_worker.stop()
            recovery_thread = getattr(RECOVERY, "_thread", None)
            recovery_active = (RECOVERY.running
                               or (recovery_thread is not None and recovery_thread.is_alive()))
            ready = (not RUNNER.running and not recovery_active and not nymeria_library_worker.running
                     and campaign_drain["requests"] == 0)
            return jsonify({"ok": True, "draining": True, "ready": bool(ready)})

    # -- histórico ----------------------------------------------------------------
    @app.get("/api/logs")
    def get_logs():
        out: list[dict[str, Any]] = []
        for path in campaign.list_campaign_logs():
            try:
                data = _read_campaign_log(path)
            except (OSError, ValueError):
                continue
            items = data.get("items", [])
            sends = [acc for it in items for acc in it.get("accounts", [])
                     if not acc.get("skipped")]
            out.append({
                "name": path.name,
                "started_at": data.get("started_at"),
                "accounts": data.get("accounts", []),
                "items": len(items),
                "sends": len(sends),
                "ok": sum(1 for s in sends if s.get("ok") is True and s.get("finalized") is True),
            })
        return jsonify({"logs": out})

    @app.get("/api/logs/<name>")
    def get_log(name: str):
        path = _log_path(name)
        if path is None:
            return jsonify({"error": "log não encontrado"}), 404
        try:
            raw = _read_campaign_log(path)
            return jsonify(_campaign_log_view(raw, history_name=path.name))
        except (OSError, ValueError):
            return jsonify({"error": "log ilegível (JSON vazio/corrompido)"}), 400

    @app.post("/api/logs/<name>/status")
    def get_log_status(name: str):
        path = _log_path(name)
        if path is None:
            return jsonify({"error": "log não encontrado"}), 404
        try:
            data = _read_campaign_log(path)
        except (OSError, ValueError):
            return jsonify({"error": "log ilegível (JSON vazio/corrompido)"}), 400
        entries: list[tuple[str, str, str, int]] = []
        for it in data.get("items", []):
            for acc in it.get("accounts", []):
                sid = acc.get("session_id")
                if not sid:
                    continue
                uploads = acc.get("uploads")
                expected_files = len(uploads) if isinstance(uploads, list) else 1
                expected_files = max(1, expected_files)
                org = acc.get("org_key") or _safe_org(acc.get("email", ""))
                if not org:
                    entries.append((str(acc.get("email") or ""), "", str(sid),
                                    expected_files))
                    continue
                entries.append((str(acc.get("email") or ""), str(org), str(sid),
                                expected_files))

        # Uma sessão autenticada por conta evita renovar o mesmo refresh token
        # para cada vídeo. Contas diferentes são consultadas em paralelo; as
        # sessões da mesma conta continuam seriais para não disputar tokens.
        grouped: dict[str, list[tuple[str, str, int]]] = {}
        for email, org, sid, expected_files in entries:
            grouped.setdefault(email, []).append((org, sid, expected_files))

        def _check_account(
            group: tuple[str, list[tuple[str, str, int]]],
        ) -> list[dict[str, Any]]:
            email, account_entries = group
            output: list[dict[str, Any]] = []
            valid = [(org, sid, expected) for org, sid, expected in account_entries
                     if org]
            invalid = [(org, sid, expected) for org, sid, expected in account_entries
                       if not org]
            output.extend({"session_id": sid, "email": email,
                           "status": "sem org_key", "expected_files": expected}
                          for _, sid, expected in invalid)
            if not valid:
                return output
            try:
                sess = Session.from_email(email)
            except (AuthError, RuntimeError, OSError) as exc:
                issue = account_issue(email, exc, stage="Consulta de sessões Minute")
                return output + [
                    {"session_id": sid, "email": email,
                     "status": "erro: acesso indisponível", "code": "session_access_unavailable",
                     "issue": issue, "expected_files": expected}
                    for _, sid, expected in valid]
            for org, sid, expected in valid:
                try:
                    result = campaign.session_result(
                        email, org, sid, session=sess)
                    result["expected_files"] = expected
                    output.append(result)
                except (AuthError, RuntimeError, OSError, json.JSONDecodeError) as exc:
                    issue = account_issue(email, exc, stage="Consulta de sessões Minute")
                    output.append({"session_id": sid, "email": email,
                                   "status": "erro: consulta indisponível", "code": "session_status_unavailable",
                                   "issue": issue,
                                   "expected_files": expected})
            return output

        results: list[dict[str, Any]] = []
        groups = list(grouped.items())
        if groups:
            workers = min(6, len(groups))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for account_results in pool.map(_check_account, groups):
                    results.extend(account_results)
        session_ready = sum(item.get("status") == "preview_ready" for item in results)
        session_pending = sum(item.get("status") == "processing" for item in results)
        session_unavailable = sum(
            str(item.get("status") or "").startswith("unprocessed:unavailable")
            for item in results)
        session_errors = (len(results) - session_ready - session_pending
                          - session_unavailable)

        # A interface fala em prévias, portanto o progresso precisa contar os
        # arquivos/chunks reais, não apenas as sessões. Uma sessão longa pode
        # conter vários MP4s e pode estar parcialmente processada.
        ready = pending = unavailable = total = errors = transient_errors = 0
        for item in results:
            expected = max(1, int(item.get("expected_files") or 1))
            reported = max(0, int(item.get("total_files") or 0))
            status = str(item.get("status") or "")
            if status.startswith("erro:"):
                total += expected
                errors += expected
                transient_errors += expected
            elif status == "sem org_key":
                total += expected
                errors += expected
            else:
                item_total = max(expected, reported)
                item_ready = max(0, int(item.get("ready_files") or 0))
                item_pending = max(0, int(item.get("pending_files") or 0))
                item_unavailable = max(
                    0, int(item.get("unavailable_files") or 0))
                missing = max(
                    0, item_total - item_ready - item_pending - item_unavailable)
                total += item_total
                ready += item_ready
                pending += item_pending
                unavailable += item_unavailable
                if status == "processing":
                    pending += missing
                else:
                    errors += missing
        return jsonify({
            "results": results,
            "attempt_status": data.get("status"),
            "delivery_summary": (delivery := _campaign_log_view(data, history_name=path.name)["delivery_summary"]),
            "all_deliveries_confirmed": bool(sum(delivery.values())) and delivery["confirmed"] == sum(delivery.values()),
            "summary": {
                "total": total, "ready": ready, "pending": pending,
                "unavailable": unavailable, "errors": errors,
                "transient_errors": transient_errors,
            },
            "sessions": {
                "total": len(results), "ready": session_ready,
                "pending": session_pending, "unavailable": session_unavailable,
                "errors": session_errors,
            },
        })

    # -- limpeza revisada de e-mail (apenas INBOX -> lixeira) -------------------
    @app.get("/api/mail-cleanup")
    def mail_cleanup_status():
        from ..mail_cleanup import CLEANUP
        result = CLEANUP.snapshot()
        result["profiles"] = [{"id": p["id"], "name": p.get("name") or "Caixa Hostinger"}
                              for p in hostinger_mail.configured_connections() if p.get("mailbox_id")]
        return jsonify(result)

    @app.post("/api/mail-cleanup/preview")
    def mail_cleanup_preview():
        from ..mail_cleanup import CLEANUP, CleanupError
        body = request.get_json(silent=True) or {}
        profiles = [p for p in hostinger_mail.configured_connections()
                    if p["id"] == body.get("profile_id") and p.get("mailbox_id")]
        if len(profiles) != 1:
            return jsonify({"error": "Selecione uma caixa Hostinger configurada."}), 400
        try:
            return jsonify(CLEANUP.start(profiles[0])), 202
        except CleanupError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/mail-cleanup/apply")
    def mail_cleanup_apply():
        from ..mail_cleanup import CLEANUP, CleanupError
        body = request.get_json(silent=True) or {}
        if body.get("confirmed") is not True:
            return jsonify({"error": "Revise e confirme os e-mails que irão para a lixeira."}), 400
        try:
            return jsonify(CLEANUP.apply(body.get("id"), body.get("uids"))), 202
        except CleanupError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/mail-cleanup/stop")
    def mail_cleanup_stop():
        from ..mail_cleanup import CLEANUP
        CLEANUP.cancel.set()
        return jsonify({"ok": True})

    # -- saldos (crowtado) -----------------------------------------------------
    @app.get("/api/balances")
    def get_balances():
        account_rows = _list_accounts()
        balances = _load_balances()
        configured = sorted(a["email"] for a in account_rows)
        configured_set = set(configured)
        saved_passwords = _crowtado_creds()
        # O cofre pode conservar credenciais de identidades removidas. Elas não
        # pertencem mais à operação atual e não devem inflar a contagem exibida
        # nem aparecer como contas conectadas no desktop.
        with_password = sorted(
            email for email in _configured_crowtado_creds() if email in configured_set
        )
        kinds = {str(account["email"]): org_policy.account_kind(str(account["email"]))
                 for account in account_rows}
        runner_state = BALANCES_RUNNER.snapshot()
        wallet_state = wallet.snapshot(account_rows, balances, set(with_password), kinds, runner_state)
        return jsonify({
            "balances": {email: record for email, record in balances.items() if email in configured_set},
            "accounts": configured,
            "account_kinds": {
                str(account["email"]): org_policy.account_kind(str(account["email"]))
                for account in account_rows
            },
            "with_password": with_password,
            "with_saved_password": sorted(
                account["email"] for account in account_rows
                if _saved_account_password(account["email"], saved_passwords)
            ),
            "refresh_needed": sorted(email for email, row in wallet_state["accounts"].items() if row["refresh_needed"]),
            "runner": runner_state,
            "wallet": wallet_state,
            "withdraw_bulk": _withdraw_bulk_snapshot(),
            "payout_method_bulk": _payout_method_snapshot(),
            "wise_cleanup": _wise_cleanup_snapshot(),
            "last_withdrawal": _last_withdrawal_receipt(configured_set),
            "exchange": fx.usd_brl_quote(),
        })

    @app.post("/api/balances/wise-cleanup")
    @_serialize_wallet_command
    def finish_pending_wise_cleanup():
        """Retoma apenas a remoção Wise e a preferência Dots, inclusive após reiniciar."""
        if not _PAYOUT_OPERATION_LOCK.acquire(blocking=False):
            return jsonify({"error": "aguarde a operação atual terminar"}), 409
        try:
            pending = _wise_cleanup_snapshot()
            if not pending.get("pending"):
                return jsonify({"ok": True, "message": "não há limpeza Wise pendente"})
            if pending.get("error"):
                return jsonify({"error": pending["error"]}), 409
            email = pending.get("email")
            password = _configured_crowtado_creds().get(email)
            if not email or not password:
                return jsonify({"error": "reconecte o acesso da conta pendente para concluir a limpeza Wise"}), 409
            result = _finish_wise_cleanup(email, password)
            if result["cleanupPending"]:
                return jsonify({"error": "limpeza ainda não confirmada; saques continuam bloqueados", "result": result}), 409
            return jsonify({"ok": True, "message": "Wise desvinculada e Dots confirmado. Nenhum saque foi solicitado.",
                            "result": result})
        finally:
            _PAYOUT_OPERATION_LOCK.release()

    @app.post("/api/balances/payout-methods/apply-all")
    @_serialize_wallet_command
    def apply_all_payout_methods():
        if _wise_cleanup_snapshot().get("pending"):
            return jsonify({"error": "conclua a limpeza Wise pendente antes de configurar métodos"}), 409
        body = request.get_json(silent=True) or {}
        method = str(body.get("method") or "").strip().lower()
        legal_name = str(body.get("legal_name") or "").strip()
        destination_email = str(body.get("destination_email") or "").strip()
        if method not in {"dots", "paypal", "wise"}:
            return jsonify({"error": "escolha Dots, PayPal ou Wise"}), 400
        if method == "wise":
            return jsonify({"error": "selecione Wise ao solicitar saque; o vínculo é feito uma conta por vez"}), 400
        if method == "paypal" and (legal_name or destination_email):
            return jsonify({"error": "PayPal usa o fluxo da Crowtado, sem destino manual. Atualize o QMoney."}), 400
        if method == "wise":
            if len(legal_name) < 2 or len(destination_email) > 254 or not re.fullmatch(
                    r"[^\s@]+@[^\s@]+\.[^\s@]+", destination_email):
                return jsonify({"error": "informe nome legal e e-mail válido do destino"}), 400
        if BALANCES_RUNNER.running or _withdraw_bulk_snapshot()["state"] == "running":
            return jsonify({"error": "aguarde a operação de saldos terminar"}), 409
        configured = {a["email"] for a in _list_accounts()
                      if org_policy.account_kind(str(a["email"])) == "crowtado"}
        creds = {e: p for e, p in _configured_crowtado_creds().items() if e in configured}
        if not creds:
            return jsonify({"error": "não há contas Crowtado conectadas"}), 400
        with _PAYOUT_METHOD_LOCK:
            if _PAYOUT_METHOD_STATE["state"] == "running":
                return jsonify({"error": "configuração em lote já está em andamento"}), 409
            _PAYOUT_METHOD_STATE.update(state="running", method=method, total=len(creds),
                                        done=0, results=[], current=None, message="")
        try:
            threading.Thread(target=_payout_method_run,
                             args=(creds, method, legal_name, destination_email),
                             daemon=True, name="moneymin-payout-methods").start()
        except RuntimeError:
            with _PAYOUT_METHOD_LOCK:
                _PAYOUT_METHOD_STATE["state"] = "error"
            return jsonify({"error": "não foi possível iniciar a configuração"}), 503
        return jsonify({"ok": True, "total": len(creds)})

    @app.post("/api/balances/refresh")
    @_serialize_wallet_command
    def refresh_balances():
        if (_withdraw_bulk_snapshot()["state"] == "running"
                or _payout_method_snapshot()["state"] == "running"
                or _PAYOUT_OPERATION_LOCK.locked()):
            return jsonify({"error": "aguarde o saque em lote terminar"}), 409
        body = request.get_json(silent=True) or {}
        if (not isinstance(body, dict) or not isinstance(body.get("emails", []), list)
                or any(not isinstance(email, str) for email in body.get("emails", []))):
            return jsonify({"error": "informe uma lista de e-mails para consultar"}), 400
        _load_balances()
        configured = {
            a["email"] for a in _list_accounts()
            if org_policy.account_kind(str(a["email"])) == "crowtado"
        }
        creds = {e: p for e, p in _configured_crowtado_creds().items() if e in configured}
        emails = [e.strip().casefold() for e in body.get("emails", []) if e.strip()]
        if emails:
            normalized = {e.casefold(): e for e in configured}
            selected = {normalized[e] for e in emails if e in normalized}
            if not selected:
                return jsonify({
                    "error": "saldo Crowtado não se aplica às contas selecionadas",
                }), 400
            creds = {e: creds[e] for e in selected if e in creds}
        elif not configured:
            return jsonify({
                "error": "não há conta Crowtado cadastrada para consultar saldo",
            }), 400
        if not creds:
            return jsonify({
                "error": (
                    "a identidade está conectada ao Minute, mas o acesso ao "
                    "Crowtado ainda não foi informado; use Conectar Crowtado "
                    "na linha da conta"
                ),
            }), 400
        try:
            BALANCES_RUNNER.start(creds, _on_balance_result)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"ok": True, "total": len(creds)})

    @app.post("/api/balances/stop")
    def stop_balance_refresh():
        BALANCES_RUNNER.stop()
        return jsonify({"ok": True, "runner": BALANCES_RUNNER.snapshot()})

    @app.post("/api/balances/withdraw")
    @_serialize_wallet_command
    def request_balance_withdraw():
        """Solicita o método salvo ou o fluxo Wise confirmado pelo usuário."""
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "requisição inválida"}), 400
        try:
            wise = _wise_withdraw_options(body)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if _payout_method_snapshot()["state"] == "running":
            return jsonify({"error": "aguarde a configuração dos métodos terminar"}), 409
        email = str(body.get("email", "")).strip()
        if not email:
            return jsonify({"error": "informe a conta"}), 400
        configured = {a["email"] for a in _list_accounts()}
        if email not in configured:
            return jsonify({"error": "conta não está configurada"}), 404
        if org_policy.account_kind(email) == "claru":
            return jsonify({"error": "saldo Crowtado não se aplica a contas Claru"}), 400
        password = _configured_crowtado_creds().get(email)
        if not password:
            return jsonify({"error": "salve a senha do crowtado primeiro"}), 400
        if BALANCES_RUNNER.running:
            return jsonify({"error": "aguarde a consulta de saldos terminar"}), 409
        if _withdraw_bulk_snapshot()["state"] == "running":
            return jsonify({"error": "aguarde o saque em lote terminar"}), 409
        balance = _load_balances().get(email)
        if isinstance(balance, dict) and crowtado.payout_in_transit(balance):
            return jsonify({"error": "há um pagamento em trânsito que a Crowtado não permite substituir; atualize o saldo após a conclusão"}), 409
        if not _confirmed_available_balance(balance):
            return jsonify({"error": "atualize o saldo desta conta antes de solicitar saque; é necessário saldo aprovado superior a US$ 25,00"}), 400
        eligibility = wallet.payout(balance, wallet.reading(balance), True)
        if not eligibility["eligible"]:
            return jsonify({"error": eligibility["reason"], "code": eligibility["code"]}), 409
        if wise and wise.get("method") != "paypal" and body.get("background") is True:
            if _wise_cleanup_snapshot().get("pending"):
                return jsonify({"error": "há uma limpeza Wise pendente; nenhum novo saque será enviado"}), 409
            payload, status = _start_withdraw_worker({email: password}, wise)
            return jsonify(payload), status
        result, status = (_withdraw_once(email, password, wise) if wise
                          else _withdraw_once(email, password))
        return jsonify(result), status

    @app.post("/api/balances/withdraw-all")
    @_serialize_wallet_command
    def request_all_balance_withdrawals():
        """Processa contas elegíveis sequencialmente, incluindo a limpeza Wise."""
        if _wise_cleanup_snapshot().get("pending"):
            return jsonify({"error": "conclua a limpeza Wise pendente antes de iniciar outro lote"}), 409
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "requisição inválida"}), 400
        try:
            wise = _wise_withdraw_options(body)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if _payout_method_snapshot()["state"] == "running":
            return jsonify({"error": "aguarde a configuração dos métodos terminar"}), 409
        if BALANCES_RUNNER.running:
            return jsonify({"error": "aguarde a consulta de saldos terminar"}), 409
        configured = {a["email"] for a in _list_accounts()}
        passwords = _configured_crowtado_creds()
        balances = _load_balances()
        eligible = {}
        for email in sorted(configured):
            if org_policy.account_kind(email) != "crowtado" or email not in passwords:
                continue
            balance = balances.get(email) or {}
            if not wallet.payout(balance, wallet.reading(balance), True)["eligible"]:
                continue
            eligible[email] = passwords[email]
        if not eligible:
            return jsonify({"error": "não há contas elegíveis: é necessário saldo aprovado superior a US$ 25,00 e nenhum pagamento em trânsito bloqueando novo saque"}), 400
        payload, status = _start_withdraw_worker(eligible, wise)
        return jsonify(payload), (200 if status == 202 else status)

    @app.put("/api/balances/credentials")
    @_serialize_wallet_command
    def put_balance_credentials():
        if (BALANCES_RUNNER.running or _withdraw_bulk_snapshot()["state"] == "running"
                or _payout_method_snapshot()["state"] == "running" or _PAYOUT_OPERATION_LOCK.locked()):
            return jsonify({"error": "aguarde a operação da Carteira terminar antes de alterar o acesso"}), 409
        body = request.get_json(silent=True) or {}
        email = body.get("email", "")
        password = body.get("password", "")
        if not isinstance(email, str) or not isinstance(password, str):
            return jsonify({"error": "email e senha devem ser texto", "code": "invalid_credentials"}), 400
        email = email.strip()
        if not email or not password:
            return jsonify({"error": "informe email e senha"}), 400
        try:
            account_bans.require_not_banned(email)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        configured = {a["email"] for a in _list_accounts()}
        if email not in configured:
            return jsonify({"error": "essa identidade não está conectada ao QMoney"}), 404
        if org_policy.account_kind(email) == "claru":
            return jsonify({"error": "contas Claru não precisam de acesso Crowtado"}), 400
        try:
            crowtado.login(email, password)
        except (crowtado.CrowtadoError, RuntimeError, OSError) as exc:
            issue = account_issue(email, exc, stage="Login Crowtado")
            return jsonify({
                "error": issue["reason"] + " " + issue["action"],
                "code": "crowtado_login_failed", "issue": issue,
            }), 400
        try:
            _save_crowtado_cred(email, password)
        except (OSError, ValueError):
            return jsonify({"ok": False, "partial": True, "code": "local_credential_save_failed",
                            "error": "O acesso Crowtado foi confirmado, mas a senha não foi salva. Confira o armazenamento local antes de tentar novamente."}), 500
        return jsonify({
            "ok": True,
            "email": email,
            "message": "Acesso ao Crowtado confirmado e salvo.",
        })

    # -- registro de enviados ---------------------------------------------------
    @app.get("/api/sent")
    def get_sent():
        try:
            return jsonify({"sent": sent_registry.summary()})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.post("/api/sent/reset")
    def reset_sent():
        with _HEAVY_RUNNER_LOCK:
            if RUNNER.running or RECOVERY.running:
                return jsonify({
                    "error": "aguarde a campanha terminar antes de resetar a lista de vídeos usados",
                }), 409
            body = request.get_json(silent=True) or {}
            scenario = body.get("scenario")
            try:
                sent_registry.reset(str(scenario) if scenario else None)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 409
            return jsonify({"ok": True, "sent": sent_registry.summary()})

    @app.post("/api/campaign/reset")
    def reset_campaign_state():
        body = request.get_json(silent=True) or {}
        if body:
            return jsonify({"error": "Reset completo não aceita filtros ou caminhos."}), 400
        try:
            with campaign_state_lease(exclusive=True), _HEAVY_RUNNER_LOCK:
                workers = (RUNNER, RECOVERY, HOLO_CACHE_RUNNER, nymeria_library_worker)
                catalogs = (task_catalog, campaign_verifications, accelerator_catalog,
                            recovery_catalog, original_library_index, prepared_library_inventory,
                            local_media_inventory, nymeria_catalog)
                if (campaign_drain["requested"] or campaign_drain["requests"]
                        or any(worker.running or (getattr(worker, "_thread", None) is not None
                                   and worker._thread.is_alive()) for worker in workers)
                        or any(catalog.busy for catalog in catalogs)):
                    return jsonify({"error_code": "campaign_reset_busy",
                                    "error": "Pare a campanha ou recuperação e aguarde as operações e consultas terminarem antes do Reset completo."}), 409
                result = campaign_reset.erase_local_campaigns()
                RUNNER.reset_idle()
                RECOVERY.reset_idle()
                with preflight_lock:
                    preflights.clear()
                    original_preflights.clear()
                    original_starts.clear()
                for catalog in catalogs:
                    catalog.clear_idle()
                # Invalidate derived selection results; raw media and evidence
                # in the Library stay intact and are re-read on demand.
                campaign._ranked_pools_cached.cache_clear()
                campaign._duration_ranked_pools.cache_clear()
                campaign._nymeria_windows.cache_clear()
        except CampaignStateLeaseError:
            return jsonify({"error_code": "campaign_reset_busy",
                            "error": "Aguarde as operações locais terminarem antes do Reset completo."}), 409
        except campaign_reset.CampaignResetError as exc:
            return jsonify({"ok": False, "error_code": exc.code,
                            "error": str(exc), "partial": exc.partial}), 409
        return jsonify({"ok": True, "state": "idle", **result})

    return app


def _log_path(name: str) -> Path | None:
    """Resolve um nome de log de forma segura (sem path traversal)."""
    if not campaign.is_campaign_history_name(name) or "/" in name \
            or "\\" in name or name == "campaign.example.json":
        return None
    path = config.DATA_DIR / name
    return path if path.exists() else None


def _parse_active_hours(raw) -> tuple[int, int] | None | bool:
    """[7, 18] -> (7, 18); None/ausente -> None (sem janela); inválido -> False."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2 or any(type(v) is not int for v in raw):
        return False
    start, end = raw
    if not (0 <= start < end <= 24):
        return False
    return (start, end)


def _safe_org(email: str) -> str | None:
    try:
        return _resolve_org(email)
    except Exception:  # noqa: BLE001 — best-effort
        return None


__all__ = ["create_app"]
