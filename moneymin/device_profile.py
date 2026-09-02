"""
device_profile.py — Identidade de APARELHO por conta (anti-colusão).

Antes, todas as contas reportavam o MESMO iPhone: uptime congelado em
`224_584_000_000_000` ns no metadata.json, calibração de câmera idêntica,
`clockOffsetNs` igual, `device={"model":"iPhone 13"}` em todo upload e nenhum
`X-Device-Id` (o app 1.22.0 envia). N contas com o mesmo relógio congelado é
a assinatura de colusão mais barata de detectar.

Aqui cada conta ganha um perfil de aparelho PRÓPRIO e persistente:

  - device_id       — UUID estável → header `X-Device-Id` (app 1.22.0+)
  - boot_wall_ms    — último boot do aparelho; `ios_systemUptimeNs` é DERIVADO
                      do momento real (recorded_at) via `uptime_ns_at()`, nunca
                      uma constante. "Reboota" sozinho após 21 dias.
  - calib           — calibração ultra-wide do iPhone 13 com jitter
                      determinístico POR CONTA (celulares diferentes, mesmo chip)
  - clock_offset_ns — offset sensor↔wall clock com jitter por conta
  - frames_gop      — GOP do frames.csv (28-32; iPhone faz ~1 keyframe/s)
  - video_bitrate_mbps — alvo ABR do re-encode POR CONTA (7.4-8.8 Mbps)
  - os_version      — release point do iOS (26.5.x) usada no UA e no app/opened

Estado persistido em `data/device_state/<email>.json` (gitignored). O perfil é
PURE FUNCTION do e-mail (sem relógio na criação): mesmo perdendo o arquivo de
estado, a conta recria o MESMO aparelho — fixado, sem rotacionar. Somente o
"reboot" de 21d é dinâmico (e é persistido). Somente stdlib.
"""
from __future__ import annotations

import json
import math
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config

# Uptime plausível de um iPhone usado no dia a dia: mínimo 6h (logo após boot)
# e máximo 21 dias (todo celular reinicia de quando em quando).
MIN_UPTIME_NS = 6 * 3600 * 1_000_000_000
MAX_UPTIME_NS = 21 * 86_400 * 1_000_000_000

_CALIB_PATH = Path(__file__).with_name("iphone_uw_calibration.json")

# Aparelhos compatíveis com o app: iPhone 12 ou mais novo. (nome comercial,
# código técnico, peso de sorteio). A calibração de referência é a ultra-wide
# do iPhone 13 (iPhone14,5) — cada geração tem lente própria, então cada
# modelo recebe um offset DETERMINÍSTICO de intrinsics (±4% em fx/fy) em cima
# da referência, mais o jitter por conta.
DEVICE_POOL: list[tuple[str, str, int]] = [
    ("iPhone 12", "iPhone13,1", 12),
    ("iPhone 13", "iPhone14,5", 30),
    ("iPhone 14", "iPhone14,7", 28),
    ("iPhone 15", "iPhone15,4", 20),
    ("iPhone 16", "iPhone17,3", 10),
]

_cache: dict[str, DeviceProfile] = {}
_cache_lock = threading.Lock()


def _state_dir() -> Path:
    d = config.DATA_DIR / "device_state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profile_path(email: str) -> Path:
    if (not email or email != email.strip()
            or any(ord(ch) < 32 or ch in '<>:"/\\|?*' for ch in email)):
        raise ValueError("email inválido para perfil de dispositivo")
    safe = email.replace("@", "_at_").replace(".", "_")
    return _state_dir() / f"device_{safe}.json"


