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
INTEGRATIONS_PATH = SECRETS_DIR / "integrations.dat"
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


_PROCESS_ENV_KEYS = set(os.environ)
_load_dotenv(ROOT / ".env")

# O cofre local vence o `.env` legado, mas nunca uma variável fornecida
# explicitamente ao processo. Assim a UI pode substituir credenciais antigas
# sem quebrar automações ou ambientes administrados externamente.
try:
    from .secure_store import load_secure_settings
    _SECURE_SETTINGS = load_secure_settings(INTEGRATIONS_PATH)
except (OSError, RuntimeError, ValueError):
    _SECURE_SETTINGS = {}


def _secure_env(key: str, value: object) -> None:
    if key not in _PROCESS_ENV_KEYS and str(value or "").strip():
        os.environ[key] = str(value).strip()


_secure_hostinger = _SECURE_SETTINGS.get("hostinger") or {}
if isinstance(_secure_hostinger, dict):
    _secure_env("HOSTINGER_MAIL_TOKEN", _secure_hostinger.get("token"))
    _secure_env("HOSTINGER_MAILBOX_ID", _secure_hostinger.get("mailbox_id"))

_secure_ego4d = _SECURE_SETTINGS.get("ego4d") or {}
if isinstance(_secure_ego4d, dict):
    _secure_env("AWS_ACCESS_KEY_ID", _secure_ego4d.get("access_key_id"))
    _secure_env("AWS_SECRET_ACCESS_KEY", _secure_ego4d.get("secret_access_key"))
    _secure_env("AWS_SESSION_TOKEN", _secure_ego4d.get("session_token"))
    _secure_env("EGO4D_AWS_REGION", _secure_ego4d.get("region"))
    if ("EGO4D_AWS_PROFILE" not in _PROCESS_ENV_KEYS
            and _secure_ego4d.get("access_key_id")
            and _secure_ego4d.get("secret_access_key")):
        # Perfil vazio faz boto3 e o fallback SigV4 usarem as chaves do cofre.
        os.environ["EGO4D_AWS_PROFILE"] = ""

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

# Versão do app usada nos headers (bate com o APK Android v1.22.0 / APKPure).
# versionCode 1004023 (targetSdk 36). Backend exige min 1.21.0; 1.22.0 é a release atual.
APP_VERSION = os.environ.get("MINUTE_APP_VERSION", "1.22.0")
ANDROID_VERSION_CODE = os.environ.get("MINUTE_ANDROID_VERSION_CODE", "1004023")
# UA Android: TODO o HTTP passa pelo OkHttp (RN fetch + AzureBlockUploader) com
# o header default `okhttp/<versão>` — o bundle não contém nenhum literal de UA
# custom, e o APK declara okhttp/4.12.0. O OkHttp manda o MESMO header para
# todos os aparelhos (a identidade fica em X-Device-Id/X-App-Version).
USER_AGENT = os.environ.get("MINUTE_USER_AGENT", "okhttp/4.12.0")
# Android no Brasil (todas as contas operam daqui).
ACCEPT_LANGUAGE = os.environ.get("MINUTE_ACCEPT_LANGUAGE", "pt-BR,pt;q=0.9")
# Header X-Device-Location (app Android 1.22.0): CSV lat,lon,accuracy,isMock
# (formatDeviceLocationHeader no bundle: lat.toFixed(6),lon.toFixed(6),round(acc),isMock).
# Só envia se LAT/LNG estiverem no ambiente — não inventa GPS.
try:
    DEVICE_LAT = float(os.environ["MINUTE_DEVICE_LAT"]) if os.environ.get("MINUTE_DEVICE_LAT") else None
except ValueError:
    DEVICE_LAT = None
try:
    DEVICE_LNG = float(os.environ["MINUTE_DEVICE_LNG"]) if os.environ.get("MINUTE_DEVICE_LNG") else None
except ValueError:
    DEVICE_LNG = None
try:
    DEVICE_LOCATION_ACCURACY = float(os.environ.get("MINUTE_DEVICE_ACCURACY", "12"))
except ValueError:
    DEVICE_LOCATION_ACCURACY = 12.0
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


