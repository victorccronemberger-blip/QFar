"""
device_profile.py — Identidade de APARELHO por conta (anti-colusão).

A réplica replica o app ANDROID 1.22.0 (com.bakerdata.minute) em um Samsung
Galaxy S21–S24. Antes, todas as contas reportavam o MESMO aparelho único:
uptime congelado, calibração idêntica, `device={"model":"SM-S918B"}` em todo
upload e nenhum `X-Device-Id`. N contas com o mesmo relógio congelado é a
assinatura de colusão mais barata de detectar.

Aqui cada conta ganha um perfil de aparelho PRÓPRIO e persistente (Samsung):

  - device_id       — `android.ssaid:{ANDROID_ID}` → header `X-Device-Id`
                      exato do app (getAndroidId, 16 hex; fallback é
                      `android_no_ssaid`).
  - device_model    — Build.MODEL real do aparelho sorteado (im.',
                      `SM-S918B` = Galaxy S23 Ultra).
  - os_version      — release do Android (ex.: "14") e sdk_int (ex.: 34)
                      usados no metadata.json, UA e /app/opened.
  - boot_wall_ms    — último boot do aparelho; `uptime_ns_at()` devolve o
                      `SystemClock.elapsedRealtimeNanos` DERIVADO do momento
                      real (recorded_at), nunca uma constante. "Reboota"
                      sozinho após 21 dias.
  - calib           — intrinsics ultra-wide SAMSUNG preservados (fx/fy/cx/cy
                      no sensor nativo 4032x3024 + Brown-Conrady k1 k2 k3
                      p1 p2 + rolling shutter) com jitter determinístico POR
                      CONTA (celulares diferentes, mesmo chip).
  - frames_gop      — GOP do frames.csv (28-32; ~1 keyframe/s a 30 fps).
  - video_bitrate_mbps — bitrate real lido do MP4 da conta (7.4-8.8 Mbps).

Estado persistido em `data/device_state/<email>.json` (gitignored). O perfil é
PURE FUNCTION do e-mail (sem relógio na criação): mesmo perdendo o arquivo de
estado, a conta recria o MESMO aparelho — fixado, sem rotacionar. Somente o
"reboot" de 21d é dinâmico (e é persistido). Perfis legados são migrados
automaticamente para um Samsung novo na 1ª leitura.
Somente stdlib.
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

from . import config, device_catalog

# Uptime plausível de um Android usado no dia a dia: mínimo 6h (logo após boot)
# e máximo 21 dias (todo celular reinicia de quando em quando).
MIN_UPTIME_NS = 6 * 3600 * 1_000_000_000
MAX_UPTIME_NS = 21 * 86_400 * 1_000_000_000

_CALIB_PATH = Path(__file__).with_name("samsung_uw_calibration.json")


def _device_pool_from_catalog() -> list[tuple[str, str, int, tuple[tuple[str, int], ...]]]:
    """Vista legada do catálogo público (comercial, MODEL, peso, OS/SDK)."""
    pool: list[tuple[str, str, int, tuple[tuple[str, int], ...]]] = []
    for model in device_catalog.catalog_models():
        releases = tuple(
            (str(rel), device_catalog.api_level_for_release(str(rel)))
            for rel in (model.get("osReleases") or ["14"])
        )
        pool.append((
            str(model.get("commercial") or model["buildModel"]),
            str(model["buildModel"]),
            max(1, int(model.get("weight") or 1)),
            releases or (("14", 34),),
        ))
    return pool


# Pool Samsung S21–S24 (global B) — alimentado por resources/samsung_device_catalog.json.
DEVICE_POOL: list[tuple[str, str, int, tuple[tuple[str, int], ...]]] = (
    _device_pool_from_catalog())

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
    """Calibração ultra-wide Samsung por Build.MODEL (referência natural)."""
    try:
        return json.loads(_CALIB_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — sem arquivo, jitter sobre defaults
        return {}


_CALIB_DB = _load_calibration_base()


def _wall_ms_now() -> int:
    return int(time.time() * 1000)


# GET /devices/recording-config (captura 06/08): backlogCapMs = 14400000.
BACKLOG_CAP_MS = 14_400_000
_BACKLOG_SLACK_S = 60.0


def effective_backlog_cap_ms(limits: dict[str, int] | None = None) -> int:
    """Backlog em vigor — preferir limites da sessão; senão default local."""
    source = limits if isinstance(limits, dict) else config.recording_limits()
    return int(source.get("backlog_cap_ms") or BACKLOG_CAP_MS)


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

    O sidecar Android usa o mesmo formato do JS (3 dígitos de millis). Hardcode
    `.000Z` ou 6 dígitos (`.609000Z`) não bate com o app.
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
    backlog_cap_ms: int | None = None,
) -> float:
    """Início da gravação já terminada e ainda dentro do backlog."""
    now = time.time() if now is None else float(now)
    duration_s = max(0.0, float(duration_s))
    if backlog_cap_ms is None:
        backlog_cap_ms = effective_backlog_cap_ms()
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
    backlog_cap_ms: int | None = None,
) -> float:
    """Epoch do início: `agora - duração - gap`, preso à janela de backlog."""
    now = time.time() if now is None else float(now)
    if backlog_cap_ms is None:
        backlog_cap_ms = effective_backlog_cap_ms()
    start = now - max(0.0, float(duration_s)) - max(0.0, float(gap_s))
    return clamp_recording_start(
        start, duration_s, now=now, backlog_cap_ms=backlog_cap_ms)


def normalize_recorded_at(
    value: str,
    *,
    duration_s: float | None = None,
    now: float | None = None,
    adjust: bool = False,
    backlog_cap_ms: int | None = None,
) -> str:
    """Reemite `recorded_at` no formato nativo.

    Com `duration_s` e `adjust=False` (padrão), rejeita horários fora do backlog
    em vez de reescrevê-los. `adjust=True` preserva o clamp legado.
    """
    wall_ms = recorded_at_to_wall_ms(value)
    if wall_ms is None:
        raise ValueError(f"recorded_at inválido: {value!r}")
    epoch = wall_ms / 1000.0
    if duration_s is not None:
        if adjust:
            epoch = clamp_recording_start(
                epoch, duration_s, now=now, backlog_cap_ms=backlog_cap_ms)
        else:
            now_s = time.time() if now is None else float(now)
            cap_ms = (BACKLOG_CAP_MS if backlog_cap_ms is None
                      else int(backlog_cap_ms))
            start_ms = epoch * 1000.0
            duration_ms = max(0.0, float(duration_s) * 1000.0)
            now_ms = now_s * 1000.0
            if start_ms < now_ms - cap_ms or start_ms + duration_ms > now_ms:
                raise ValueError("recorded_at fora da janela de backlog permitida")
    return format_recorded_at(epoch)


@dataclass
class DeviceProfile:
    """Identidade de aparelho de uma conta (um Samsung "virtual" por conta)."""

    email: str
    device_id: str
    device_model: str = "SM-S918B"          # Build.MODEL (short e sidecar)
    sidecar_model: str = "SM-S918B"         # Build.MODEL completo (metadata.json)
    os_version: str = "14"                  # release do Android (UA, app/opened)
    sdk_int: int = 34                       # Build.VERSION.SDK_INT (metadata.json)
    sidecar_system_version: str = "14"      # systemVersion = Build.VERSION.RELEASE
    logical_camera_id: str = "4"            # id da câmera (camera_logical_X)
    boot_wall_ms: int = 0                   # último boot (epoch ms)
    created_wall_ms: int = 0                # 1ª vez que a conta usou a réplica
    frames_gop: int = 30
    video_bitrate_mbps: float = 8.0
    calib: dict[str, Any] = field(default_factory=dict)
    device_id_source: str = "unknown"
    from_anchor: bool = False
    anchor_owner: bool = False

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
            "sdk_int", "sidecar_system_version", "logical_camera_id",
            "boot_wall_ms", "created_wall_ms", "frames_gop",
            "video_bitrate_mbps", "calib", "device_id_source", "from_anchor", "anchor_owner")
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
        """Uptime do sistema (ns, base SystemClock.elapsedRealtimeNanos).

        Regras de plausibilidade (mesmo domínio do app Android):
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
        """UA do HTTP Android: OkHttp default (`okhttp/4.12.0` no APK) — o app
        não sobrescreve o header; é o MESMO para todos os aparelhos."""
        return config.USER_AGENT

    def location_header_value(self) -> str | None:
        """Valor EXATO do `formatDeviceLocationHeader` do bundle:
        `<lat 6 casas>,<lon 6 casas>,<accuracy arredondada>,<isMock>`."""
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
        # isMock em JS (String(bool) → "true"/"false" minúsculo), mesmo formato
        # do concat do formatDeviceLocationHeader do bundle.
        return f"{lat:.6f},{lng:.6f},{round(accuracy)},false"

    def headers(self, *, include_location: bool = True) -> dict[str, str]:
        """Headers de identidade que o app Android 1.22.0 envia.

        `include_location=False` é usado para rotas sem gate geográfico — o app
        só envia `X-Device-Location` nas rotas de quota/elegibilidade geo
        (DETALHAMENTO §2.1), nunca em TODA chamada.
        """
        out = {
            "X-App-Version": config.APP_VERSION,
            "User-Agent": self.user_agent(),
            "X-Device-Id": self.device_id,
        }
        if include_location:
            location = self.location_header_value()
            if location:
                out["X-Device-Location"] = location
        return out

    def opened_payload(self, auth_method: str = "SESSION_RESUMED",
                       opened_at: str | None = None) -> dict[str, Any]:
        """Corpo do POST /api/v1/app/opened (Hermes `buildAppOpenedPayload`).

        `os_version` = `android ${Platform.Version}` → API level (SDK_INT).
        `opened_at` é opcional na OpenAPI; o APK não envia.
        """
        body: dict[str, Any] = {
            "auth_method": auth_method,
            "app_version": config.APP_VERSION,
            "device_model": self.device_model,
            "os_version": f"android {int(self.sdk_int)}",
        }
        if opened_at:
            body["opened_at"] = opened_at
        return body

    def sidecar_device_meta(self) -> dict[str, str]:
        """device do metadata.json (Shape Android: model + systemName/Version)."""
        return {
            "model": self.sidecar_model,
            "systemName": config.NATIVE_SIDECAR_SYSTEM_NAME,
            "systemVersion": self.sidecar_system_version,
        }

    def sidecar_platform_meta(self) -> dict[str, Any]:
        """platform do metadata.json: `{type:'android', version:sdkInt}`."""
        return {
            "type": config.NATIVE_PLATFORM_OS,
            "version": int(self.sdk_int),
        }

    def upload_device_meta(self) -> dict[str, str]:
        """getDeviceUploadMeta curto do POST /uploads: Build.MODEL apenas."""
        return {"model": self.device_model}

    def upload_platform_meta(self) -> dict[str, str]:
        """getDeviceUploadMeta curto do POST /uploads: só `os`."""
        return {"os": config.NATIVE_PLATFORM_OS}


