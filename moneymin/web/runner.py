"""
runner.py — Execução de campanha em segundo plano para a interface web.

Envolve `campaign.run_campaign` numa thread daemon, adaptando os hooks
`progress`/`should_stop` para um buffer de eventos consultável por polling
(`GET /api/campaigns/current`) e uma flag de parada cooperativa.

Uma campanha por vez por processo: `start()` levanta `RuntimeError` se já
houver uma em andamento.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .. import campaign, holo_accelerator
from ..campaign import CampaignConfig, run_campaign

_MAX_EVENTS = 2000


def _fmt_wait(total_s: int) -> str:
    """Countdown legível: '45s', '12m05s', '3h20m'."""
    m, s = divmod(total_s, 60)
    if m >= 60:
        return f"{m // 60}h{m % 60:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def friendly_campaign_error(value: Any) -> str:
    """Converte falhas técnicas em orientação curta, sem vazar internals.

    O erro original continua no JSON operacional salvo em disco. A API local e
    a interface recebem somente esta versão adequada para o usuário final.
    """
    text = str(value or "").strip().lower()
    if not text:
        return "O QMoney não conseguiu concluir esta etapa. Tente novamente."
    if "memoryerror" in text or "cannot allocate memory" in text or "not enough memory" in text:
        return "Este computador ficou sem memória durante a preparação. O QMoney liberou a etapa e seguirá com outro vídeo."
    if "cobertura imu insuficiente" in text or "sem amostras válidas de imu" in text:
        sensor = "acelerômetro" if "acelerômetro" in text else (
            "giroscópio" if "giroscópio" in text else "sensor")
        return f"O vídeo tem uma lacuna real no {sensor} do Ego4D e foi descartado antes do envio."
    if "sem imu real" in text or "sem cobertura contínua de imu" in text:
        return "O vídeo não possui sensores contínuos suficientes e foi descartado antes do envio."
    if any(term in text for term in (
            "invalidaccesskeyid", "signaturedoesnotmatch", "expiredtoken",
            "credenciais ego4d inválidas", "credenciais aws ausentes")):
        return "As credenciais do Ego4D não foram aceitas. Atualize-as na aba Integrações."
    if "acesso negado ao ego4d" in text or "accessdenied" in text:
        return "A licença Ego4D não autorizou este arquivo. O QMoney seguirá com outro vídeo."
    if any(term in text for term in ("disabled", "desativad", "blocked account")):
        return "A conta foi desativada no Minute. Valide-a antes de continuar."
    if any(term in text for term in (
            "unauthorized", "forbidden", "authentication", "autherror",
            "token expired", "token inválido", "http 401", "http 403")):
        return "A autenticação da conta expirou. Use “Verificar todas” na aba Contas."
    if "http 429" in text or "rate limit" in text or "too many requests" in text:
        return "O serviço limitou novas tentativas. Aguarde alguns minutos e tente novamente."
    if any(term in text for term in ("timeout", "timed out", "tempo esgotado")):
        return "O serviço demorou mais que o esperado. O QMoney pode tentar novamente."
    if any(term in text for term in (
            "connection", "network", "name resolution", "dns", "remote end",
            "falha de rede", "conexão")):
        return "A conexão foi interrompida. Confira a internet e tente novamente."
    if any(term in text for term in ("no space", "disk full", "espaço insuficiente")):
        return "Não há espaço livre suficiente na biblioteca de mídia."
    if any(term in text for term in ("http 500", "http 502", "http 503", "http 504")):
        return "O serviço do Minute está instável. Aguarde e tente novamente."
    if "http 400" in text or "bad request" in text:
        return "O serviço recusou este envio. Revise a conta e a categoria selecionada."
    if any(term in text for term in (
            "clip", "video", "vídeo", "manifest", "duração", "duration",
            "ffmpeg", "ffprobe")):
        return "Não foi possível preparar este vídeo. O QMoney o preservou e seguirá para o próximo."
    return "O envio não foi concluído após as tentativas automáticas. Valide a conta e tente novamente."


def _public_event(kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Cria um marco legível para a linha do tempo; eventos ruidosos somem."""
    email = str(payload.get("email") or "").strip()
    if kind == "campaign_start":
        accounts = len(payload.get("accounts") or [])
        tasks = len(payload.get("tasks") or [])
        return {
            "level": "info", "stage": "Início", "title": "Campanha iniciada",
            "detail": f"{accounts} conta(s) · {tasks} categoria(s) selecionada(s)",
        }
    if kind == "task_start":
        name = str(payload.get("task_name") or payload.get("scenario") or "Categoria")
        count = int(payload.get("count") or 0)
        return {
            "level": "info", "stage": "Conteúdo", "title": name,
            "detail": f"Selecionando {count} vídeo(s) compatível(is)",
        }
    if kind == "sent_reset":
        return {
            "level": "warning", "stage": "Conteúdo",
            "title": "Catálogo reutilizado",
            "detail": "Todos os vídeos elegíveis já haviam sido usados; a seleção recomeçou.",
        }
    if kind == "task_error":
        return {
            "level": "error", "stage": "Conteúdo",
            "title": "Categoria ignorada",
            "detail": "Não foi possível selecionar um vídeo compatível para esta categoria.",
        }
    if kind == "delay_start":
        return {
            "level": "info", "stage": "Intervalo", "title": "Intervalo programado",
            "detail": f"Próximo vídeo em aproximadamente {_fmt_wait(int(payload.get('delay_s') or 0))}",
        }
    if kind == "clip_prepare_start":
        duration = int(float(payload.get("dur_s") or 0))
        return {
            "level": "info", "stage": "Preparação", "title": "Preparando vídeo",
            "detail": f"Duração aproximada: {_fmt_wait(duration)}",
        }
    if kind == "clip_prepare_done" and not payload.get("ok"):
        return {
            "level": "warning", "stage": "Preparação", "title": "Vídeo ignorado",
            "detail": friendly_campaign_error(payload.get("error")),
        }
    if kind == "clip_ready":
        duration = int(float(payload.get("duration_ms") or 0) / 1000)
        return {
            "level": "success", "stage": "Preparação", "title": "Vídeo pronto",
            "detail": f"{_fmt_wait(duration)} · vídeo e sensores preparados",
        }
    if kind == "account_start":
        return {
            "level": "info", "stage": "Envio", "title": "Envio iniciado",
            "detail": email or "Conta selecionada",
        }
    if kind == "account_retry":
        attempt = int(payload.get("attempt") or 0)
        maximum = int(payload.get("max_attempts") or 0)
        return {
            "level": "warning", "stage": "Recuperação", "title": "Nova tentativa agendada",
            "detail": (f"{email} · tentativa {attempt + 1} de {maximum} em "
                       f"{_fmt_wait(int(payload.get('delay_s') or 0))}"),
        }
    if kind == "account_done":
        skipped = bool(payload.get("skipped"))
        ok = bool(payload.get("ok"))
        if skipped:
            return {
                "level": "warning", "stage": "Envio", "title": "Conta ignorada",
                "detail": f"{email} · {friendly_campaign_error(payload.get('error')) if payload.get('error') else 'este vídeo já foi processado'}",
            }
        if ok:
            return {
                "level": "success", "stage": "Envio", "title": "Envio concluído",
                "detail": email,
            }
        return {
            "level": "error", "stage": "Envio", "title": "Envio não concluído",
            "detail": f"{email} · {friendly_campaign_error(payload.get('error'))}",
        }
    if kind == "recording_wait_start":
        return {
            "level": "info", "stage": "Gravação", "title": "Linha do tempo da conta",
            "detail": (f"{email} · simulação de gravação por "
                       f"{_fmt_wait(int(float(payload.get('delay_s') or 0)))}"),
        }
    if kind == "window_wait_start":
        hours = payload.get("active_hours") or [0, 0]
        return {
            "level": "warning", "stage": "Aguardando", "title": "Fora do horário de envio",
            "detail": f"A campanha retomará dentro da janela {hours[0]}h–{hours[1]}h.",
        }
    if kind == "item_incomplete":
        return {
            "level": "error", "stage": "Envio", "title": "Lote incompleto",
            "detail": "Uma ou mais contas não concluíram o envio; o vídeo foi preservado.",
        }
    if kind == "storage_cleanup":
        files = int(payload.get("files") or 0)
        mib = float(payload.get("bytes") or 0) / (1024 ** 2)
        return {
            "level": "info", "stage": "Organização", "title": "Espaço liberado",
            "detail": f"{files} arquivo(s) temporário(s) removido(s) · {mib:.1f} MB",
        }
    if kind == "campaign_stopping":
        return {
            "level": "warning", "stage": "Encerrando", "title": "Parada solicitada",
            "detail": "O envio atual será concluído antes de encerrar.",
        }
    if kind == "campaign_stopped":
        return {
            "level": "warning", "stage": "Encerrada", "title": "Campanha encerrada",
            "detail": "A operação foi parada com segurança.",
        }
    if kind == "campaign_done":
        return {
            "level": "success", "stage": "Concluída", "title": "Campanha concluída",
            "detail": "Todos os resultados foram salvos no Histórico.",
        }
    if kind == "campaign_error":
        return {
            "level": "error", "stage": "Falha", "title": "Campanha interrompida",
            "detail": friendly_campaign_error(payload.get("error")),
        }
    # Logs brutos, contadores por segundo e fases internas não entram no feed.
    return None


