"""
crowtado.py — Login no crowtado (Clerk FAPI) e consulta de saldo.

O crowtado usa Clerk (clerk.crowtado.com). O sign-in por senha NÃO exige
captcha (diferente do sign-up), então o login é 100% por API, sem navegador:

  1. POST /v1/client                    -> cria o client (cookie __client)
  2. POST /v1/client/sign_ins           -> identifier+password (strategy=password)
  3. POST /v1/client/sessions/{id}/tokens -> JWT de sessão (~60s de vida)
  4. GET /api/trpc/payouts.summary com Cookie __session=<jwt>

Como o JWT expira rápido, a sessão Clerk em memória emite um token novo sem
refazer o login. Somente o fallback de compatibilidade abre um navegador.

O sign-up exige captcha (Cloudflare Turnstile via Clerk), então `criar_conta`
usa um Chrome REAL lançado com `--remote-debugging-port` (perfil persistente em
`data/chrome-crowtado/`) controlado via CDP — navegador lançado pelo Playwright
não recebe token do Turnstile (navigator.webdriver=true). A verificação de
email é lida da caixa catch-all da Hostinger (moneymin.hostinger_mail).

Exemplo:
    from moneymin.crowtado import consultar_saldo_api
    print(consultar_saldo_api("user@example.com", "senha"))
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import config

CLERK_BASE = "https://clerk.crowtado.com"
SITE_BASE = "https://www.crowtado.com"
EARNINGS_PATH = "/pt-BR/dashboard/earnings"
_CLERK_QS = "_clerk_js_version=5.88.0&__clerk_api_version=2024-10-01"
_ORIGIN = {"Origin": SITE_BASE, "Referer": SITE_BASE + "/"}

# Perfil persistente do Chrome real usado no sign-up (turnstile confia mais
# num perfil "vivido" — cookies/histórico acumulam entre execuções).
CHROME_PROFILE = config.DATA_DIR / "chrome-crowtado"
# Código de indicação padrão (aplicado na URL de sign-up). Fonte única:
# `config.CROWTADO_REF` — IMUTÁVEL, fixo no código.
DEFAULT_REF = config.CROWTADO_REF


class CrowtadoError(RuntimeError):
    """Falha de login ou consulta no crowtado."""


class CrowtadoSession:
    """Sessão autenticada: cookie jar do Clerk + JWT de sessão renovável."""

    def __init__(self, opener: urllib.request.OpenerDirector, session_id: str):
        self.opener = opener
        self.session_id = session_id

    def _fapi(self, path: str, data: dict[str, str] | None = None) -> tuple[int, Any]:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(
            f"{CLERK_BASE}{path}?{_CLERK_QS}", data=body, method="POST" if data else "GET"
        )
        for k, v in _ORIGIN.items():
            req.add_header(k, v)
        # Cloudflare (erro 1010) bloqueia o UA padrão do urllib — usar UA neutro.
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0")
        if body:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return exc.code, text

    def session_jwt(self) -> str:
        """JWT de sessão fresco (lido do last_active_token do client, ~60s de vida)."""
        status, body = self._fapi("/v1/client")
        client = (body.get("response") or {}) if isinstance(body, dict) else {}
        for s in client.get("sessions") or []:
            if s.get("id") == self.session_id or s.get("status") == "active":
                jwt = (s.get("last_active_token") or {}).get("jwt")
                if jwt:
                    return jwt
        raise CrowtadoError(f"sessão sem token ativo ({status}): {str(body)[:200]}")


_SESSION_LOCK = threading.Lock()
_SESSION_CACHE: dict[str, tuple[str, CrowtadoSession]] = {}


def _password_fingerprint(password: str) -> str:
    """Identifica troca de senha sem guardar outra cópia dela na memória."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def clear_cached_session(email: str | None = None) -> None:
    """Descarta uma sessão específica ou todo o cache autenticado em memória."""
    with _SESSION_LOCK:
        if email is None:
            _SESSION_CACHE.clear()
        else:
            _SESSION_CACHE.pop(email, None)


