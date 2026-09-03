"""Diagnóstico local de prontidão para executar campanhas."""
from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import config, holo_accelerator, holoassist
from .sidecar import ffmpeg_bin, ffprobe_bin


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _provider_from_preferences() -> str:
    path = config.DATA_DIR / "webui_prefs.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "holoassist"
    provider = str(raw.get("dataset_provider") or "holoassist").strip().lower()
    return provider if provider in {"holoassist", "ego4d", "all"} else "holoassist"


def _binary_works(command: str) -> bool:
    try:
        result = subprocess.run(
            [command, "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _private_browser_present() -> bool:
    """Confirma o Chromium privado distribuído com o pacote Release."""
    roots = [
        Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")),
        config.RUNTIME_ROOT / "ms-playwright",
    ]
    for root in roots:
        if not str(root) or not root.is_dir():
            continue
        patterns = (
            "chromium-*/chrome-win*/chrome.exe",
            "chromium_headless_shell-*/chrome-headless-shell-win*/chrome-headless-shell.exe",
        )
        if any(any(root.glob(pattern)) for pattern in patterns):
            return True
    return False


def _valid_account_tokens() -> tuple[int, int]:
    valid = total = 0
    if not config.SECRETS_DIR.exists():
        return valid, total
    for path in config.SECRETS_DIR.glob("token_*.json"):
        total += 1
        try:
            token = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        has_identity = bool(token.get("email"))
        has_auth = bool(
            token.get("refreshToken")
            or token.get("refresh_token")
            or token.get("idToken")
            or token.get("id_token")
        )
        if has_identity and has_auth:
            valid += 1
    return valid, total


def _aws_credentials_present() -> bool:
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return True
    profile = config.EGO4D_AWS_PROFILE or "default"
    configured = os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "").strip()
    candidates = ([Path(configured)] if configured else [
        config.EGO4D_LOCAL_AWS_CREDENTIALS,
        Path.home() / ".aws" / "credentials",
    ])
    for path in candidates:
        if not path.is_file():
            continue
        parser = configparser.RawConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error):
            continue
        if (parser.has_section(profile)
                and parser.get(profile, "aws_access_key_id", fallback="").strip()
                and parser.get(profile, "aws_secret_access_key", fallback="").strip()):
            return True
    return False


def campaign_readiness(provider: str | None = None) -> dict[str, Any]:
    """Retorna checks locais sem autenticar contas nem enviar dados."""
    selected = (provider or _provider_from_preferences()).strip().lower()
    if selected not in {"holoassist", "ego4d", "all"}:
        raise ValueError("provider deve ser holoassist, ego4d ou all")

    checks: list[dict[str, str]] = []
    ffmpeg = ffmpeg_bin()
    ffprobe = ffprobe_bin()
    media_ok = _binary_works(ffmpeg) and _binary_works(ffprobe)
    checks.append(_check(
        "FFmpeg/FFprobe",
        "ok" if media_ok else "error",
        "disponíveis para preparar vídeo" if media_ok
        else "use Reparar instalação na aba Integrações",
    ))

    browser_ok = _private_browser_present()
    checks.append(_check(
        "Navegador privado",
        "ok" if browser_ok else "warning",
        "Chromium incluído para cadastros e verificações" if browser_ok
        else "componente incluído no pacote; use Reparar instalação no QMoney",
    ))

    valid_tokens, total_tokens = _valid_account_tokens()
    checks.append(_check(
        "Contas Minute",
        "ok" if valid_tokens else "error",
        f"{valid_tokens} token(s) utilizável(is) de {total_tokens} arquivo(s)"
        if valid_tokens
        else "nenhuma conta; copie secrets/ de forma privada ou adicione pela interface",
    ))

    if selected in {"holoassist", "all"}:
        annotations = holoassist.annotations_path()
        splits = holoassist.data_dir() / "data-splits-v1_2.zip"
        metadata_ok = (
            annotations.exists()
            and annotations.stat().st_size > 1_000_000
            and splits.exists()
            and splits.stat().st_size > 0
        )
        seed_names = (
            "video.index.json.gz",
            "video_compress.index.json.gz",
            "imu.index.json.gz",
        )
        indexes_ok = all((holoassist._INDEX_SEED_DIR / name).exists() for name in seed_names)
        checks.append(_check(
            "Catálogo HoloAssist",
            "ok" if metadata_ok else "warning",
            "metadados instalados" if metadata_ok
            else "será preparado automaticamente ao selecionar HoloAssist",
        ))
        checks.append(_check(
            "Índices HoloAssist",
            "ok" if indexes_ok else "error",
            "índices portáteis disponíveis" if indexes_ok
            else "índices portáteis ausentes no pacote",
        ))
        if metadata_ok:
            try:
                cache = holo_accelerator.cache_status()
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                checks.append(_check(
                    "Acelerador HoloAssist",
                    "warning",
                    f"cache ainda não pôde ser medido: {exc}",
                ))
            else:
                ready = int(cache.get("ready") or 0)
                total = int(cache.get("total") or 0)
                checks.append(_check(
                    "Acelerador HoloAssist",
                    "ok" if ready else "warning",
                    f"{ready}/{total} clipe(s) prontos; os demais são preparados sob demanda",
                ))

    if selected in {"ego4d", "all"}:
        ego_dir = config.MEDIA_DATA_DIR / "ego4d"
        catalog_ok = (
            (ego_dir / "ego4d.json").exists()
            and (ego_dir / "clips.csv").exists()
        )
        aws_ok = _aws_credentials_present()
        checks.append(_check(
            "Catálogo Ego4D",
            "ok" if catalog_ok else ("warning" if aws_ok else "error"),
            "catálogo instalado" if catalog_ok else (
                "será preparado automaticamente com as credenciais salvas"
                if aws_ok else
                "configure as credenciais na aba Integrações"
            ),
        ))
        checks.append(_check(
            "Credenciais Ego4D",
            "ok" if aws_ok else "error",
            "perfil AWS encontrado" if aws_ok else "perfil AWS autorizado não encontrado",
        ))

    free_gib = shutil.disk_usage(config.ROOT).free / (1024 ** 3)
    checks.append(_check(
        "Espaço livre",
        "ok" if free_gib >= 20 else "warning",
        f"{free_gib:.1f} GiB livres; recomendado pelo menos 20 GiB",
    ))
    checks.append(_check(
        "Criador de contas",
        "ok" if config.HOSTINGER_MAIL_TOKEN else "warning",
        "HOSTINGER_MAIL_TOKEN configurado" if config.HOSTINGER_MAIL_TOKEN
        else "token Hostinger ausente; campanhas com contas já cadastradas continuam possíveis",
    ))

    return {
        "ready": not any(item["status"] == "error" for item in checks),
        "provider": selected,
        "checks": checks,
    }


__all__ = ["campaign_readiness"]
