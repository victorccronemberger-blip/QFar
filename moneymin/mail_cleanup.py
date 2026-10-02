"""User-reviewed INBOX cleanup. Never DELETE, empty Trash, or follow mail links.

Hostinger contract: POST messages/{uid}/move, {targetFolder: special-use Trash}.
https://github.com/hostinger/mail-api-python-sdk/blob/main/docs/MessagesApi.md
"""
from __future__ import annotations

import copy
import hashlib
import html
import re
import threading
import time
import unicodedata
import uuid
import urllib.parse
import urllib.request
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from . import config, hostinger_mail as mail, tls


class CleanupError(ValueError):
    pass


def _normalized(text):
    text = ''.join(c for c in unicodedata.normalize('NFKD', html.unescape(text))
                   if not unicodedata.combining(c)).casefold()
    return re.sub(r'\s+', ' ', re.sub(r'[\u200b-\u200d\ufeff]', '', text)).strip()


def payment_signal(text):
    """Evidence of a payout, not a provider name or an arbitrary substring.

    Never call this on transport headers, CSS, tracking URLs or signatures:
    DKIM's signed header list routinely contains Content-TRANSFER-Encoding.
    """
    text = _normalized(text)
    if re.search(r'\b(?:payouts?|pay\s*out|withdraw(?:al|als|n)?|cash\s*out|saques?|sacar)\b', text):
        return True
    money = r'(?:payments?|pagamentos?|transfers?|transferencias?|rewards?|recompensas?|resgates?|retirada|remessa|money|dinheiro|funds)'
    outcome = r'(?:ready|available|sent|received|completed|successful|processed|released|on its way|disponivel|disponiveis|pront[oa]s?|enviad[oa]s?|recebid[oa]s?|aprovad[oa]s?|liberad[oa]s?|confirmad[oa]s?|concluid[oa]s?)'
    # Do not span sentences: an unrelated footer must not supply the outcome.
    between = r'(?:[^.!?\n]|\.(?=\d))'
    if re.search(rf'\b{money}\b{between}{{0,65}}\b{outcome}\b|\b{outcome}\s+(?:(?:your|the|seu|o|um)\s+)?{money}\b', text):
        return True
    if re.search(rf'\b(?:claim|redeem|resgatar|receba|receber)\b[^.!?\n]{{0,40}}\b{money}\b', text):
        return True
    # Short transfer subjects can omit the amount/status.
    return bool(re.fullmatch(r'(?:paypal|wise)', text)
                or re.search(r'\b(?:wise|paypal|dots|tremendous|airtm)\b.{0,30}\b(?:transfer|transferencia|remessa)\b', text))


