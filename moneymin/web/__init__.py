"""Servico HTTP local consumido exclusivamente pela interface Qt do QMoney."""
from __future__ import annotations

import argparse
import sys

from .server import create_app


class _SafeStream:
    """Wrapper de stdout/stderr que não deixa o log derrubar o servidor.

    Quando o servidor é lançado em background e o console/pai morre, qualquer
    `print` (logs da campanha, do upload, do werkzeug) levantaria
        `OSError: [Errno 22] Invalid argument` — e um print na thread da campanha
        a derrubava no meio. Consoles Windows com encoding legado também podem
        gerar UnicodeEncodeError. Nesses casos, a escrita vira no-op.
    """

    def __init__(self, stream) -> None:
        self._stream = stream

    def write(self, data):
        try:
            return self._stream.write(data)
        except (OSError, UnicodeError):
            return len(data)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except (OSError, UnicodeError):
            pass

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def _harden_stdio() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not None and not isinstance(stream, _SafeStream):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    pass
            setattr(sys, name, _SafeStream(stream))


def run_webui(host: str = "127.0.0.1", port: int = 8876,
              open_browser: bool = False) -> None:
    """Sobe o servico local. ``open_browser`` e mantido apenas por compatibilidade."""
    _harden_stdio()
    print(f"QMoney service em http://{host}:{port}  (Ctrl+C para sair)")
    _serve(create_app(), host, port, False)


def _serve(app, host: str, port: int, open_browser: bool) -> None:
    import webbrowser

    url = f"http://{host}:{port}"
    if open_browser:
        # abre depois de um instante, sem bloquear o startup do servidor
        import threading
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    # A interface Qt usa uma porta exclusiva e fixa. Trocar silenciosamente
    # poderia conectar o desktop ao servico de outra instalacao.
    app.run(host=host, port=port, threaded=True, use_reloader=False)


def main() -> None:
    """Entrada do comando instalado ``moneymin``."""
    parser = argparse.ArgumentParser(description="Servico local da interface Qt do QMoney")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--porta", "--port", type=int, default=8876)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    run_webui(host=args.host, port=args.porta, open_browser=not args.no_browser)


__all__ = ["create_app", "main", "run_webui"]