def get_profile(email: str, first_use_ms: int | None = None) -> DeviceProfile:
    """Perfil do aparelho da conta (cria e persiste na 1ª vez).

    `first_use_ms` (epoch ms) fixa o 1º uso virtual do aparelho na criação —
    ex.: o instante de registro da conta (mtime do token). Sem ele, cai na
    referência do 1º lote (18/08).

    Perfis legados são substituídos por um Samsung novo na 1ª leitura — a
    identidade de aparelho rotaciona UMA vez na migração.
    """
    with _cache_lock:
        cached = _cache.get(email)
    if cached is not None:
        return cached

    path = _profile_path(email)
    profile: DeviceProfile | None = None
    if path.exists():
        try:
            candidate = DeviceProfile.from_dict(
                json.loads(path.read_text(encoding="utf-8")))
            if (not candidate.device_id
                    or not candidate.device_id.startswith("android.ssaid:")
                    or not isinstance(candidate.calib, dict)
                    or not candidate.calib.get("distortion_model")):
                profile = None  # perfil legado/inválido — recria como Android
            else:
                # O nome do arquivo é a fonte de verdade. Um estado copiado de
                # outra conta não pode continuar escrevendo no caminho alheio.
                candidate.email = email
                profile = candidate
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


def _model_ref(build_model: str) -> dict[str, Any]:
    """Referência de calibração ultra-wide de um Build.MODEL Samsung."""
    models = _CALIB_DB.get("models") or {}
    ref = models.get(build_model) or {}
    if not ref:
        ref = {"fx": 1545.0, "fy": 1543.0, "k1": -0.24, "k2": 0.12,
               "k3": -0.035, "p1": 0.001, "p2": -0.002, "readoutS": 0.0104,
               "logicalCameraId": "4"}
    # cx/cy por modelo (coletados do aparelho real) — fallback no centro
    # compartilhado do arquivo, senão no centro do sensor nativo.
    if not ref.get("cx"):
        ref = dict(ref)
        center = _CALIB_DB.get("reference") or {"cx": 2016.0, "cy": 1512.0}
        ref["cx"] = float(center.get("cx") or 2016.0)
        ref["cy"] = float(center.get("cy") or 1512.0)
    return ref