class CampaignRunner:
    """Thread de campanha + buffer de eventos + parada cooperativa."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = "idle"  # idle|running|stopping|done|stopped|error
        self.events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)
        self._seq = 0
        self.error: str | None = None
        self.log_path: str | None = None
        self.total_sends = 0
        self.done_sends = 0
        self.ok_sends = 0
        self.failed_sends = 0
        self.skipped_sends = 0
        self.current = ""  # descrição da atividade atual (p/ a barra de status)
        self.stage = "Aguardando"

    # --- ciclo de vida ----------------------------------------------------
    @property
    def running(self) -> bool:
        return self.state in ("running", "stopping")

    def start(self, cfg: CampaignConfig) -> None:
        """Inicia a campanha numa thread de fundo. Erro se já houver uma rodando."""
        with self._lock:
            if self.running:
                raise RuntimeError("já há uma campanha em andamento")
            self._stop.clear()
            self.state = "running"
            self.events.clear()
            self._seq = 0
            self.error = None
            self.log_path = None
            self.done_sends = 0
            self.ok_sends = 0
            self.failed_sends = 0
            self.skipped_sends = 0
            self.current = "iniciando…"
            self.stage = "Início"
            n_acc = max(1, len(cfg.accounts))
            hours = float(getattr(cfg, "target_hours_per_account", 0) or 0)
            if hours > 0:
                # estimativa: sessão média ~15 min até a campanha reportar o real
                per = max(1, int(hours * 3600 / 900 + 0.999))
                self.total_sends = per * n_acc
            else:
                total = 0
                for task in cfg.tasks:
                    n = len(task.clip_uids) if task.clip_uids else task.count
                    if getattr(cfg, "share_clips", True):
                        total += n * n_acc
                    elif task.clip_uids:
                        total += n
                    else:
                        total += n * n_acc
                self.total_sends = total
            self._thread = threading.Thread(target=self._run, args=(cfg,),
                                            daemon=True, name="moneymin-campaign")
            self._thread.start()

    def stop(self) -> None:
        """Pede parada cooperativa (o motor para entre envios e no delay)."""
        requested = False
        with self._lock:
            if self.state == "running":
                self.state = "stopping"
                self._stop.set()
                self.stage = "Encerrando"
                requested = True
        if requested:
            self._record("campaign_stopping")

    # --- thread de fundo ----------------------------------------------------
    def _run(self, cfg: CampaignConfig) -> None:
        try:
            run_campaign(cfg, progress=self._on_event,
                         should_stop=self._stop.is_set)
        except Exception as exc:  # noqa: BLE001 — reporta qualquer falha na UI
            with self._lock:
                self.state = "error"
                self.error = friendly_campaign_error(exc)
                self.current = ""
                self.stage = "Falha"
            self._record("campaign_error", error=exc)
        else:
            # Defesa final: mesmo que uma implementação/customização do motor
            # retorne sem emitir evento terminal, a interface nunca fica presa
            # eternamente em running/stopping.
            with self._lock:
                if self.state == "running":
                    self.state = "done"
                    self.current = ""
                    self.stage = "Concluída"
                elif self.state == "stopping":
                    self.state = "stopped"
                    self.current = ""
                    self.stage = "Encerrada"

    def _on_event(self, kind: str, payload: dict[str, Any]) -> None:
        self._record(kind, **payload)
        with self._lock:
            if kind == "account_done":
                self.done_sends += 1
                if payload.get("skipped"):
                    self.skipped_sends += 1
                elif payload.get("ok"):
                    self.ok_sends += 1
                else:
                    self.failed_sends += 1
            elif kind == "campaign_done":
                self.log_path = payload.get("log_path")
                if self.state != "stopped":
                    self.state = "done"
                self.current = ""
                self.stage = "Concluída"
            elif kind == "campaign_stopped":
                self.state = "stopped"
                self.current = ""
                self.stage = "Encerrada"
            elif kind == "task_start":
                scen = str(payload.get("scenario") or "")
                name = str(payload.get("task_name") or
                           campaign.SCENARIO_PT.get(scen, scen))
                self.current = f"categoria: {name}"
                self.stage = "Conteúdo"
            elif kind == "clip_prepare_start":
                self.current = (f"preparando clipe {str(payload.get('clip_uid'))[:12]} "
                                f"({payload.get('dur_s')}s)…")
                self.stage = "Preparação"
            elif kind == "clip_prepare_progress":
                phase = str(payload.get("phase") or "")
                labels = {
                    "video_lookup": "localizando vídeo licenciado",
                    "video_index": "catalogando offsets do HoloAssist",
                    "video_download": "baixando somente o vídeo selecionado",
                    "video_cached": "vídeo HoloAssist encontrado no cache",
                    "video_ready": "vídeo-fonte pronto",
                    "imu_lookup": "localizando sensores licenciados",
                    "imu_preflight": "validando continuidade dos sensores",
                    "imu_index": "catalogando offsets dos sensores",
                    "imu_download": "baixando somente os sensores selecionados",
                    "imu_cached": "sensores encontrados no cache",
                    "imu_ready": "sensores prontos",
                    "encode": "codificando vídeo na RTX",
                    "encode_ready": "codificação concluída",
                    "sidecar": "montando sidecar de sensores",
                }
                label = labels.get(phase, "preparando conteúdo")
                current = int(payload.get("current") or 0)
                total = int(payload.get("total") or 0)
                suffix = f" — {current * 100 // total}%" if total > 0 else ""
                self.current = f"{label}{suffix}…"
                self.stage = "Preparação"
            elif kind == "account_start":
                self.current = f"enviando para {payload.get('email')}…"
                self.stage = "Envio"
            elif kind == "storage_cleanup":
                freed = float(payload.get("bytes") or 0) / (1024 ** 2)
                self.current = (
                    f"liberando espaço: {payload.get('files') or 0} arquivo(s), "
                    f"{freed:.1f} MB"
                )
                self.stage = "Organização"
            elif kind == "account_progress":
                phase = str(payload.get("phase") or "envio")
                phase_pt = {
                    "encode": "preparando vídeo da conta",
                    "create/sas": "abrindo envio",
                    "create": "registrando envio",
                    "transport": "subindo vídeo",
                    "complete": "confirmando envio",
                    "sidecar": "preparando sensores",
                }.get(phase, "processando envio")
                suffix = ""
                if phase == "transport" and payload.get("total_bytes"):
                    percent = float(payload.get("percent") or 0.0)
                    speed_mbps = float(payload.get("speed_bps") or 0.0) * 8 / 1_000_000
                    eta_s = int(payload.get("eta_s") or 0)
                    suffix = f" — {percent:.0f}% · {speed_mbps:.1f} Mbps"
                    if eta_s > 0:
                        suffix += f" · faltam {_fmt_wait(eta_s)}"
                self.current = f"{payload.get('email')}: {phase_pt}{suffix}…"
                self.stage = "Envio"
            elif kind == "batch_tick":
                pending = int(payload.get("pending_accounts") or 0)
                elapsed = int(payload.get("elapsed_s") or 0)
                self.current = (f"envios ativos: {pending} — "
                                f"{_fmt_wait(elapsed)} decorridos…")
                self.stage = "Envio"
            elif kind == "account_retry_tick":
                self.current = ("recuperando envio — nova tentativa em "
                                f"{_fmt_wait(int(payload.get('remaining_s') or 0))}…")
                self.stage = "Recuperação"
            elif kind == "recording_wait_start":
                self.current = (
                    f"{payload.get('email')}: gravando vídeo — "
                    f"{_fmt_wait(int(payload.get('delay_s') or 0))}…")
                self.stage = "Gravação"
            elif kind == "recording_wait_tick":
                pending = int(payload.get("pending_accounts") or 0)
                remaining = _fmt_wait(int(payload.get("remaining_s") or 0))
                if pending > 1:
                    self.current = (f"{pending} contas: gravação em paralelo — "
                                    f"próxima liberação em {remaining}…")
                else:
                    account = payload.get("email") or "conta"
                    self.current = (f"{account}: gravação em andamento — "
                                    f"faltam {remaining}…")
                self.stage = "Gravação"
            elif kind == "delay_tick":
                self.current = (f"intervalo entre vídeos: "
                                f"{_fmt_wait(int(payload.get('remaining_s') or 0))}…")
                self.stage = "Intervalo"
            elif kind == "account_gap_start":
                self.current = (f"intervalo entre contas: "
                                f"{_fmt_wait(int(payload.get('delay_s') or 0))}…")
                self.stage = "Intervalo"
            elif kind == "account_gap_tick":
                self.current = (f"intervalo entre contas: "
                                f"{_fmt_wait(int(payload.get('remaining_s') or 0))}…")
                self.stage = "Intervalo"
            elif kind == "window_wait_start":
                h = payload.get("active_hours") or [0, 0]
                self.current = f"fora do horário de envio ({h[0]}h–{h[1]}h)…"
                self.stage = "Aguardando"
            elif kind == "window_wait_tick":
                self.current = ("fora do horário de envio — retoma em "
                                f"{_fmt_wait(int(payload.get('remaining_s') or 0))}…")
                self.stage = "Aguardando"

    # --- consulta -------------------------------------------------------------
    def snapshot(self, since: int = 0) -> dict[str, Any]:
        """Estado atual p/ polling. `since` devolve só eventos com seq > since."""
        with self._lock:
            events = [e for e in self.events if e["seq"] > since]
            return {
                "state": self.state,
                "events": events,
                "last_seq": self._seq,
                "totals": {
                    "total_sends": self.total_sends,
                    "done_sends": self.done_sends,
                    "ok_sends": self.ok_sends,
                    "failed_sends": self.failed_sends,
                    "skipped_sends": self.skipped_sends,
                },
                "current": self.current,
                "stage": self.stage,
                "log_path": self.log_path,
                "error": self.error,
            }

    def _record(self, kind: str, **payload: Any) -> None:
        event = _public_event(kind, payload)
        if event is None:
            return
        with self._lock:
            self._seq += 1
            self.events.append({"seq": self._seq, "ts": time.time(),
                                "kind": kind, **event})


# Runner único por processo (uma campanha por vez).
RUNNER = CampaignRunner()


class BalancesRunner:
    """Consulta saldos pela API em paralelo, com fallback de navegador serial.

    O caminho normal não abre navegador. Se a API falhar para uma conta, apenas
    ela usa o fluxo antigo; fallbacks são seriais para não abrir vários Chromes.
    Os resultados são persistidos a cada conta via callback.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state = "idle"  # idle|running|done|error
        self.current = ""
        self.total = 0
        self.done = 0
        self.fast_done = 0
        self.fallbacks = 0

    @property
    def running(self) -> bool:
        return self.state == "running"

    def start(self, creds: dict[str, str], on_result) -> None:
        """creds: {email: senha}. on_result(email, summary|None, erro|None)."""
        with self._lock:
            if self.running:
                raise RuntimeError("já há uma consulta de saldos em andamento")
            self.state = "running"
            self.total = len(creds)
            self.done = 0
            self.fast_done = 0
            self.fallbacks = 0
            self.current = "iniciando consulta rápida…"
            self._thread = threading.Thread(target=self._run, args=(creds, on_result),
                                            daemon=True, name="moneymin-balances")
            self._thread.start()

    def _run(self, creds: dict[str, str], on_result) -> None:
        # Import tardio: Playwright só é carregado se algum fallback for necessário.
        from ..crowtado import consultar_saldo_api, consultar_saldo_navegador

        try:
            failures: list[tuple[str, str, Exception]] = []
            workers = min(6, max(1, len(creds)))
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="moneymin-balance") as pool:
                futures = {
                    pool.submit(consultar_saldo_api, email, senha): (email, senha)
                    for email, senha in creds.items()
                }
                with self._lock:
                    self.current = f"consultando {len(creds)} conta(s) em paralelo…"
                for future in as_completed(futures):
                    email, senha = futures[future]
                    try:
                        summary = future.result()
                    except Exception as exc:  # noqa: BLE001 — tenta fallback depois
                        failures.append((email, senha, exc))
                        continue
                    on_result(email, summary, None)
                    with self._lock:
                        self.done += 1
                        self.fast_done += 1

            for fallback_index, (email, senha, api_error) in enumerate(failures, 1):
                with self._lock:
                    self.fallbacks = len(failures)
                    self.current = (f"fallback pelo navegador {fallback_index}/"
                                    f"{len(failures)}: {email}…")
                try:
                    summary = consultar_saldo_navegador(email, senha, headed=False)
                    on_result(email, summary, None)
                except Exception as browser_error:  # noqa: BLE001 — uma conta falha, segue
                    on_result(
                        email, None,
                        f"API: {type(api_error).__name__}: {api_error}; "
                        f"navegador: {type(browser_error).__name__}: {browser_error}",
                    )
                with self._lock:
                    self.done += 1
            with self._lock:
                self.state = "done"
                self.current = ""
        except Exception:  # noqa: BLE001
            with self._lock:
                self.state = "error"
                self.current = ""
            raise

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self.state, "current": self.current,
                    "total": self.total, "done": self.done,
                    "fast_done": self.fast_done, "fallbacks": self.fallbacks}