def _load_calibration_base() -> dict[str, Any]:
    """Calibração REAL da ultra-wide do iPhone 14,5 (chip de referência)."""
    try:
        data = json.loads(_CALIB_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — sem arquivo, jitter sobre zeros
        return {}
    return data.get("calibration") or {}


def _wall_ms_now() -> int:
    return int(time.time() * 1000)


# GET /devices/recording-config (captura 06/08): backlogCapMs = 14400000.
BACKLOG_CAP_MS = 14_400_000
_BACKLOG_SLACK_S = 60.0


def recorded_at_to_wall_ms(recorded_at: str) -> int | None:
    """Converte `recorded_at` (ISO-8601 com Z) em epoch ms. None se inválido."""
    import datetime
    try:
        return int(
            datetime.datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
            .timestamp() * 1000
        )
    except Exception:  # noqa: BLE001
        return None


def format_recorded_at(epoch_s: float) -> str:
    """ISO-8601 como `Date.toISOString()` do React Native: `YYYY-MM-DDTHH:mm:ss.sssZ`.

    O sidecar real (sessão 627d39d8) usa 3 dígitos (`.562Z`). Hardcode `.000Z`
    ou 6 dígitos (`.609000Z`) não bate com o app.
    """
    ms_total = int(round(float(epoch_s) * 1000.0))
    sec, milli = divmod(ms_total, 1000)
    if sec < 0:
        sec, milli = 0, 0
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sec)) + f".{milli:03d}Z"


def clamp_recording_start(
    start_epoch_s: float,
    duration_s: float = 0.0,
    *,
    now: float | None = None,
    backlog_cap_ms: int = BACKLOG_CAP_MS,
) -> float:
    """Início da gravação já terminada e ainda dentro do backlog (4h)."""
    now = time.time() if now is None else float(now)
    duration_s = max(0.0, float(duration_s))
    cap_s = max(1.0, float(backlog_cap_ms) / 1000.0)
    latest = now - duration_s
    earliest = now - cap_s + _BACKLOG_SLACK_S
    if earliest > latest:
        return latest
    start = float(start_epoch_s)
    if start < earliest:
        return earliest
    if start > latest:
        return latest
    return start


def recording_start_epoch(
    duration_s: float,
    *,
    now: float | None = None,
    gap_s: float = 0.0,
    backlog_cap_ms: int = BACKLOG_CAP_MS,
) -> float:
    """Epoch do início: `agora - duração - gap`, preso à janela de backlog."""
    now = time.time() if now is None else float(now)
    start = now - max(0.0, float(duration_s)) - max(0.0, float(gap_s))
    return clamp_recording_start(
        start, duration_s, now=now, backlog_cap_ms=backlog_cap_ms)


def normalize_recorded_at(
    value: str,
    *,
    duration_s: float | None = None,
    now: float | None = None,
) -> str:
    """Reemite `recorded_at` no formato nativo; opcionalmente prende ao backlog."""
    wall_ms = recorded_at_to_wall_ms(value)
    epoch = (wall_ms / 1000.0) if wall_ms is not None else (
        time.time() if now is None else float(now))
    if duration_s is not None:
        epoch = clamp_recording_start(epoch, duration_s, now=now)
    return format_recorded_at(epoch)


