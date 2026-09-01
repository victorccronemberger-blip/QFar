"""
config.py — Configuração central do projeto MoneyMin.

Centraliza URLs, chaves e caminhos. Valores sensíveis (API key, tokens) podem
ser sobrescritos por variáveis de ambiente ou por um arquivo `.env` na raiz do
projeto — nunca comite `.env` nem a pasta `secrets/` (veja `.gitignore`).

Uso:
    from moneymin import config
    config.BASE_URL
    config.tokens_dir()
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# --- Raiz do projeto e diretórios -------------------------------------------
# Em desenvolvimento tudo continua na raiz do checkout. No executável, os
# dados mutáveis vivem em %LOCALAPPDATA%/QMoney e os recursos somente-leitura
# são resolvidos dentro do bundle criado pelo PyInstaller.
CODE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
ROOT = Path(os.environ.get("QMONEY_USER_ROOT", CODE_ROOT)).expanduser().resolve()
RUNTIME_ROOT = Path(os.environ.get("QMONEY_RUNTIME_ROOT", CODE_ROOT)).expanduser().resolve()
LIBRARY_ROOT = Path(os.environ.get("QMONEY_LIBRARY_ROOT", ROOT)).expanduser().resolve()

DATA_DIR = ROOT / "data"
MEDIA_DATA_DIR = LIBRARY_ROOT / "data"
VIDEOS_DIR = MEDIA_DATA_DIR / "videos"
MANIFESTS_DIR = MEDIA_DATA_DIR / "manifests"
SECRETS_DIR = ROOT / "secrets"
REFERENCE_DIR = CODE_ROOT / "reference"
OPENAPI_PATH = REFERENCE_DIR / "openapi.json"


def _load_dotenv(path: Path) -> None:
    """Carregador mínimo de `.env` (sem dependências externas)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

# O patch privado pode carregar o perfil AWS junto do projeto para facilitar a
# migração para outro computador. Variáveis já definidas pelo usuário sempre
# vencem; os arquivos portáteis são apenas o fallback local e continuam dentro
# de ``secrets/``, que nunca entra no Git.
EGO4D_LOCAL_AWS_DIR = SECRETS_DIR / "aws"
EGO4D_LOCAL_AWS_CREDENTIALS = EGO4D_LOCAL_AWS_DIR / "credentials"
EGO4D_LOCAL_AWS_CONFIG = EGO4D_LOCAL_AWS_DIR / "config"
if EGO4D_LOCAL_AWS_CREDENTIALS.is_file():
    os.environ.setdefault(
        "AWS_SHARED_CREDENTIALS_FILE", str(EGO4D_LOCAL_AWS_CREDENTIALS)
    )
if EGO4D_LOCAL_AWS_CONFIG.is_file():
    os.environ.setdefault("AWS_CONFIG_FILE", str(EGO4D_LOCAL_AWS_CONFIG))

# --- Endpoints e credenciais ------------------------------------------------
# Base da minute-api (Azure Container Apps) — extraída do APK / captura mitm.
BASE_URL = os.environ.get(
    "MINUTE_BASE_URL",
    "https://minute-api-production.nicebush-c949d8c8.eastus2.azurecontainerapps.io",
)

# Firebase Web API key (projeto d8amobile) — vem do res/values/strings.xml do
# APK, portanto não é um segredo pessoal, mas é sobrescrevível por ambiente.
FIREBASE_API_KEY = os.environ.get(
    "MINUTE_FIREBASE_API_KEY",
    "AIzaSyD1wdhw0mNPRIWA7ZALPnbZu4Lg7Lax5uE",
)

# Código de convite da org (usado no registro de novas contas). IMUTÁVEL —
# fixo no código, não configurável via .env.
INVITE_CODE = "VZEAE7WC"

# --- Hostinger Mail (catch-all para códigos de verificação) ------------------
# Token da API de email da Hostinger (hPanel -> Advanced -> API). Sem default:
# configure no `.env` (HOSTINGER_MAIL_TOKEN=...).
HOSTINGER_MAIL_TOKEN = os.environ.get("HOSTINGER_MAIL_TOKEN", "")
# resourceId da caixa (ex.: AC5ce3f1...); vazio = primeira caixa da conta.
HOSTINGER_MAILBOX_ID = os.environ.get("HOSTINGER_MAILBOX_ID", "")
HOSTINGER_MAIL_BASE = "https://api.mail.hostinger.com"

