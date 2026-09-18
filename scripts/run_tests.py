"""Executa unittest com dados isolados, sem carregar a configuração pessoal."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    arguments = sys.argv[1:] or ["discover", "-s", "test", "-q"]
    with tempfile.TemporaryDirectory(prefix="qmoney-tests-") as root:
        environment = os.environ.copy()
        environment.update(QMONEY_USER_ROOT=root, QMONEY_LIBRARY_ROOT=root)
        return subprocess.run(
            [sys.executable, "-m", "unittest", *arguments],
            cwd=project, env=environment,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