# Runner único de saldos: APIs paralelas; no máximo um navegador de fallback.
BALANCES_RUNNER = BalancesRunner()


class HoloCacheRunner:
    """Pré-cache HoloAssist retomável em uma thread de fundo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state = "idle"  # idle|running|stopping|done|stopped|disk_limit|error
        self.current = ""
        self.phase = ""
        self.index = 0
        self.total = 0
        self.ready = 0
        self.failed = 0
        self.error: str | None = None
        self.result: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return self.state in ("running", "stopping")

    def start(
        self,
        *,
        task: str,
        min_dur_s: float = 60,
        max_dur_s: float = 1800,
        limit: int | None = None,
        min_free_gb: float = 50,
    ) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("o acelerador HoloAssist já está em andamento")
            self.state = "running"
            self.current = "preparando catálogo…"
            self.phase = "catalog"
            self.index = 0
            self.total = 0
            self.ready = 0
            self.failed = 0
            self.error = None
            self.result = None
            kwargs = {
                "task": task,
                "min_dur_s": min_dur_s,
                "max_dur_s": max_dur_s,
                "limit": limit,
                "min_free_gb": min_free_gb,
            }
            self._thread = threading.Thread(
                target=self._run,
                kwargs=kwargs,
                daemon=True,
                name="moneymin-holo-cache",
            )
            self._thread.start()

    def stop(self) -> None:
        requested = False
        with self._lock:
            if self.state == "running":
                self.state = "stopping"
                self.current = "parada solicitada — concluindo o clipe atual…"
                requested = True
        if requested:
            holo_accelerator.request_stop()

    def _run(self, **kwargs: Any) -> None:
        try:
            result = holo_accelerator.warm_cache(
                **kwargs,
                progress=self._on_event,
            )
        except Exception as exc:  # noqa: BLE001 — erro precisa aparecer na interface
            with self._lock:
                self.state = "error"
                self.error = f"{type(exc).__name__}: {exc}"
                self.current = ""
                self.phase = ""
            return

        terminal = str(result.get("status") or "complete")
        state_map = {
            "complete": "done",
            "stopped": "stopped",
            "disk_limit": "disk_limit",
        }
        with self._lock:
            self.result = dict(result)
            self.state = state_map.get(terminal, terminal)
            self.index = int(result.get("index") or self.index)
            self.total = int(result.get("total") or self.total)
            self.ready = int(result.get("ready") or 0)
            self.failed = int(result.get("failed") or 0)
            self.current = ""
            self.phase = ""

    def _on_event(self, kind: str, payload: dict[str, Any]) -> None:
        labels = {
            "video_lookup": "localizando vídeo",
            "video_index": "lendo índice de vídeo",
            "video_download": "baixando vídeo",
            "video_cached": "vídeo já estava no cache",
            "video_ready": "vídeo-fonte pronto",
            "imu_lookup": "localizando sensores",
            "imu_index": "lendo índice de sensores",
            "imu_download": "baixando sensores",
            "imu_cached": "sensores já estavam no cache",
            "imu_ready": "sensores prontos",
            "encode": "normalizando vídeo",
            "encode_ready": "vídeo normalizado",
            "sidecar": "preparando sensores",
        }
        with self._lock:
            self.index = int(payload.get("index") or self.index)
            self.total = int(payload.get("total") or self.total)
            name = str(payload.get("video_name") or "")
            if kind == "cached":
                self.ready += 1
                self.phase = "cached"
                self.current = f"cache confirmado: {name}"
            elif kind == "start":
                self.phase = "download"
                self.current = f"preparando {name}…"
            elif kind == "phase":
                phase = str(payload.get("phase") or "")
                self.phase = phase
                label = labels.get(phase, phase or "preparando")
                phase_current = int(payload.get("phase_current") or 0)
                phase_total = int(payload.get("phase_total") or 0)
                suffix = (f" — {phase_current * 100 // phase_total}%"
                          if phase_total > 0 else "")
                self.current = f"{name}: {label}{suffix}…"
            elif kind == "done":
                self.ready += 1
                self.phase = "done"
                self.current = f"pronto: {name}"
            elif kind == "failed":
                self.failed += 1
                self.phase = "failed"
                self.current = f"falhou: {name} — {payload.get('error', '')}"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "current": self.current,
                "phase": self.phase,
                "index": self.index,
                "total": self.total,
                "ready": self.ready,
                "failed": self.failed,
                "error": self.error,
                "result": self.result,
            }


# O pré-cache e a campanha compartilham disco, rede e FFmpeg; o servidor impede
# que os dois runners pesados sejam iniciados ao mesmo tempo.
HOLO_CACHE_RUNNER = HoloCacheRunner()