# --- Crowtado (registro de conta com referral) --------------------------------
# Código de indicação IMUTÁVEL — fixo no código, não configurável.
CROWTADO_REF = "288TVN3C"
CROWTADO_SIGNUP_URL = f"https://www.crowtado.com/sign-up?ref={CROWTADO_REF}"

# Versão do app usada nos headers (bate com a captura mitm).
# Backend exige min 1.21.0 (erro app_version_too_old em 08/08);
# App Store já está em 1.22.0 (release 13/08/2026) — version-check do
# backend ainda reporta latest 1.21.0, mas 1.21.0 segue aceita.
APP_VERSION = os.environ.get("MINUTE_APP_VERSION", "1.22.0")
USER_AGENT = f"Minute/{APP_VERSION} (com.bakerdata.minute; build:1; iOS 26.5.2)"
# iOS no Brasil (todas as contas operam daqui).
ACCEPT_LANGUAGE = os.environ.get("MINUTE_ACCEPT_LANGUAGE", "pt-BR,pt;q=0.9")
# Teto do cache de MP4 por aparelho (`*_native_acc*.mp4` em data/ego4d/).
# O `_native.mp4` não conta — é a fonte do re-encode rápido. 0 = sem teto.
VIDEO_CACHE_GB = float(os.environ.get("MINUTE_VIDEO_CACHE_GB", "40"))

# Perfil/region usados somente para o dataset Ego4D. O perfil vazio força a
# cadeia padrão do boto3 (variáveis AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY,
# IAM role etc.); sem configuração explícita preservamos o profile `default`
# adotado pelo CLI oficial.
_EGO4D_PROFILE_ENV = os.environ.get("EGO4D_AWS_PROFILE")
EGO4D_AWS_PROFILE = (
    _EGO4D_PROFILE_ENV
    if _EGO4D_PROFILE_ENV is not None
    else os.environ.get("AWS_PROFILE", "default")
)
EGO4D_AWS_REGION = os.environ.get("EGO4D_AWS_REGION", "").strip()


# --- Perfil nativo do app iOS (réplica do upload) ----------------------------
# Valores que o app nativo REAL envia. Extraídos da captura iOS (req 072):
# o meta do POST /uploads usa o formato CURTO getDeviceUploadMeta, enquanto o
# metadata.json DENTRO do sidecar usa o formato COMPLETO (iPhone14,5 etc.).
# Centralizados aqui para não ter valores mágicos espalhados no código.

# Formato curto do POST /uploads (captura 072): só "model".
NATIVE_DEVICE_MODEL = os.environ.get("MINUTE_NATIVE_DEVICE_MODEL", "iPhone 13")
# Formato curto do POST /uploads (captura 072): só "os".
NATIVE_PLATFORM_OS = os.environ.get("MINUTE_NATIVE_PLATFORM_OS", "ios")
# Foto/código da câmera usada no upload ("built-in" ou "external").
NATIVE_CAMERA_SOURCE = os.environ.get("MINUTE_NATIVE_CAMERA_SOURCE", "built-in")
# Formato COMPLETO do device dentro do sidecar (iPhone14,5 = iPhone 13 técnico).
# Usado pelo metadata.json do .data.zip (o que alimenta os checks de integridade).
NATIVE_SIDECAR_MODEL = os.environ.get("MINUTE_NATIVE_SIDECAR_MODEL", "iPhone14,5")
NATIVE_SIDECAR_SYSTEM_NAME = os.environ.get("MINUTE_NATIVE_SIDECAR_SYSTEM_NAME", "iOS")
NATIVE_SIDECAR_SYSTEM_VERSION = os.environ.get("MINUTE_NATIVE_SIDECAR_SYSTEM_VERSION", "26.5")
# clockOffsetNs real observado na gravação d9f4fa6f (26.5.2) — usado no metadata.
NATIVE_CLOCK_OFFSET_NS = os.environ.get("MINUTE_NATIVE_CLOCK_OFFSET_NS", "-125")


def tokens_dir() -> Path:
    """Diretório onde ficam os `token_<email>.json` (gitignored)."""
    SECRETS_DIR.mkdir(exist_ok=True)
    return SECRETS_DIR


def token_path(email: str) -> Path:
    """Caminho do arquivo de token para um e-mail."""
    safe = email.replace("@", "_at_").replace(".", "_")
    return tokens_dir() / f"token_{safe}.json"