@dataclass
class DeviceProfile:
    """Identidade de aparelho de uma conta (um celular "virtual" por conta)."""

    email: str
    device_id: str
    device_model: str = "iPhone 13"          # formato curto (POST /uploads, app/opened)
    sidecar_model: str = "iPhone14,5"        # formato técnico (metadata.json)
    os_version: str = "26.5.2"               # release point do iOS (UA, app/opened)
    sidecar_system_version: str = "26.5"     # systemVersion do metadata.json real
    boot_wall_ms: int = 0                    # último boot (epoch ms)
    created_wall_ms: int = 0                 # 1ª vez que a conta usou a réplica
    clock_offset_ns: int = -125
    frames_gop: int = 30
    video_bitrate_mbps: float = 8.0
    calib: dict[str, Any] = field(default_factory=dict)

    # --- persistência -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeviceProfile:
        # Perfis de versões anteriores podem não conter os campos mais novos.
        # Não passe None explicitamente: isso apagaria os defaults do dataclass
        # e só explodiria mais tarde em abs(), headers ou no ffmpeg.
        known = {f: data[f] for f in (
            "email", "device_id", "device_model", "sidecar_model", "os_version",
            "sidecar_system_version", "boot_wall_ms", "created_wall_ms",
            "clock_offset_ns", "frames_gop", "video_bitrate_mbps", "calib")
                 if f in data and data[f] is not None}
        return cls(**known)

    def _persist(self) -> None:
        try:
            _profile_path(self.email).write_text(
                json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass  # melhor esforço — o perfil segue vivo em memória

    # --- tempo --------------------------------------------------------------
    def uptime_ns_at(self, wall_ms: int) -> int:
        """Uptime do sistema (ns) no instante `wall_ms` (epoch ms).

        Regras de plausibilidade:
          - < 6h  → skew de relógio/localmente recém-bootado: empurra um boot
            24h antes (uptime mínimo plausível);
          - > 21d → o celular "rebootou": novo boot entre 6h e 3d atrás
            (persistido, determinístico por dia).
        """
        # wall em NS (boot fica em ms no perfil) — as comparações são todas em ns
        wall_ns = int(wall_ms) * 1_000_000
        u = wall_ns - int(self.boot_wall_ms) * 1_000_000
        if u < MIN_UPTIME_NS:
            u += 86_400 * 1_000_000_000
        if u > MAX_UPTIME_NS:
            day = int(wall_ms) // 86_400_000
            rng = random.Random(f"moneymin.reboot:{self.device_id}:{day}")
            self.boot_wall_ms = int(wall_ms) - rng.randint(
                int(MIN_UPTIME_NS / 1e6), 3 * 86_400_000)
            u = wall_ns - int(self.boot_wall_ms) * 1_000_000
            self._persist()
        return u

    # --- saídas usadas pela réplica -----------------------------------------
    def user_agent(self) -> str:
        """UA por conta: a forma é a da captura mitm, mas o iOS varia por conta."""
        return (f"Minute/{config.APP_VERSION} (com.bakerdata.minute; "
                f"build:1; iOS {self.os_version})")

    def location_header_value(self) -> str | None:
        """JSON do `toFix` nativo: latitude, longitude, accuracy, isMock."""
        lat = getattr(self, "latitude", None)
        lng = getattr(self, "longitude", None)
        if lat is None:
            lat = config.DEVICE_LAT
        if lng is None:
            lng = config.DEVICE_LNG
        if lat is None or lng is None:
            return None
        try:
            lat = float(lat)
            lng = float(lng)
            accuracy = float(
                getattr(self, "location_accuracy", None)
                or config.DEVICE_LOCATION_ACCURACY
            )
        except (TypeError, ValueError):
            return None
        if (not math.isfinite(lat) or not -90.0 <= lat <= 90.0
                or not math.isfinite(lng) or not -180.0 <= lng <= 180.0):
            return None
        if not math.isfinite(accuracy) or accuracy <= 0:
            accuracy = 12.0
        return json.dumps({
            "latitude": lat,
            "longitude": lng,
            "accuracy": accuracy,
            "isMock": False,
        }, separators=(",", ":"))

    def headers(self) -> dict[str, str]:
        """Headers de identidade que o app 1.22.0 envia em toda chamada."""
        out = {
            "X-App-Version": config.APP_VERSION,
            "User-Agent": self.user_agent(),
            "X-Device-Id": self.device_id,
        }
        location = self.location_header_value()
        if location:
            out["X-Device-Location"] = location
        return out

    def opened_payload(self, auth_method: str = "SESSION_RESUMED",
                       opened_at: str | None = None) -> dict[str, Any]:
        """Corpo do POST /api/v1/app/opened (telemetria de abertura do app)."""
        body: dict[str, Any] = {
            "auth_method": auth_method,
            "app_version": config.APP_VERSION,
            "device_model": self.device_model,
            "os_version": f"ios {self.os_version}",
        }
        if opened_at:
            body["opened_at"] = opened_at
        return body

    def sidecar_device_meta(self) -> dict[str, str]:
        """device COMPLETO do metadata.json (modelo técnico + systemName/Version)."""
        return {
            "model": self.sidecar_model,
            "systemName": config.NATIVE_SIDECAR_SYSTEM_NAME,
            "systemVersion": self.sidecar_system_version,
        }

    def sidecar_platform_meta(self) -> dict[str, str]:
        """platform do metadata.json (`os` + `version` = systemVersion)."""
        return {
            "os": config.NATIVE_PLATFORM_OS,
            "version": self.sidecar_system_version,
        }

    def upload_device_meta(self) -> dict[str, str]:
        """getDeviceUploadMeta curto do POST /uploads: só o nome comercial."""
        return {"model": self.device_model}

    def upload_platform_meta(self) -> dict[str, str]:
        """getDeviceUploadMeta curto do POST /uploads: só `os`."""
        return {"os": config.NATIVE_PLATFORM_OS}


def get_profile(email: str, first_use_ms: int | None = None) -> DeviceProfile:
    """Perfil do aparelho da conta (cria e persiste na 1ª vez).

    `first_use_ms` (epoch ms) fixa o 1º uso virtual do aparelho na criação —
    ex.: o instante de registro da conta (mtime do token). Sem ele, cai na
    referência do 1º lote (18/08).
    """
    with _cache_lock:
        cached = _cache.get(email)
    if cached is not None:
        return cached

    path = _profile_path(email)
    profile: DeviceProfile | None = None
    if path.exists():
        try:
            profile = DeviceProfile.from_dict(
                json.loads(path.read_text(encoding="utf-8")))
            if not profile.device_id or not isinstance(profile.calib, dict):
                profile = None
            else:
                # O nome do arquivo é a fonte de verdade. Um estado copiado de
                # outra conta não pode continuar escrevendo no caminho alheio.
                profile.email = email
        except (json.JSONDecodeError, ValueError, TypeError):
            profile = None

    if profile is None:
        profile = _create_profile(email, first_use_ms=first_use_ms)
        profile._persist()

    with _cache_lock:
        _cache[email] = profile
    return profile


# Referência do 1º lote de contas (batch de 18/08). O "primeiro uso" virtual
# de cada conta é DERIVADO daqui (nunca do relógio da máquina): a mesma conta
# recriando o perfil em qualquer máquina/data recria o MESMO aparelho — o
# aparelho é fixado por e-mail e não fica rotacionando.
_FIRST_USE_REF_MS = 1_787_011_200_000  # 2026-08-18T00:00:00Z (epoch ms)


def _create_profile(email: str, first_use_ms: int | None = None) -> DeviceProfile:
    """Gera um perfil novo — PURE FUNCTION de (e-mail, 1º uso).

    Todo campo é derivado da seed `moneymin.device:<email>` (+ o instante de
    1º uso, fixo por conta): mesmo perdendo o
    `data/device_state/<email>.json`, a recriação reproduz idêntico aparelho
    (mesmo modelo, calibração, boot e idade). O único estado dinâmico é o
    "reboot" automático de `uptime_ns_at()` após 21d (persistido por dia).

    `first_use_ms`: instante real de registro da conta (ex.: mtime do token) —
    o aparelho nasce QUANDO a conta nasceu. Contas sem essa referência caem no
    1º lote (18/08).
    """
    seed = f"moneymin.device:{email}"
    rng = random.Random(seed)
    base = _load_calibration_base()

    # Aparelho da conta: iPhone 12 ou mais novo (compatibilidade do app),
    # sorteado com pesos — as contas NÃO são um enxame de iPhone 13 idênticos.
    device_model, sidecar_model = rng.choices(
        [(m[0], m[1]) for m in DEVICE_POOL],
        weights=[m[2] for m in DEVICE_POOL])[0]

    # Calibração: referência real do iPhone 13 + offset DETERMINÍSTICO por
    # MODELO (lente de geração diferente, ±4% em fx/fy) + jitter fino POR
    # CONTA (mesmo chip, montagem diferente).
    model_rng = random.Random(f"moneymin.model:{sidecar_model}")
    model_fx = model_rng.uniform(-0.04, 0.04)
    model_fy = model_rng.uniform(-0.04, 0.04)
    model_c = model_rng.uniform(-0.003, 0.003)
    fx = float(base.get("fx") or 1552.02)
    fy = float(base.get("fy") or 1552.02)
    cx = float(base.get("cx") or 2009.94)
    cy = float(base.get("cy") or 1519.41)
    center_x = float(base.get("centerX") or 2015.02)
    center_y = float(base.get("centerY") or 1514.35)
    calib = {
        "fx": round(fx * (1.0 + model_fx) * (1.0 + rng.uniform(-0.0012, 0.0012)), 6),
        "fy": round(fy * (1.0 + model_fy) * (1.0 + rng.uniform(-0.0012, 0.0012)), 6),
        "cx": round(cx * (1.0 + model_c) + rng.uniform(-1.2, 1.2), 3),
        "cy": round(cy * (1.0 + model_c) + rng.uniform(-1.2, 1.2), 3),
        "centerX": round(center_x + rng.uniform(-0.4, 0.4), 2),
        "centerY": round(center_y + rng.uniform(-0.4, 0.4), 2),
        "referenceWidth": int(base.get("referenceWidth") or 4032),
        "referenceHeight": int(base.get("referenceHeight") or 3024),
        "lensDistortionLookupTable":
            list(base.get("lensDistortionLookupTable") or []),
        "inverseLensDistortionLookupTable":
            list(base.get("inverseLensDistortionLookupTable") or []),
        "pixelSizeMm": float(base.get("pixelSizeMm") or 0.001),
    }

    # 1º uso virtual: QUANDO a conta nasceu (first_use_ms, ex.: registro/token).
    # Não some jitter aqui: ao criar um perfil imediatamente após a conta, isso
    # colocava o primeiro uso até 6h NO FUTURO. Contas do 1º lote (sem
    # referência própria) usam o lote de 18/08 + jitter (0–3d) — idade
    # plausível para quem já vem subindo gravações desde a semana 18/08.
    if first_use_ms is not None:
        created_wall_ms = min(int(first_use_ms), _wall_ms_now())
    else:
        created_wall_ms = _FIRST_USE_REF_MS + int(rng.uniform(0.0, 3 * 86_400 * 1_000))
    # Último reboot: 6h–3d ANTES do 1º uso — o uptime é plausível em qualquer
    # instante (o "reboot" automático de 21d cuida do resto).
    boot_wall_ms = created_wall_ms - int(
        rng.uniform(MIN_UPTIME_NS / 1e6, 3 * 86_400 * 1_000))
    return DeviceProfile(
        email=email,
        device_id=str(uuid.uuid5(uuid.NAMESPACE_URL, seed)),
        device_model=device_model,
        sidecar_model=sidecar_model,
        os_version=rng.choices(("26.5.2", "26.5.1", "26.5"),
                              weights=(70, 20, 10))[0],
        sidecar_system_version="26.5",
        boot_wall_ms=boot_wall_ms,
        created_wall_ms=created_wall_ms,
        clock_offset_ns=rng.randint(-135, -115),  # real: -125/-126
        frames_gop=rng.randint(28, 32),
        video_bitrate_mbps=round(rng.uniform(7.4, 8.8), 1),
        calib=calib,
    )


def repair_future_timestamps(
    profile: DeviceProfile,
    *,
    first_use_ms: int | None = None,
    now_ms: int | None = None,
) -> bool:
    """Migra perfis da versão que somava jitter ao primeiro uso conhecido.

    Preserva toda a identidade do aparelho e o intervalo boot→primeiro uso;
    apenas desloca os dois relógios para trás. Devolve True se houve reparo.
    """
    now_ms = int(now_ms if now_ms is not None else _wall_ms_now())
    known_first_use = int(first_use_ms) if first_use_ms is not None else None
    if (profile.created_wall_ms <= now_ms
            and (known_first_use is None
                 or profile.created_wall_ms <= known_first_use)):
        return False
    target_ms = min(known_first_use if known_first_use is not None else now_ms, now_ms)
    shift_ms = int(profile.created_wall_ms) - target_ms
    profile.created_wall_ms = target_ms
    profile.boot_wall_ms = int(profile.boot_wall_ms) - shift_ms
    profile._persist()
    return True


def profile_age_days(email: str) -> float:
    """Idade do perfil da conta em dias (0 se não existe)."""
    path = _profile_path(email)
    if not path.exists():
        return 0.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        created = int(data.get("created_wall_ms") or 0)
        return max(0.0, (_wall_ms_now() - created) / 86_400_000)
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0.0
