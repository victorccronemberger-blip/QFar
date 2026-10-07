"""Servico HTTP local consumido exclusivamente pela interface Qt do QMoney."""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys
import threading

from .server import create_app, _require_local_api_token
from .. import campaign, config, tls
from ..state_lease import service_state_lease


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
        except (OSError, UnicodeError, ValueError):
            return len(data)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except (OSError, UnicodeError, ValueError):
            pass

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def _harden_stdio() -> None:
    # Impede que falhas de inicialização de ferramentas auxiliares (por
    # exemplo, DLL ausente no ffprobe) abram uma caixa modal do Windows e
    # bloqueiem o motor. O subprocesso ainda devolve erro normalmente.
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            current = int(kernel32.GetErrorMode())
            kernel32.SetErrorMode(current | 0x0001 | 0x0002 | 0x8000)
        except (AttributeError, OSError, ValueError):
            pass
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


def _watch_parent(parent_pid: int | None) -> None:
    """Encerra o serviço congelado assim que o desktop que o iniciou terminar."""
    if not parent_pid or parent_pid <= 0 or os.name != "nt":
        return

    def wait_for_parent() -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # HANDLE tem largura de ponteiro: o retorno padrão c_int trunca em x64.
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        synchronize = 0x00100000
        handle = kernel32.OpenProcess(synchronize, False, int(parent_pid))
        if not handle:
            # ERROR_INVALID_PARAMETER: o PID já não existe. Acesso negado ou
            # outra falha de observação não comprovam que o desktop terminou.
            if ctypes.get_last_error() == 87:
                os._exit(0)
            return
        try:
            result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            kernel32.CloseHandle(handle)
        if result == 0:  # WAIT_OBJECT_0: término confirmado
            os._exit(0)

    threading.Thread(target=wait_for_parent, daemon=True,
                     name="qmoney-parent-watch").start()


def _validate_local_binding(host: str, port: int) -> tuple[str, int]:
    """Accept only a literal loopback address and an explicit valid TCP port."""
    if not isinstance(host, str):
        raise ValueError("O serviço local deve usar um endereço de loopback.")
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("O serviço local deve usar um endereço de loopback.") from None
    if not address.is_loopback:
        raise ValueError("O serviço local deve usar um endereço de loopback.")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("A porta local deve ser um inteiro entre 1 e 65535.")
    return str(address), port


def run_webui(host: str = "127.0.0.1", port: int = 8876,
              open_browser: bool = False, parent_pid: int | None = None) -> None:
    """Sobe o servico local. ``open_browser`` e mantido apenas por compatibilidade."""
    host, port = _validate_local_binding(host, port)
    _require_local_api_token()
    with service_state_lease(config.DATA_DIR):
        _run_claimed_service(host, port, parent_pid)


def _run_claimed_service(host: str, port: int, parent_pid: int | None) -> None:
    """Start jobs and serve only while the OS state lease is held."""
    _harden_stdio()
    tls.configure_environment()
    _watch_parent(parent_pid)
    print(f"QMoney service em http://{host}:{port}  (Ctrl+C para sair)")
    app = create_app()

    # Cold classification runs only through the requested catalog job. Starting
    # it here competes with account loading before the user opens a campaign.
    _serve(app, host, port, False)


def _serve(app, host: str, port: int, open_browser: bool) -> None:
    host, port = _validate_local_binding(host, port)
    _require_local_api_token()
    if app.config.get("QMONEY_LOCAL_API_AUTHENTICATED") is not True:
        raise RuntimeError("Uma fixture sem autenticação não pode iniciar o serviço local.")
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
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()
    run_webui(host=args.host, port=args.porta, open_browser=not args.no_browser,
              parent_pid=args.parent_pid or None)


__all__ = ["create_app", "main", "run_webui"]