class _MailHTML(HTMLParser):
    """Visible copy and actionable links; never stylesheet/attribute keywords."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.links = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('style', 'script', 'head'):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ('p', 'div', 'br', 'tr', 'li', 'h1', 'h2', 'h3'):
            self.text.append('\n')
        if tag in ('a', 'area'):
            self.links.extend(value for key, value in attrs if key == 'href' and value)

    def handle_endtag(self, tag):
        if tag in ('style', 'script', 'head') and self.hidden:
            self.hidden -= 1
        if not self.hidden and tag in ('p', 'div', 'tr', 'li', 'h1', 'h2', 'h3'):
            self.text.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


_URL = re.compile(r'https?://[^\s<>"\']+', re.I)


def _payment_link(url):
    """Recognize redemption routes, excluding provider login/legal/marketing links."""
    try:
        parsed = urllib.parse.urlsplit(html.unescape(url))
        host = (parsed.hostname or '').casefold().rstrip('.')
        path = urllib.parse.unquote(parsed.path).casefold()
        if host in ('pay.dots.dev', 'paypal.me'):
            return True
        if host == 'my.dots.dev' and path.startswith('/flow/'):
            return True
        provider = any(host == domain or host.endswith('.' + domain)
                       for domain in ('dots.dev', 'tremendous.com', 'paypal.com', 'wise.com', 'airtm.com'))
        return provider and bool(re.search(
            r'/(?:payouts?|rewards?|redeem|claim|transfers?|pay|payments?)(?:/|$)', path))
    except ValueError:
        return False


def _uncertain_payment_link(url):
    """Do not discard an unfamiliar action route on a payment portal."""
    try:
        parsed = urllib.parse.urlsplit(html.unescape(url))
        host = (parsed.hostname or '').casefold().rstrip('.')
        path = urllib.parse.unquote(parsed.path).casefold().rstrip('/')
        portal = host in ('my.dots.dev', 'api.tremendous.com', 'wise.com', 'www.wise.com',
                          'paypal.com', 'www.paypal.com')
        informational = re.match(
            r'^/(?:[a-z]{2}/)?(?:confirm-email|verify(?:-email)?|auth|login|signin|sign-in|signup|sign-up|'
            r'register|reset-password|forgot-password|unsubscribe|privacy|terms|legal|help|support|blog|'
            r'about|contact|pricing|business|personal)(?:/|$)', path)
        return bool(portal and path and not informational)
    except ValueError:
        return False


def classify(raw: bytes, now=None):
    """Absence of a signal is insufficient if MIME/identity/content is uncertain."""
    now = time.time() if now is None else now
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        if payment_signal(str(msg.get('Subject') or '')):
            return 'payment', 'Assunto de saque ou recebimento'
        if any(not msg.get(k) for k in ('From', 'To', 'Date', 'Message-ID', 'Subject')):
            return 'review', 'Identificação incompleta'
        date = parsedate_to_datetime(str(msg['Date']))
        if date.tzinfo is None or now - date.timestamp() < 600:
            return 'review', 'Mensagem recente ou data incerta'
        texts = []
        links = []
        uncertain = False
        for part in msg.walk():
            uncertain |= bool(part.defects)
            if part.is_multipart():
                continue
            if part.get_content_type() not in ('text/plain', 'text/html'):
                # A CID logo is decoration, not an unreadable financial document.
                inline_image = (part.get_content_maintype() == 'image'
                                and part.get_content_disposition() == 'inline'
                                and part.get('Content-ID'))
                uncertain |= not bool(inline_image)
                if payment_signal(str(part.get_filename() or '')):
                    return 'payment', 'Anexo relacionado a saque'
                continue
            content = part.get_content()
            if not isinstance(content, str) or '\ufffd' in content:
                uncertain = True
                continue
            if part.get_content_type() == 'text/html':
                parsed = _MailHTML()
                parsed.feed(content)
                content = ''.join(parsed.text)
                links.extend(parsed.links)
            links.extend(_URL.findall(content))
            texts.append(_URL.sub('', content))
            uncertain |= bool(part.defects)
        if any(_payment_link(link) for link in links):
            return 'payment', 'Link de saque ou recebimento'
        if any(payment_signal(text) for text in texts):
            return 'payment', 'Saque ou recebimento no conteúdo'
        if any(_uncertain_payment_link(link) for link in links):
            return 'review', 'Link financeiro não reconhecido; revisar'
        if uncertain or not any(t.strip() for t in texts):
            return 'review', 'Conteúdo não analisável integralmente'
        return 'move', 'Sem indicação de saque ou recebimento'
    except Exception:
        return 'review', 'Falha ao analisar mensagem; preservada'


class Mailbox:
    def __init__(self, profile):
        self.token = profile['token']
        # Do not silently choose the first mailbox of a multi-mailbox token.
        self.id = str(profile.get('mailbox_id') or '')
        if not self.id:
            raise CleanupError('Selecione uma caixa explícita na integração Hostinger.')
        self.base = '/api/v1/mailboxes/' + urllib.parse.quote(self.id, safe='')

    def get(self, path):
        status, body = mail._request(self.base + path, token=self.token)
        if status != 200 or not isinstance(body, dict):
            raise CleanupError(f'Leitura da caixa falhou (HTTP {status}); nada será presumido.')
        return body

    def pages(self, path):
        for page in range(1, 101):
            sort = '&sort=uid' if path.endswith('/messages') else ''
            body = self.get(f'{path}?perPage=100&page={page}{sort}')
            rows = body.get('data')
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise CleanupError('Resposta incompleta da caixa; limpeza bloqueada.')
            yield from rows
            pagination = body.get('pagination')
            if isinstance(pagination, dict):
                current, last = pagination.get('page'), pagination.get('totalPages')
                if type(current) is not int or type(last) is not int or current != page or last < 0:
                    raise CleanupError('Paginação inválida; limpeza bloqueada.')
                if current >= last:
                    return
                if not rows:
                    raise CleanupError('Página incompleta da caixa; limpeza bloqueada.')
            elif len(rows) < 100:
                return
        raise CleanupError('Limite de análise atingido; limpeza bloqueada.')

    def trash(self):
        paths = {row.get('path') for row in self.pages('/folders')
                 if str(row.get('specialUse') or '').casefold() == '\\trash'}
        if len(paths) != 1 or not next(iter(paths)) or 'INBOX' in paths:
            raise CleanupError('Não foi possível identificar uma única lixeira oficial. Nada movido.')
        return next(iter(paths))

    def messages(self):
        seen = set()
        for row in self.pages('/folders/INBOX/messages'):
            uid = row.get('uid')
            if type(uid) is not int or uid <= 0 or uid in seen or row.get('path') != 'INBOX':
                raise CleanupError('A caixa mudou durante a leitura ou retornou dados inválidos. Gere nova prévia.')
            seen.add(uid)
            yield row

    def source(self, uid):
        req = urllib.request.Request(config.HOSTINGER_MAIL_BASE + self.base
                                     + f'/folders/INBOX/messages/{uid}/source')
        req.add_header('Authorization', f'Bearer {self.token}')
        req.add_header('Accept', 'message/rfc822')
        req.add_header('User-Agent', 'curl/8.5.0')
        try:
            with tls.urlopen(req, timeout=30) as response:
                raw = response.read(2_000_001)
                if response.status != 200 or len(raw) > 2_000_000:
                    raise CleanupError('Mensagem não pôde ser lida integralmente.')
                return raw
        except Exception as exc:
            raise CleanupError('Não foi possível ler a mensagem; preservada.') from exc

    def move(self, uid, trash):
        # Explicit MOVE, never DELETE; no automatic retry after an uncertain reply.
        status, _ = mail._request(self.base + f'/folders/INBOX/messages/{uid}/move',
                                  'POST', {'targetFolder': trash}, token=self.token)
        if status != 204:
            raise CleanupError('Movimentação não confirmada. Pare e confira a caixa e a lixeira antes de repetir.')


class Cleanup:
    def __init__(self):
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.state = {'state': 'idle', 'items': [], 'checked': 0, 'moved': 0, 'skipped': 0}
        self.plan = None

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def start(self, profile):
        with self.lock:
            if self.state['state'] in ('scanning', 'moving'):
                raise CleanupError('Aguarde a operação atual ou pare a limpeza.')
            self.cancel.clear()
            self.plan = None
            self.state = {'state': 'scanning', 'id': uuid.uuid4().hex,
                          'mailbox': profile.get('name') or profile['mailbox_id'],
                          'items': [], 'checked': 0, 'moved': 0, 'skipped': 0}
            threading.Thread(target=self.scan, args=(dict(profile),), daemon=True).start()
            return self.snapshot()

    def scan(self, profile):
        try:
            box = Mailbox(profile)
            trash = box.trash()
            candidates = {}
            for row in box.messages():
                if self.cancel.is_set():
                    self.update(state='cancelled')
                    return
                uid = row['uid']
                if payment_signal(str(row.get('subject') or '')):
                    action, reason = 'payment', 'Assunto de saque ou pagamento'
                else:
                    try:
                        raw = box.source(uid)
                        action, reason = classify(raw)
                        if action == 'move':
                            candidates[uid] = hashlib.sha256(raw).hexdigest()
                    except CleanupError:
                        action, reason = 'review', 'Leitura incompleta; preservada'
                with self.lock:
                    self.state['items'].append({'uid': uid, 'subject': str(row.get('subject') or '(sem assunto)'),
                        'date': str(row.get('date') or ''), 'action': action, 'reason': reason})
                    self.state['checked'] += 1
            if self.cancel.is_set():
                self.update(state='cancelled')
                return
            with self.lock:
                self.plan = (box, trash, candidates, time.monotonic())
                self.state['state'] = 'ready'
        except Exception as exc:
            self.update(state='error', error=str(exc) if isinstance(exc, CleanupError)
                        else 'Falha na análise. Nada foi movido.')

    def apply(self, plan_id, selected):
        with self.lock:
            if self.state['state'] != 'ready' or plan_id != self.state['id'] or not self.plan:
                raise CleanupError('Prévia inválida, já utilizada ou indisponível.')
            box, trash, candidates, created = self.plan
            if time.monotonic() - created > 1800:
                raise CleanupError('Prévia expirada. Analise novamente a caixa.')
            if (not isinstance(selected, list) or not selected
                    or any(type(uid) is not int or uid not in candidates for uid in selected)
                    or len(set(selected)) != len(selected)):
                raise CleanupError('Seleção inválida; e-mails protegidos não podem ser movidos.')
            self.cancel.clear()
            self.state.update(state='moving', total=len(selected), processed=0)
            threading.Thread(target=self.execute, args=(box, trash, candidates, list(selected)), daemon=True).start()
            return self.snapshot()

    def execute(self, box, trash, candidates, selected):
        try:
            if box.trash() != trash:
                raise CleanupError('A lixeira mudou. Gere nova prévia.')
            for uid in selected:
                if self.cancel.is_set():
                    self.update(state='cancelled')
                    return
                raw = box.source(uid)
                # Changed UID/content, a newly recognized payout, or uncertainty: preserve.
                if hashlib.sha256(raw).hexdigest() != candidates[uid] or classify(raw)[0] != 'move':
                    with self.lock:
                        self.state['skipped'] += 1
                else:
                    if self.cancel.is_set():
                        self.update(state='cancelled')
                        return
                    box.move(uid, trash)
                    with self.lock:
                        self.state['moved'] += 1
                with self.lock:
                    self.state['processed'] += 1
            self.update(state='done')
        except Exception as exc:
            self.update(state='error', error=str(exc) if isinstance(exc, CleanupError)
                        else 'Operação interrompida; o último movimento pode ter ocorrido. Confira a lixeira. Não repetimos automaticamente.')


CLEANUP = Cleanup()