def _cached_login(email: str, password: str) -> CrowtadoSession:
    """Reaproveita o client Clerk enquanto ele ainda consegue emitir um JWT."""
    fingerprint = _password_fingerprint(password)
    with _SESSION_LOCK:
        cached = _SESSION_CACHE.get(email)
    if cached and cached[0] == fingerprint:
        try:
            cached[1].session_jwt()
            return cached[1]
        except CrowtadoError:
            clear_cached_session(email)
    session = login(email, password)
    with _SESSION_LOCK:
        _SESSION_CACHE[email] = (fingerprint, session)
    return session


def login(email: str, password: str) -> CrowtadoSession:
    """Autentica no Clerk por senha. Levanta CrowtadoError se falhar.

    Se a conta pedir segundo fator por email (email_code), busca o código na
    caixa catch-all da Hostinger (moneymin.hostinger_mail) e confirma.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    sess = CrowtadoSession(opener, "")

    status, body = sess._fapi("/v1/client", {})
    if status != 200:
        raise CrowtadoError(f"criação de client falhou ({status}): {str(body)[:200]}")

    status, body = sess._fapi(
        "/v1/client/sign_ins",
        {"identifier": email, "password": password, "strategy": "password"},
    )
    sign_in = ((body.get("response") if isinstance(body, dict) else None) or {})
    si_status = sign_in.get("status")

    if si_status == "needs_second_factor":
        sid_sign_in = sign_in.get("id")
        fatores = {f.get("strategy") for f in sign_in.get("supported_second_factors") or []}
        if "email_code" not in fatores:
            raise CrowtadoError(f"2º fator não suportado por este cliente: {fatores}")
        # Snapshot ANTES de pedir o código: ignora emails antigos na caixa.
        from .hostinger_mail import max_uid, wait_for_code

        uid_base = max_uid(email)
        status, body = sess._fapi(
            f"/v1/client/sign_ins/{sid_sign_in}/prepare_second_factor",
            {"strategy": "email_code"},
        )
        if status != 200:
            raise CrowtadoError(f"prepare_second_factor falhou ({status}): {str(body)[:200]}")
        # Código chega no email da conta (caixa catch-all da Hostinger).
        code = wait_for_code(email, sender="crowtado.com", min_uid=uid_base, timeout=180)
        status, body = sess._fapi(
            f"/v1/client/sign_ins/{sid_sign_in}/attempt_second_factor",
            {"strategy": "email_code", "code": code},
        )
        sign_in = ((body.get("response") if isinstance(body, dict) else None) or {})
        si_status = sign_in.get("status")

    sid = sign_in.get("created_session_id")
    if status != 200 or si_status != "complete" or not sid:
        erros = body.get("errors") if isinstance(body, dict) else None
        detalhe = (erros[0].get("long_message") if erros else str(body)[:200])
        raise CrowtadoError(f"login falhou ({status}): {detalhe}")
    sess.session_id = sid
    return sess


_SUMMARY_RE = re.compile(
    r'availableCents[\\]*":(\d+)'
    r'.*?inTransitCents[\\]*":(\d+)'
    r'.*?lifetimeCents[\\]*":(\d+)'
    r'.*?pendingCents[\\]*":(\d+)',
    re.S,
)


def _extrai_summary(texto: str) -> dict[str, int]:
    """Extrai o payouts.summary de um payload flight RSC (com escapes ou não)."""
    m = _SUMMARY_RE.search(texto)
    if m:
        return {
            "availableCents": int(m.group(1)),
            "inTransitCents": int(m.group(2)),
            "lifetimeCents": int(m.group(3)),
            "pendingCents": int(m.group(4)),
        }
    return {}


def _summary_from_payload(payload: Any) -> dict[str, int]:
    """Normaliza as formas direta/aninhada usadas pelo payouts.summary."""
    required = ("availableCents", "inTransitCents", "lifetimeCents", "pendingCents")
    if isinstance(payload, dict):
        if all(name in payload for name in required):
            try:
                return {name: int(payload[name]) for name in required}
            except (TypeError, ValueError) as exc:
                raise CrowtadoError("payouts.summary devolveu valores inválidos") from exc
        for name in ("summary", "payouts", "data"):
            if name in payload:
                summary = _summary_from_payload(payload[name])
                if summary:
                    return summary
    elif isinstance(payload, list):
        for item in payload:
            summary = _summary_from_payload(item)
            if summary:
                return summary
    return {}


def consultar_saldo_api(email: str, senha: str) -> dict[str, int]:
    """Consulta o saldo pela API tRPC, sem iniciar navegador."""
    session = _cached_login(email, senha)
    payload = _site_trpc(session, "payouts.summary", None, method="GET")
    summary = _summary_from_payload(payload)
    if not summary:
        raise CrowtadoError(f"payouts.summary sem saldo reconhecível: {str(payload)[:200]}")
    return summary


_WITHDRAW_RESULT_FIELDS = {
    "status",
    "amountCents",
    "currency",
    "thresholdCents",
    "minimumCents",
    "holdReason",
    "failureReason",
    "dotsEmailDelivery",
    "dotsSmsDelivery",
    "rail",
}


def solicitar_link_saque(email: str, senha: str) -> dict[str, Any]:
    """Solicita ao Crowtado/Dots o link para sacar todo o saldo disponível.

    Replica o botão da página de ganhos (`payouts.withdraw`, método `dots`).
    A chamada apenas cria a solicitação e pede o envio do link pelo provedor;
    confirmação, 2FA e dados de pagamento continuam fora deste cliente.
    """
    session = _cached_login(email, senha)
    payload = _site_trpc(
        session, "payouts.withdraw", {"method": "dots"}, method="POST")
    if not isinstance(payload, dict) or not payload.get("status"):
        raise CrowtadoError(
            f"payouts.withdraw devolveu resposta inválida: {str(payload)[:200]}")
    # Não repassa links/tokens ou campos novos desconhecidos para a interface.
    return {key: value for key, value in payload.items()
            if key in _WITHDRAW_RESULT_FIELDS}


def consultar_saldo_navegador(
        email: str, senha: str, headed: bool = True) -> dict[str, int]:
    """Login no crowtado pelo navegador e leitura do saldo (available/pending).

    O sign-in NÃO tem captcha (diferente do sign-up): preenche email+senha no
    formulário do Clerk e, se pedir 2º fator, busca o código na caixa catch-all
    da Hostinger e digita. Depois abre /dashboard/earnings e captura a resposta
    do tRPC payouts.summary (availableCents, pendingCents, ...).

    headed=True (default) porque o Cloudflare barra headless com mais frequência.
    Devolve o dict do summary. Levanta CrowtadoError em falha.
    """
    from playwright.sync_api import sync_playwright

    from .hostinger_mail import max_uid, wait_for_code

    summary: dict[str, Any] = {}

    def _on_response(resp) -> None:
        if summary:
            return
        try:
            if "payouts.summary" in resp.url:
                body = resp.json()
                for item in (body if isinstance(body, list) else [body]):
                    data = ((item or {}).get("result") or {}).get("data") or {}
                    payload = data.get("json") or {}
                    if isinstance(payload, dict) and "availableCents" in payload:
                        summary.update(payload)
            elif "dashboard/earnings" in resp.url and "_rsc" in resp.url:
                summary.update(_extrai_summary(resp.text()))
        except Exception:  # noqa: BLE001 — resposta ilegível, segue o fluxo
            pass

    with sync_playwright() as pw:
        launch = {"channel": "chrome", "headless": not headed,
                  "args": ["--disable-blink-features=AutomationControlled"]}
        try:
            browser = pw.chromium.launch(**launch)
        except Exception:  # noqa: BLE001 — sem Chrome instalado: cai p/ Chromium
            launch.pop("channel")
            browser = pw.chromium.launch(**launch)
        try:
            page = browser.new_context(locale="pt-BR").new_page()
            print("[*] abrindo sign-in do crowtado...")
            page.goto(SITE_BASE + "/pt-BR/sign-in", wait_until="domcontentloaded")

            page.wait_for_selector("#identifier-field", timeout=60_000)
            page.fill("#identifier-field", email)
            uid_base = max_uid(email)
            page.click("button.cl-formButtonPrimary")

            page.wait_for_selector("#password-field", timeout=60_000)
            page.fill("#password-field", senha)
            page.click("button.cl-formButtonPrimary")

            # Espera a transição: ou sai do sign-in (login direto), ou cai na
            # tela de 2º fator ("novo dispositivo", código por email).
            # Re-clica no botão se travar em factor-one (validação assíncrona).
            destino = None
            for _ in range(6):
                page.wait_for_timeout(5000)
                url = page.url
                if "/sign-in" not in url:
                    destino = "direto"
                    break
                if "factor-two" in url:
                    destino = "2fa"
                    break
                if page.query_selector("button.cl-formButtonPrimary"):
                    page.click("button.cl-formButtonPrimary")
            if not destino:
                raise CrowtadoError(
                    f"após a senha a página não avançou (url={page.url.split('?')[0]}): "
                    f"{page.inner_text('body')[:200]}"
                )
            print(f"[*] pós-senha: {destino} — {page.url.split('?')[0]}")

            if destino == "2fa":
                # 2FA por email: campo OTP único (data-input-otp) ou digit-N.
                page.wait_for_selector('input[data-input-otp], input[id^="digit-"]',
                                       timeout=30_000)
                print(f"[*] 2FA: aguardando código no email {email} ...")
                code = wait_for_code(email, sender="crowtado.com",
                                     min_uid=uid_base, timeout=180)
                print(f"[+] código recebido: {code}")
                if page.query_selector('input[id^="digit-"]'):
                    for i, digit in enumerate(code):
                        page.fill(f"#digit-{i}-field", digit)
                else:
                    page.locator('input[data-input-otp]').press_sequentially(code, delay=80)

            page.wait_for_url(lambda url: "/sign-in" not in url, timeout=60_000)
            print(f"[+] login OK — url: {page.url.split('?')[0]}")

            page.on("response", _on_response)
            page.goto(SITE_BASE + EARNINGS_PATH, wait_until="domcontentloaded")
            for _ in range(30):
                if summary:
                    break
                page.wait_for_timeout(1000)

            if not summary:
                # fallback: flight payload embutido no HTML (streaming SSR)
                summary.update(_extrai_summary(page.content()))
        finally:
            browser.close()

    if not summary:
        raise CrowtadoError("não capturei o payouts.summary (layout/rede mudou?).")
    return summary


def consultar_saldo(
        email: str, senha: str, headed: bool = True, *,
        fallback_browser: bool = True) -> dict[str, int]:
    """Consulta rápida por API e, opcionalmente, recorre ao navegador."""
    try:
        return consultar_saldo_api(email, senha)
    except CrowtadoError as api_error:
        if not fallback_browser:
            raise
        try:
            return consultar_saldo_navegador(email, senha, headed=headed)
        except Exception as browser_error:  # noqa: BLE001 — preserva os dois diagnósticos
            raise CrowtadoError(
                f"API falhou ({api_error}); navegador falhou ({browser_error})"
            ) from browser_error


# --- vínculo do email Minute (externalMobileCapture) ---------------------------

# Task "egocentric-household-mobile-latam" (fixa — é a única com capture externo).
TASK_ID_MINUTE = "78d06f56-16f7-449c-8e31-684a1dac6b3e"


def _site_trpc(sess: CrowtadoSession, proc: str, payload: dict[str, Any] | None,
               method: str = "POST") -> Any:
    """Chama /api/trpc/<proc> em www.crowtado.com com o JWT de sessão.

    O middleware do Clerk exige __session (JWT fresco) + __client_uat — o
    __client_uat já vem no cookie jar do login, então o JWT é injetado no jar
    (em vez de header Cookie manual, que o substituiria).
    tRPC batch de 1: POST {"0": {"json": payload}} / GET ?input={"0":{"json":...}}.
    Devolve o "json" da resposta ou levanta CrowtadoError.
    """
    jar = next(h.cookiejar for h in sess.opener.handlers
               if isinstance(h, urllib.request.HTTPCookieProcessor))
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name="__session", value=sess.session_jwt(),
        port=None, port_specified=False,
        domain="www.crowtado.com", domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True, secure=True, expires=None, discard=True,
        comment=None, comment_url=None, rest={}, rfc2109=False))
    if method == "POST":
        url = f"{SITE_BASE}/api/trpc/{proc}?batch=1"
        body = json.dumps({"0": {"json": payload}}).encode()
    else:
        qs = urllib.parse.quote(json.dumps({"0": {"json": payload}}))
        url = f"{SITE_BASE}/api/trpc/{proc}?batch=1&input={qs}"
        body = None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent",
                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
    req.add_header("Accept", "*/*")
    req.add_header("Origin", SITE_BASE)
    req.add_header("Referer", SITE_BASE + "/pt-BR/dashboard/tasks")
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with sess.opener.open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise CrowtadoError(
            f"tRPC {proc} falhou ({exc.code}): "
            f"{exc.read().decode('utf-8', 'replace')[:200]}") from exc
    item = data[0] if isinstance(data, list) and data else data
    if isinstance(item, dict) and item.get("error"):
        raise CrowtadoError(f"tRPC {proc} erro: {str(item['error'])[:200]}")
    return (((item or {}).get("result") or {}).get("data") or {}).get("json")


# Gêneros aceitos pelo modal de demografia (gate obrigatório desde 26/08).
GENDERS = ("female", "male", "non_binary", "self_describe", "prefer_not_to_say")


def preencher_demografia(email: str, senha: str, birth_month: int, birth_year: int,
                         gender: str | None = None) -> dict[str, Any]:
    """Preenche o gate obrigatório de demografia (mês/ano de nascimento + gênero).

    Sem isso o site responde 412 DEMOGRAPHICS_REQUIRED
    ("Birth month, birth year, and gender are required") em qualquer tRPC de
    task — inclusive externalMobileCapture.saveEmail/myStatus. O gênero é
    opcional no backend para maior de idade, mas enviamos por realismo.
    Devolve o demographicsStatus (state == "complete"). Levanta CrowtadoError.
    """
    if not 1 <= birth_month <= 12:
        raise CrowtadoError(f"birth_month inválido: {birth_month}")
    if gender is not None and gender not in GENDERS:
        raise CrowtadoError(f"gender inválido: {gender} (use um de {GENDERS})")
    sess = login(email, senha)
    payload: dict[str, Any] = {"birthMonth": birth_month, "birthYear": birth_year}
    if gender:
        payload["gender"] = gender
    status = _site_trpc(sess, "profile.saveDemographics", payload)
    if isinstance(status, dict) and status.get("state") not in (None, "complete"):
        raise CrowtadoError(f"saveDemographics não completou (state={status.get('state')})")
    return status


def vincular_minute(email: str, senha: str, task_id: str = TASK_ID_MINUTE) -> dict[str, Any]:
    """Vincula o email do Minute na task do crowtado ('Conecte seu e-mail do Minute').

    Sem esse vínculo os envios do app Minute NÃO são creditados no crowtado.
    É a mutation externalMobileCapture.saveEmail — login por senha (API Clerk)
    basta, sem navegador. Depois confirma lendo externalMobileCapture.myStatus.
    Devolve o myStatus (linkedAt, emailVerifiedAt, ...). Levanta CrowtadoError.
    """
    sess = login(email, senha)
    _site_trpc(sess, "externalMobileCapture.saveEmail",
               {"taskId": task_id, "email": email, "locale": "pt-BR",
                "surface": "detail"})
    status = _site_trpc(sess, "externalMobileCapture.myStatus",
                        {"taskId": task_id, "locale": "pt-BR"}, method="GET")
    itens = status if isinstance(status, list) else [status]
    ok = any(isinstance(s, dict) and s.get("linkedAt")
             and (not s.get("taskId") or s.get("taskId") == task_id)
             for s in itens)
    if not ok:
        raise CrowtadoError(f"saveEmail não vinculou (myStatus={str(status)[:200]})")
    return status


# --- criação de conta (sign-up com Turnstile) ---------------------------------

def _chrome_exe() -> str:
    """Caminho do Chrome instalado. Erro claro se não achar."""
    import os
    import shutil
    import sys
    cands: list[str] = []
    if sys.platform == "win32":
        cands += [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    elif sys.platform == "darwin":
        cands.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for cand in cands:
        if os.path.exists(cand):
            return cand
    for name in ("chrome", "google-chrome", "google-chrome-stable",
                 "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    pw = _playwright_chrome()
    if pw:
        return pw
    raise CrowtadoError("Chrome não encontrado — o sign-up precisa de um Chrome real.")


def _playwright_chrome() -> str | None:
    """Chromium instalado por `playwright install chromium` (fallback ao Chrome)."""
    import glob
    import os
    import sys
    if sys.platform == "win32":
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
        pat = os.path.join(base, "chromium-*", "chrome-win", "chrome.exe")
    elif sys.platform == "darwin":
        pat = os.path.expanduser(
            "~/Library/Caches/ms-playwright/chromium-*/chrome-mac/"
            "Chromium.app/Contents/MacOS/Chromium")
    else:
        pat = os.path.expanduser(
            "~/.cache/ms-playwright/chromium-*/chrome-linux/chrome")
    matches = sorted(glob.glob(pat), reverse=True)
    return matches[0] if matches else None


def _wait_port(port: int, timeout: float = 30.0) -> None:
    import socket
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            _time.sleep(0.4)
    raise CrowtadoError(f"Chrome não abriu a porta de depuração {port}.")


def criar_conta(email: str, senha: str, ref: str = DEFAULT_REF,
                timeout_email: int = 180) -> None:
    """Cria uma conta no crowtado (sign-up + verificação de email automática).

    O sign-up tem Cloudflare Turnstile (via Clerk) — só passa num Chrome REAL,
    então lançamos o Chrome com `--remote-debugging-port` (perfil persistente
    em CHROME_PROFILE) e dirigimos via CDP. O código de verificação é lido da
    caixa catch-all da Hostinger (hostinger_mail.wait_for_code), então o email
    precisa ser de um domínio catch-all configurado (ex.: @academy4u.com.br).

    Levanta CrowtadoError em qualquer falha. Retorna None em sucesso (a conta
    já sai verificada e logada).
    """
    import subprocess

    from playwright.sync_api import sync_playwright

    from .hostinger_mail import max_uid, wait_for_code

    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    proc = subprocess.Popen(
        [_chrome_exe(), f"--remote-debugging-port={port}",
         f"--user-data-dir={CHROME_PROFILE}", "--no-first-run",
         "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_port(port)
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            ctx.clear_cookies()  # sessão da conta anterior (batch) não vaza
            page = ctx.new_page()
            page.goto(f"{SITE_BASE}/pt-BR/sign-up?ref={ref}",
                      wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(4000)

            # o form Clerk fica atrás do aceite dos termos + CTA custom do site
            page.locator("input[type=checkbox]").first.check()
            page.wait_for_timeout(1000)
            page.get_by_text("Cadastre-se como Colaborador", exact=False).first.click()
            page.wait_for_selector("#emailAddress-field", timeout=30_000)
            page.fill("#emailAddress-field", email)
            page.fill("#password-field", senha)
            uid_base = max_uid()
            page.click("button.cl-formButtonPrimary")

            destino = None
            for _ in range(12):
                page.wait_for_timeout(3000)
                if "verify" in page.url:
                    destino = "verify"
                    break
                if "/sign-up" not in page.url:
                    destino = "direto"
                    break
                err = page.evaluate(
                    "(document.querySelector('.cl-formFieldErrorText')||{}).innerText || ''")
                if err:
                    raise CrowtadoError(f"form de sign-up recusou: {err}")
            if destino is None:
                raise CrowtadoError(
                    f"sign-up não avançou (url={page.url.split('?')[0]}) — "
                    f"captcha/Turnstile pode ter reprovado o navegador")

            if destino == "verify":
                code = wait_for_code(email, sender="crowtado.com",
                                     min_uid=uid_base, timeout=timeout_email)
                if page.query_selector('input[id^="digit-"]'):
                    for i, digit in enumerate(code):
                        page.fill(f"#digit-{i}-field", digit)
                else:
                    page.locator('input[data-input-otp]').press_sequentially(code, delay=80)

            # sucesso = saiu do fluxo de sign-up (cai no dashboard)
            for _ in range(20):
                page.wait_for_timeout(1500)
                if "/sign-up" not in page.url:
                    break
            else:
                raise CrowtadoError("código aceito mas a conta não saiu do sign-up "
                                    f"(url={page.url.split('?')[0]})")
            browser.close()
    except CrowtadoError:
        raise
    except Exception as exc:  # noqa: BLE001 — Playwright quebra de N jeitos
        raise CrowtadoError(f"criação de conta falhou: {type(exc).__name__}: {exc}") from exc
    finally:
        proc.terminate()