# --- Perfil nativo do app ANDROID (réplica do upload) ------------------------
# Valores extraídos do APK Android v1.22.0 (jadx_out/sources/app/useminute):
# o meta do POST /uploads usa o formato CURTO getDeviceUploadMeta
# (platform={os}, device={model}), enquanto o metadata.json DENTRO do sidecar
# usa o formato COMPLETO (platform={type,version:sdk} + device com
# systemName/systemVersion). O aparelho de cada conta vem do device_profile
# (pool Samsung Galaxy S21–S24); estes são apenas os fallbacks de uploads
# avulsos sem perfil.

# Formato curto do POST /uploads: só "model" (Build.MODEL de um Samsung comum).
NATIVE_DEVICE_MODEL = os.environ.get("MINUTE_NATIVE_DEVICE_MODEL", "SM-S918B")
# Formato curto do POST /uploads: só "os".
NATIVE_PLATFORM_OS = os.environ.get("MINUTE_NATIVE_PLATFORM_OS", "android")
# Formato COMPLETO do device dentro do sidecar (Build.MODEL + Android release).
NATIVE_SIDECAR_MODEL = os.environ.get("MINUTE_NATIVE_SIDECAR_MODEL", "SM-S918B")
NATIVE_SIDECAR_SYSTEM_NAME = os.environ.get("MINUTE_NATIVE_SIDECAR_SYSTEM_NAME", "Android")
NATIVE_SIDECAR_SYSTEM_VERSION = os.environ.get("MINUTE_NATIVE_SIDECAR_SYSTEM_VERSION", "14")

# IMU do pipeline "ego" do app (EgoImu.SAMPLING_PERIOD_US = 2000 -> 500 Hz).
# A câmera Trinet externa roda a 562 Hz (TrinetImuCsv.IMU_SAMPLE_RATE_HZ).
ANDROID_IMU_SAMPLE_RATE_HZ = 500

# O fingerprint OkHttp/Android exige curl_cffi; o fallback urllib é Python puro
# (JA3 próprio) e é o elo mais fraco. Em produção é recomendado exigir curl.
REQUIRE_CURL = os.environ.get("MINUTE_REQUIRE_CURL", "").strip() == "1"


# --- Limites efetivos de gravação (default = recording-config real 06/08) ----
# Aplicados pelo warmup quando o GET /devices/recording-config responde; o
# servidor pode mudar min/max/backlog e o app APLICA — a réplica acompanha.
_EFFECTIVE_LIMITS: dict[str, int] = {
    "min_duration_ms": 60_000,
    "max_duration_ms": 1_800_000,
    "backlog_cap_ms": 14_400_000,
}


def recording_limits() -> dict[str, int]:
    """Limites de gravação em vigor (min/max duração + backlog)."""
    return dict(_EFFECTIVE_LIMITS)


def apply_recording_config(payload: dict) -> None:
    """Funde a `nativeCameraPolicy`/recording-config remota nos limites locais.

    Sanidade: min em [1s, 1h], max em [min, 6h], backlog em [1min, 24h].
    Campos ausentes preservam o valor atual.
    """
    if not isinstance(payload, dict):
        return
    try:
        min_ms = int(payload.get("minDurationMs") or _EFFECTIVE_LIMITS["min_duration_ms"])
        min_ms = max(1_000, min(3_600_000, min_ms))
        max_ms = int(payload.get("maxDurationMs") or _EFFECTIVE_LIMITS["max_duration_ms"])
        max_ms = max(min_ms, min(21_600_000, max_ms))
        backlog = int(payload.get("backlogCapMs") or _EFFECTIVE_LIMITS["backlog_cap_ms"])
        backlog = max(60_000, min(86_400_000, backlog))
        _EFFECTIVE_LIMITS["min_duration_ms"] = min_ms
        _EFFECTIVE_LIMITS["max_duration_ms"] = max_ms
        _EFFECTIVE_LIMITS["backlog_cap_ms"] = backlog
    except (TypeError, ValueError):
        pass

# clockDomain exato do telefone no metadata (EgoCameraController.clockDomain).
ANDROID_CLOCK_DOMAIN = os.environ.get("MINUTE_ANDROID_CLOCK_DOMAIN", "android_elapsedRealtimeNanos")


def tokens_dir() -> Path:
    """Diretório onde ficam os `token_<email>.json` (gitignored)."""
    SECRETS_DIR.mkdir(exist_ok=True)
    return SECRETS_DIR


def token_path(email: str) -> Path:
    """Caminho do arquivo de token para um e-mail."""
    safe = email.replace("@", "_at_").replace(".", "_")
    return tokens_dir() / f"token_{safe}.json"
