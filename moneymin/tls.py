"""TLS portátil do QMoney.

Combina as autoridades confiáveis do Windows com o pacote Mozilla distribuído
pelo ``certifi``. Assim HTTPS continua funcionando em uma instalação nova mesmo
quando o armazenamento local de certificados está incompleto.
"""
from __future__ import annotations

import os
import ssl
import urllib.request
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


def build_opener(*handlers: Any) -> urllib.request.OpenerDirector:
    """Cria opener urllib com o TLS portátil, cookies/proxy e demais handlers."""
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context()), *handlers)


def urlopen(url: Any, data: bytes | None = None, timeout: float | None = None):
    """Equivalente a urllib.request.urlopen usando o contexto do QMoney."""
    return urllib.request.urlopen(url, data=data, timeout=timeout, context=context())


__all__ = ["build_opener", "ca_bundle", "configure_environment", "context", "urlopen"]