def _create_profile(email: str, first_use_ms: int | None = None) -> DeviceProfile:
    """Gera um perfil novo — PURE FUNCTION de (e-mail, 1º uso).

    Com âncora USB ativa, propaga a família do S22 (modelo/OS/SDK) e gera
    SSAID sintético por conta. Sem âncora, usa o catálogo público ponderado.
    """
    seed = f"moneymin.android:{email}"
    rng = random.Random(seed)
    identity = device_catalog.generate_identity(email)

    # 1º uso virtual: QUANDO a conta nasceu (first_use_ms, ex.: registro/token).
    if first_use_ms is not None:
        created_wall_ms = min(int(first_use_ms), _wall_ms_now())
    else:
        created_wall_ms = _FIRST_USE_REF_MS + int(rng.uniform(0.0, 3 * 86_400 * 1_000))
    # Último reboot: 6h–3d ANTES do 1º uso — uptime plausível em qualquer instante.
    boot_wall_ms = created_wall_ms - int(
        rng.uniform(MIN_UPTIME_NS / 1e6, 3 * 86_400 * 1_000))
    return DeviceProfile(
        email=email,
        device_id=str(identity["device_id"]),
        device_model=str(identity["device_model"]),
        sidecar_model=str(identity["sidecar_model"]),
        os_version=str(identity["os_version"]),
        sdk_int=int(identity["sdk_int"]),
        sidecar_system_version=str(identity["sidecar_system_version"]),
        logical_camera_id=str(identity["logical_camera_id"]),
        boot_wall_ms=boot_wall_ms,
        created_wall_ms=created_wall_ms,
        frames_gop=int(identity["frames_gop"]),
        video_bitrate_mbps=float(identity["video_bitrate_mbps"]),
        calib=dict(identity["calib"]),
        device_id_source=str(identity.get("device_id_source", "unknown")),
        from_anchor=identity.get("from_anchor") is True,
        anchor_owner=identity.get("anchor_owner") is True,
    )


def repair_future_timestamps(
    profile: DeviceProfile,
    *,
    first_use_ms: int | None = None,
    now_ms: int | None = None,
) -> bool:
    """Migra perfis com relógio no futuro: preserva identidade, desloca boot.

    Devolve True se houve reparo.
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
