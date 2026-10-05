"""TLS portátil do QMoney.

Combina as autoridades confiáveis do Windows com o pacote Mozilla distribuído
pelo ``certifi``. Assim HTTPS continua funcionando em uma instalação nova mesmo
quando o armazenamento local de certificados está incompleto.
"""
from __future__ import annotations

import os
import ssl
import urllib.request
from urllib.parse import urlsplit
from functools import lru_cache
from typing import Any

import certifi


def ca_bundle() -> str:
    """Caminho do bundle Mozilla incluído no executável empacotado."""
    return certifi.where()


def configure_environment() -> None:
    """Expõe o mesmo bundle para bibliotecas que não usam nosso contexto."""
    bundle = ca_bundle()
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                 "AWS_CA_BUNDLE"):
        os.environ.setdefault(name, bundle)


@lru_cache(maxsize=1)
def context() -> ssl.SSLContext:
    """Contexto que preserva raízes do Windows e acrescenta o bundle Mozilla."""
    configure_environment()
    value = ssl.create_default_context()
    value.load_verify_locations(cafile=ca_bundle())
    return value


class _RequiredProxy(urllib.request.ProxyHandler):
    def proxy_open(self, req, proxy, type):
        # Selected registration proxies must also override NO_PROXY/Windows bypass.
        req.set_proxy(urlsplit(proxy).netloc, "http")
        return None


def build_opener(*handlers: Any) -> urllib.request.OpenerDirector:
    """Cria opener urllib com o TLS portátil, cookies/proxy e demais handlers."""
    from .registration_proxy import endpoint
    proxy = endpoint()
    if proxy:
        handlers = (_RequiredProxy({"http":proxy,"https":proxy}), *handlers)
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context()), *handlers)


def urlopen(url: Any, data: bytes | None = None, timeout: float | None = None):
    """Equivalente a urllib.request.urlopen usando o contexto do QMoney."""
    from .registration_proxy import endpoint
    if endpoint():
        return build_opener().open(url, data=data, timeout=timeout)
    return urllib.request.urlopen(url, data=data, timeout=timeout, context=context())


__all__ = ["build_opener", "ca_bundle", "configure_environment", "context", "urlopen"]
