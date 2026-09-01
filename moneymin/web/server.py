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
                                         body: {accounts, tasks, count, max_dur_s,
                                         delay_mode (off|clip|fixed), delay_s,
                                         paralelismo automático entre contas,
                                         active_hours: [7, 18] | null}
    GET    /api/campaigns/current?since=N  estado + eventos novos (polling)
    POST   /api/campaigns/stop           parada cooperativa

  Acelerador HoloAssist (pré-cache retomável)
    GET    /api/holo-cache               cobertura local + estado do runner
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
    POST   /api/balances/withdraw        {email} -> solicita link de saque ao Dots
    PUT    /api/balances/credentials     {email, password} -> salva senha do crowtado
                                         (secrets/crowtado_passwords.json)

  Registro de enviados (data/sent_videos.json — dedup entre campanhas)
    GET    /api/sent                     resumo por cenário (clipes/envios)
    POST   /api/sent/reset               limpa o registro (body: {scenario?})
"""
from __future__ import annotations

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from .. import campaign, config, crowtado, holo_accelerator, holoassist, sent_registry
from ..atomic_io import load_json, save_json
from ..campaign import AccountSpec, CampaignConfig, TaskSpec
from ..minute_api import AuthError, Session, login
from .runner import BALANCES_RUNNER, HOLO_CACHE_RUNNER, RUNNER

PREFS_PATH = config.DATA_DIR / "webui_prefs.json"
BALANCES_PATH = config.DATA_DIR / "balances.json"
CROWTADO_PW_PATH = config.SECRETS_DIR / "crowtado_passwords.json"
MAX_DUR_S = 1800.0  # cap do recording-config do Minute
_PERSISTENCE_LOCK = threading.RLock()
_HEAVY_RUNNER_LOCK = threading.Lock()
_WITHDRAW_LOCK = threading.Lock()
_WITHDRAW_IN_FLIGHT: set[str] = set()
_WITHDRAW_LAST_REQUEST: dict[str, float] = {}
_WITHDRAW_COOLDOWN_S = 60.0


# --- preferências ------------------------------------------------------------

def _load_prefs() -> dict[str, Any]:
    value = load_json(PREFS_PATH, {})
    return value if isinstance(value, dict) else {}


def _save_prefs(prefs: dict[str, Any]) -> None:
    with _PERSISTENCE_LOCK:
        save_json(PREFS_PATH, prefs)


# --- contas -------------------------------------------------------------------

def _list_accounts() -> list[dict[str, Any]]:
    """Contas = token_*.json em secrets/ (sem rede). org_key vem do cache de prefs."""
    prefs = _load_prefs()
    org_keys = prefs.get("org_keys", {})
    out: list[dict[str, Any]] = []
    for path in sorted(config.tokens_dir().glob("token_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        email = data.get("email")
        if not email:
            continue
        out.append({
            "email": email,
            "expires_at": data.get("expires_at", 0),
            "org_key": org_keys.get(email),
        })
    return out


def _resolve_org(email: str, session: Session | None = None) -> str:
    """Resolve (e cacheia) a org_key da conta: prefs -> me()['organizations'][0]."""
    # Validar sempre, inclusive quando a organização já está no cache. O HUB
    # devolve /users/me = 200 para contas desativadas; ensure_auth inspeciona o
    # campo `disabled` e impede que a campanha comece com uma conta bloqueada.
    sess = session or Session.from_email(email)
    profile = sess.ensure_auth()
    cached = _load_prefs().get("org_keys", {}).get(email)
    if cached:
        return cached
    orgs = profile.get("organizations") or []
    if not orgs:
        raise RuntimeError(f"a conta {email} não pertence a nenhuma organização")
    org_key = orgs[0]["resourceKey"]
    # Outra resolução pode terminar ao mesmo tempo. Releia dentro do lock para
    # não sobrescrever a org_key que a thread vizinha acabou de persistir.
    with _PERSISTENCE_LOCK:
        prefs = _load_prefs()
        prefs.setdefault("org_keys", {})[email] = org_key
        _save_prefs(prefs)
    return org_key


def _check_account_health(email: str) -> dict[str, Any]:
    """Valida uma conta e devolve um estado estável para a verificação em lote."""
    try:
        session = Session.from_email(email)
        org_key = _resolve_org(email, session=session)
        return {
            "email": email, "status": "active", "org_key": org_key,
            "expires_at": session.data.get("expires_at", 0),
        }
    except (AuthError, RuntimeError, OSError) as exc:
        error = str(exc)
        disabled = "conta desativada no hub" in error.lower()
        return {
            "email": email,
            "status": "disabled" if disabled else "error",
            "error": error,
        }


# --- saldos (crowtado) ----------------------------------------------------------

def _load_balances() -> dict[str, Any]:
    """Cache de saldos: {email: {availableCents, ..., updated_at, error?}}."""
    value = load_json(BALANCES_PATH, {})
    return value if isinstance(value, dict) else {}


def _save_balances(balances: dict[str, Any]) -> None:
    with _PERSISTENCE_LOCK:
        save_json(BALANCES_PATH, balances)


def _crowtado_creds() -> dict[str, str]:
    """Senhas do crowtado por email: contas.jsonl (registrar_conta) + arquivo de senhas."""
    creds: dict[str, str] = {}
    contas = config.DATA_DIR / "contas.jsonl"
    try:
        for line in contas.read_text(encoding="utf-8-sig").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("email") and rec.get("senha"):
                creds[rec["email"]] = rec["senha"]
    except OSError:
        pass
    stored = load_json(CROWTADO_PW_PATH, {})
    if isinstance(stored, dict):
        creds.update({str(email): str(password)
                      for email, password in stored.items()})
    return creds


def _save_crowtado_cred(email: str, password: str) -> None:
    with _PERSISTENCE_LOCK:
        stored = load_json(CROWTADO_PW_PATH, {})
        creds = stored if isinstance(stored, dict) else {}
        creds[email] = password
        save_json(CROWTADO_PW_PATH, creds)


def _remove_account_data(email: str) -> None:
    """Remove caches editáveis ligados à conta (o cadastro histórico fica intacto)."""
    with _PERSISTENCE_LOCK:
        stored = load_json(CROWTADO_PW_PATH, {})
        creds = stored if isinstance(stored, dict) else {}
        if creds.pop(email, None) is not None:
            save_json(CROWTADO_PW_PATH, creds)

        balances = _load_balances()
        if balances.pop(email, None) is not None:
            _save_balances(balances)
    crowtado.clear_cached_session(email)


def _on_balance_result(email: str, summary: dict | None, erro: str | None) -> None:
    with _PERSISTENCE_LOCK:
        balances = _load_balances()
        rec = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        if summary:
            rec.update(summary)
            rec["error"] = None
        else:
            rec["error"] = erro
        balances[email] = rec
        _save_balances(balances)


def _withdraw_message(email: str, result: dict[str, Any]) -> str:
    """Mensagem curta e segura para o resultado do payouts.withdraw."""
    status = str(result.get("status") or "")
    if status == "ok":
        if result.get("dotsEmailDelivery") == "sent":
            return f"link de saque enviado por email para {email}"
        if result.get("dotsSmsDelivery") == "sent":
            return f"link de saque enviado por SMS para a conta {email}"
        if result.get("dotsEmailDelivery") == "already_settled":
            return f"saque solicitado para {email}; cadastro Dots já estava concluído"
        return f"solicitação de saque enviada para {email}"
    if status == "review_required":
        return f"saque de {email} enviado para revisão do Crowtado"
    messages = {
        "below_minimum": "saldo abaixo do mínimo para saque",
        "hold": "saques estão temporariamente bloqueados para esta conta",
        "dots_not_ready": "o método Dots ainda não está disponível para esta conta",
        "no_balance": "não há saldo disponível para saque",
        "dots_failed": "o Dots recusou a solicitação de saque",
        "dots_pending_retry": "o Crowtado ainda tentará enviar o link novamente",
    }
    return messages.get(
        status, f"saque não solicitado (status: {status or 'desconhecido'})")


# --- app -----------------------------------------------------------------------

def _parse_duration_range(values) -> tuple[float, float]:
    """Valida o teto da UI e o mínimo legado de clientes anteriores."""
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


def create_app() -> Flask:
    # No QMoney o Flask e apenas o servico local consumido pela interface Qt.
    # Nenhum frontend web e publicado ou usado como fallback.
    app = Flask(__name__, static_folder=None)

    @app.get("/")
    def index():
        return jsonify({"service": "qmoney", "ui": "qt", "ok": True})

    # -- contas ---------------------------------------------------------------
    @app.get("/api/accounts")
    def get_accounts():
        return jsonify({"accounts": _list_accounts()})

    @app.post("/api/accounts")
    def add_account():
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))
        if not email or not password:
            return jsonify({"error": "informe email e senha"}), 400
        try:
            login(email, password)
        except (RuntimeError, OSError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "email": email})

    @app.post("/api/accounts/register")
    def register_account():
        """Cria conta nova no Minute (convite fixo) e já deixa tudo salvo.

        Se o email já existir no Minute, cai pro login simples. A senha também
        vai para secrets/crowtado_passwords.json (mesma credencial do crowtado)
        para a aba Saldos funcionar sem redigitar.
        """
        from ..minute_api import register as minute_register

        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))
        if not email or not password:
            return jsonify({"error": "informe email e senha"}), 400
        try:
            minute_register(email, password)  # usa config.INVITE_CODE
            created = True
        except RuntimeError:
            try:
                login(email, password)  # já existia — só autentica
                created = False
            except RuntimeError as exc:
                return jsonify({"error": str(exc)}), 400
        _save_crowtado_cred(email, password)
        return jsonify({"ok": True, "email": email, "created": created})

    @app.delete("/api/accounts/<email>")
    def remove_account(email: str):
        if RUNNER.running:
            return jsonify({"error": "pare a campanha antes de remover uma conta"}), 409
        if BALANCES_RUNNER.running:
            return jsonify({"error": "aguarde a consulta de saldos terminar"}), 409
        path = config.token_path(email)
        if not path.exists():
            return jsonify({"error": f"conta não encontrada: {email}"}), 404
        path.unlink()
        prefs = _load_prefs()
        prefs.get("org_keys", {}).pop(email, None)
        selected = prefs.get("selected_accounts")
        if isinstance(selected, list):
            prefs["selected_accounts"] = [e for e in selected if e != email]
        _save_prefs(prefs)
        _remove_account_data(email)
        return jsonify({"ok": True})

    @app.post("/api/accounts/<email>/check")
    def check_account(email: str):
        try:
            sess = Session.from_email(email)
            org_key = _resolve_org(email, session=sess)
        except (AuthError, RuntimeError, OSError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "email": email, "org_key": org_key,
                        "expires_at": sess.data.get("expires_at", 0)})

    @app.post("/api/accounts/check-all")
    def check_all_accounts():
        if RUNNER.running:
            return jsonify({
                "error": "aguarde ou pare a campanha antes de verificar todas as contas",
            }), 409
        emails = [account["email"] for account in _list_accounts()]
        results_by_email: dict[str, dict[str, Any]] = {}
        if emails:
            workers = min(6, len(emails))
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="moneymin-account-check") as pool:
                futures = {pool.submit(_check_account_health, email): email
                           for email in emails}
                for future in as_completed(futures):
                    email = futures[future]
                    try:
                        results_by_email[email] = future.result()
                    except Exception as exc:  # noqa: BLE001 — uma conta não mata o lote
                        results_by_email[email] = {
                            "email": email, "status": "error",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
        results = [results_by_email[email] for email in emails]
        return jsonify({
            "ok": True,
            "total": len(results),
            "active": sum(result["status"] == "active" for result in results),
            "disabled": [result for result in results
                         if result["status"] == "disabled"],
            "errors": [result for result in results if result["status"] == "error"],
            "results": results,
        })

    # -- categorias --------------------------------------------------------------
    @app.get("/api/tasks")
    def get_tasks():
        email = str(request.args.get("email", "")).strip()
        if not email:
            return jsonify({"error": "informe ?email=<conta>"}), 400
        try:
            dataset_provider = campaign.normalize_dataset_provider(
                request.args.get("dataset")
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            min_dur_s, max_dur_s = _parse_duration_range(request.args)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            # Uma Session só: _resolve_org + catálogo. Dois refresh seguidos
            # no Firebase invalidam o refreshToken e o GET vira 400.
            sess = Session.from_email(email)
            org_key = _resolve_org(email, session=sess)
            tasks = campaign.available_tasks(
                email, org_key, min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                include_unavailable=True, dataset_provider=dataset_provider,
                session=sess)
        except json.JSONDecodeError:
            return jsonify({
                "error": "a API devolveu resposta vazia (não-JSON). Tente de novo.",
            }), 400
        except AuthError as exc:
            app.logger.warning("GET /api/tasks auth %s: %s", email, exc)
            return jsonify({"error": str(exc)}), 400
        except (RuntimeError, OSError) as exc:
            app.logger.warning("GET /api/tasks %s: %s", email, exc)
            msg = str(exc)
            if "Expecting value" in msg:
                msg = "a API devolveu resposta vazia (não-JSON). Tente de novo."
            return jsonify({"error": msg}), 400
        return jsonify({"email": email, "org_key": org_key,
                        "dataset": dataset_provider, "tasks": tasks,
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

    # -- acelerador HoloAssist -------------------------------------------------
    @app.get("/api/holo-cache")
    def holo_cache_status():
        task = str(request.args.get("task") or holo_accelerator.DEFAULT_TASK)
        if task not in holoassist.MINUTE_TASK_TYPES:
            return jsonify({"error": "tarefa HoloAssist inválida"}), 400
        raw_limit = request.args.get("limit")
        try:
            limit = None if raw_limit in (None, "", "0") else int(raw_limit)
            if limit is not None and not 1 <= limit <= 1000:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "limit deve estar entre 1 e 1000"}), 400
        try:
            cache = holo_accelerator.cache_status(task, limit=limit)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            cache = {
                "task": task,
                "total": 0,
                "ready": 0,
                "partial": 0,
                "pending": 0,
                "last_run": {},
                "catalog_error": str(exc),
            }
        return jsonify({
            "cache": cache,
            "runner": HOLO_CACHE_RUNNER.snapshot(),
            "tasks": sorted(holoassist.MINUTE_TASK_TYPES),
        })

    @app.post("/api/holo-cache/start")
    def holo_cache_start():
        body = request.get_json(silent=True) or {}
        task = str(body.get("task") or holo_accelerator.DEFAULT_TASK)
        if task not in holoassist.MINUTE_TASK_TYPES:
            return jsonify({"error": "tarefa HoloAssist inválida"}), 400
        try:
            raw_limit = body.get("limit")
            limit = None if raw_limit in (None, "", 0, "0") else int(raw_limit)
            if limit is not None and not 1 <= limit <= 1000:
                raise ValueError
            min_free_gb = float(body.get("min_free_gb", 50))
            if not math.isfinite(min_free_gb) or not 5 <= min_free_gb <= 1000:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({
                "error": "use limite entre 1 e 1000 e reserva de disco entre 5 e 1000 GiB",
            }), 400

        # Falhe antes de abrir a thread quando os metadados ainda não foram
        # instalados. A mensagem original explica qual comando deve ser usado.
        try:
            catalog = holo_accelerator.cache_status(task, limit=limit)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return jsonify({"error": str(exc)}), 400

        with _HEAVY_RUNNER_LOCK:
            if RUNNER.running:
                return jsonify({
                    "error": "pare a campanha antes de iniciar o acelerador HoloAssist",
                }), 409
            if HOLO_CACHE_RUNNER.running:
                return jsonify({
                    "ok": True,
                    "already_running": True,
                    "runner": HOLO_CACHE_RUNNER.snapshot(),
                })
            try:
                HOLO_CACHE_RUNNER.start(
                    task=task,
                    limit=limit,
                    min_free_gb=min_free_gb,
                )
            except RuntimeError as exc:
                return jsonify({"error": str(exc)}), 409
        return jsonify({
            "ok": True,
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
        with _HEAVY_RUNNER_LOCK:
            if RUNNER.running or HOLO_CACHE_RUNNER.running:
                return jsonify({
                    "error": "pare a campanha e o acelerador antes de limpar a mídia",
                }), 409
            result = campaign.cleanup_media_cache(config.DATA_DIR / "ego4d")
        return jsonify({"ok": not result["errors"], **result})

    # -- campanha ---------------------------------------------------------------
    @app.post("/api/campaigns")
    def start_campaign():
        if RUNNER.running:
            return jsonify({
                "ok": True,
                "already_running": True,
                "state": RUNNER.state,
                "total_sends": RUNNER.total_sends,
            })
        if HOLO_CACHE_RUNNER.running:
            return jsonify({
                "error": "pare o acelerador HoloAssist antes de iniciar a campanha",
            }), 409
        body = request.get_json(silent=True) or {}
        cleanup_after_upload = body.get("cleanup_after_upload", True)
        if not isinstance(cleanup_after_upload, bool):
            return jsonify({
                "error": "cleanup_after_upload deve ser true ou false",
            }), 400
        try:
            dataset_provider = campaign.normalize_dataset_provider(
                body.get("dataset")
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        emails = [str(e).strip() for e in body.get("accounts", []) if str(e).strip()]
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

        known = {a["email"] for a in _list_accounts()}
        missing = [e for e in emails if e not in known]
        if missing:
            return jsonify({"error": "conta(s) sem token: " + ", ".join(missing)}), 400

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
        if not accounts:
            return jsonify({
                "error": "nenhuma conta autenticou. " + "; ".join(skipped),
            }), 400

        # Nunca aceite do browser a associação task_id -> cenário. Uma aba
        # antiga ou uma troca rápida de conta podia mandar um par inconsistente
        # e selecionar vídeos de outra categoria. O catálogo atual da conta é a
        # fonte de verdade.
        try:
            available = campaign.available_tasks(
                accounts[0].email, accounts[0].org_key,
                min_dur_s=min_dur_s, max_dur_s=max_dur_s,
                include_unavailable=True, dataset_provider=dataset_provider)
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
                account_task_ids = {
                    str(task.get("id")) for task in sess.all_tasks(account.org_key)
                    if task.get("id")
                }
            except (AuthError, RuntimeError, OSError) as exc:
                return account.email, f"preflight falhou para {account.email}: {exc}"
            missing_tasks = selected_ids - account_task_ids
            return account.email, missing_tasks

        others = accounts[1:]
        if others:
            by_email = {acc.email: acc for acc in accounts}
            viable = [accounts[0]]
            workers = max(1, min(8, len(others)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for email, result in pool.map(_preflight, others):
                    if isinstance(result, str):
                        skipped.append(result)
                        continue
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

        # O MP4 é normalizado uma vez antes do lote. Três PUTs simultâneos
        # reduzem 11 contas a quatro ondas sem disputar CPU com o ffmpeg.
        account_workers = min(len(accounts), campaign.max_account_workers())

        cfg = CampaignConfig(accounts=accounts, tasks=tasks,
                             work_dir=config.DATA_DIR / "ego4d",
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
                             cleanup_after_upload=cleanup_after_upload,
                             realistic_timeline=True,
                             active_hours=active_hours)
        with _HEAVY_RUNNER_LOCK:
            if HOLO_CACHE_RUNNER.running:
                return jsonify({
                    "error": "pare o acelerador HoloAssist antes de iniciar a campanha",
                }), 409
            try:
                RUNNER.start(cfg)
            except RuntimeError as exc:
                # Corrida entre dois cliques/abas: se o outro request venceu e
                # iniciou, este POST também é sucesso idempotente, nunca erro 409.
                if RUNNER.running:
                    return jsonify({
                        "ok": True,
                        "already_running": True,
                        "state": RUNNER.state,
                        "total_sends": RUNNER.total_sends,
                    })
                return jsonify({"error": str(exc)}), 409
        payload = {
            "ok": True,
            "total_sends": RUNNER.total_sends,
            "selected_tasks": [
                {"task_id": t.task_id, "scenario": t.scenario} for t in tasks
            ],
            "accounts": [acc.email for acc in accounts],
            "dataset": dataset_provider,
        }
        if skipped:
            payload["skipped_accounts"] = skipped
        return jsonify(payload)

    @app.get("/api/campaigns/current")
    def campaign_current():
        try:
            since = int(request.args.get("since", 0))
        except ValueError:
            since = 0
        return jsonify(RUNNER.snapshot(since=since))

    @app.post("/api/campaigns/stop")
    def campaign_stop():
        RUNNER.stop()
        return jsonify({"ok": True, "state": RUNNER.state})

    # -- histórico ----------------------------------------------------------------
    @app.get("/api/logs")
    def get_logs():
        out: list[dict[str, Any]] = []
        for path in campaign.list_campaign_logs():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
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
                "ok": sum(1 for s in sends if s.get("ok")),
            })
        return jsonify({"logs": out})

    @app.get("/api/logs/<name>")
    def get_log(name: str):
        path = _log_path(name)
        if path is None:
            return jsonify({"error": "log não encontrado"}), 404
        try:
            return jsonify(json.loads(path.read_text(encoding="utf-8-sig")))
        except json.JSONDecodeError:
            return jsonify({"error": "log ilegível (JSON vazio/corrompido)"}), 400

    @app.post("/api/logs/<name>/status")
    def get_log_status(name: str):
        path = _log_path(name)
        if path is None:
            return jsonify({"error": "log não encontrado"}), 404
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        results: list[dict[str, Any]] = []
        for it in data.get("items", []):
            for acc in it.get("accounts", []):
                sid = acc.get("session_id")
                if not sid:
                    continue
                org = acc.get("org_key") or _safe_org(acc.get("email", ""))
                if not org:
                    results.append({"session_id": sid, "email": acc.get("email"),
                                    "status": "sem org_key"})
                    continue
                try:
                    results.append(campaign.session_result(acc["email"], org, sid))
                except (AuthError, RuntimeError, OSError) as exc:
                    results.append({"session_id": sid, "email": acc.get("email"),
                                    "status": f"erro: {exc}"})
        return jsonify({"results": results})

    # -- saldos (crowtado) -----------------------------------------------------
    @app.get("/api/balances")
    def get_balances():
        configured = sorted(a["email"] for a in _list_accounts())
        return jsonify({
            "balances": _load_balances(),
            "accounts": configured,
            "with_password": sorted(_crowtado_creds()),
            "runner": BALANCES_RUNNER.snapshot(),
        })

    @app.post("/api/balances/refresh")
    def refresh_balances():
        body = request.get_json(silent=True) or {}
        configured = {a["email"] for a in _list_accounts()}
        creds = {e: p for e, p in _crowtado_creds().items() if e in configured}
        emails = [str(e).strip() for e in body.get("emails", []) if str(e).strip()]
        if emails:
            creds = {e: creds[e] for e in emails if e in creds}
        if not creds:
            return jsonify({"error": "nenhuma conta configurada com senha do crowtado salva"}), 400
        try:
            BALANCES_RUNNER.start(creds, _on_balance_result)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"ok": True, "total": len(creds)})

    @app.post("/api/balances/withdraw")
    def request_balance_withdraw():
        """Solicita somente o envio do link Dots; conclusão e 2FA são manuais."""
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip()
        if not email:
            return jsonify({"error": "informe a conta"}), 400
        configured = {a["email"] for a in _list_accounts()}
        if email not in configured:
            return jsonify({"error": "conta não está configurada"}), 404
        password = _crowtado_creds().get(email)
        if not password:
            return jsonify({"error": "salve a senha do crowtado primeiro"}), 400
        if BALANCES_RUNNER.running:
            return jsonify({"error": "aguarde a consulta de saldos terminar"}), 409

        now = time.monotonic()
        with _WITHDRAW_LOCK:
            if email in _WITHDRAW_IN_FLIGHT:
                return jsonify({"error": "já há uma solicitação em andamento"}), 409
            elapsed = now - _WITHDRAW_LAST_REQUEST.get(email, 0.0)
            if elapsed < _WITHDRAW_COOLDOWN_S:
                wait_s = max(1, math.ceil(_WITHDRAW_COOLDOWN_S - elapsed))
                return jsonify({
                    "error": f"link já solicitado; aguarde {wait_s}s para repetir",
                }), 429
            _WITHDRAW_IN_FLIGHT.add(email)

        success = False
        try:
            result = crowtado.solicitar_link_saque(email, password)
            success = result.get("status") in {"ok", "review_required"}
            message = _withdraw_message(email, result)
            response = {
                "ok": success, "email": email,
                "message": message, "result": result,
            }
            return jsonify(response), 200 if success else 409
        except crowtado.CrowtadoError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            with _WITHDRAW_LOCK:
                _WITHDRAW_IN_FLIGHT.discard(email)
                if success:
                    _WITHDRAW_LAST_REQUEST[email] = time.monotonic()

    @app.put("/api/balances/credentials")
    def put_balance_credentials():
        body = request.get_json(silent=True) or {}
        email = str(body.get("email", "")).strip()
        password = str(body.get("password", ""))
        if not email or not password:
            return jsonify({"error": "informe email e senha"}), 400
        _save_crowtado_cred(email, password)
        return jsonify({"ok": True, "email": email})

    # -- registro de enviados ---------------------------------------------------
    @app.get("/api/sent")
    def get_sent():
        return jsonify({"sent": sent_registry.summary()})

    @app.post("/api/sent/reset")
    def reset_sent():
        body = request.get_json(silent=True) or {}
        scenario = body.get("scenario")
        sent_registry.reset(str(scenario) if scenario else None)
        return jsonify({"ok": True, "sent": sent_registry.summary()})

    return app


def _log_path(name: str) -> Path | None:
    """Resolve um nome de log de forma segura (sem path traversal)."""
    if not (name.startswith("campaign_") and name.endswith(".json")) or "/" in name \
            or "\\" in name or name == "campaign.example.json":
        return None
    path = config.DATA_DIR / name
    return path if path.exists() else None


def _parse_active_hours(raw) -> tuple[int, int] | None | bool:
    """[7, 18] -> (7, 18); None/ausente -> None (sem janela); inválido -> False."""
    if raw is None:
        return None
    try:
        start, end = int(raw[0]), int(raw[1])
    except (TypeError, ValueError, IndexError):
        return False
    if not (0 <= start < end <= 24):
        return False
    return (start, end)


def _safe_org(email: str) -> str | None:
    try:
        return _resolve_org(email)
    except Exception:  # noqa: BLE001 — best-effort
        return None


__all__ = ["create_app"]
