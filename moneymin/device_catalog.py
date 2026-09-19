"""Catálogo público Samsung + geração determinística de identidade de aparelho.

Alimenta `device_profile` com Build.MODEL / OS / SDK vindos de fontes públicas
(Play supported_devices, MobileModels, SamMobile). Com âncora USB ativa,
novos perfis propagam a família do aparelho real (modelo/OS/SDK) e geram
identificador sintético por e-mail. Um ID informado na âncora tem procedência
declarada, não verificada automaticamente por este módulo.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import random
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config

_CATALOG_PATH = Path(__file__).with_name("resources") / "samsung_device_catalog.json"
_CALIB_PATH = Path(__file__).with_name("samsung_uw_calibration.json")


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    return json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_calibration() -> dict[str, Any]:
    try:
        return json.loads(_CALIB_PATH.read_text(encoding="utf-8"))
    except OSError:
        return {}


def api_level_for_release(release: str) -> int:
    levels = load_catalog().get("apiLevels") or {}
    if release in levels:
        return int(levels[release])
    # Fallback conservador se o catálogo não listar a release.
    try:
        major = int(str(release).split(".", 1)[0])
    except ValueError:
        return 34
    return {12: 31, 13: 33, 14: 34, 15: 35, 16: 36}.get(major, 34)


def catalog_models() -> list[dict[str, Any]]:
    models = load_catalog().get("models") or []
    return [m for m in models if isinstance(m, dict) and m.get("buildModel")]


def pick_model(rng: random.Random) -> dict[str, Any]:
    models = catalog_models()
    if not models:
        raise RuntimeError("catálogo Samsung vazio")
    weights = [max(1, int(m.get("weight") or 1)) for m in models]
    return rng.choices(models, weights=weights, k=1)[0]


def pick_os(model: dict[str, Any], rng: random.Random) -> tuple[str, int]:
    releases = [str(r) for r in (model.get("osReleases") or ["14"])]
    release = releases[rng.randrange(len(releases))]
    return release, api_level_for_release(release)


def _email_key(email: str) -> str:
    return str(email or "").strip().casefold()


def android_id_for_email(email: str) -> str:
    """SSAID sintético só do e-mail (sem âncora de aparelho)."""
    digest = hashlib.sha256(f"moneymin.android.id:{email}".encode("utf-8"))
    return digest.hexdigest()[:16]


def device_id_for_email(email: str) -> str:
    return f"android.ssaid:{android_id_for_email(email)}"


def shell_android_id_value(anchor: dict[str, Any] | None) -> str | None:
    """Extrai o hex do `shell_android_id` da âncora (objeto ou string)."""
    if not isinstance(anchor, dict):
        return None
    raw = anchor.get("shell_android_id")
    if isinstance(raw, dict):
        value = str(raw.get("value") or "").strip().lower()
    else:
        value = str(raw or "").strip().lower()
    if len(value) == 16 and all(c in "0123456789abcdef" for c in value):
        return value
    return None


def android_id_from_shell_seed(shell_id: str, email: str) -> str:
    """Deriva SSAID por conta a partir do ID shell do aparelho + e-mail.

    O valor shell (adb) tem mais lastro no hardware que um hash só do e-mail,
    mas NÃO é o SSAID do Minute. Espelha a ideia AOSP (HMAC → 64 bits):
    HMAC-SHA256(shell_id, email) nos primeiros 16 hex.
    """
    key = str(shell_id).strip().lower().encode("utf-8")
    msg = _email_key(email).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:16]


def device_id_from_shell_seed(shell_id: str, email: str) -> str:
    return f"android.ssaid:{android_id_from_shell_seed(shell_id, email)}"


def calibration_for_model(build_model: str, rng: random.Random) -> dict[str, Any]:
    """Referência UW do modelo + jitter determinístico por conta (via rng)."""
    calib_db = load_calibration()
    models = calib_db.get("models") or {}
    ref = dict(models.get(build_model) or {})
    center = calib_db.get("reference") or {
        "cx": 2016.0, "cy": 1512.0, "sensorWidth": 4032, "sensorHeight": 3024,
    }
    sensor = load_catalog().get("sensorDefaults") or {}
    ref_w = int(center.get("sensorWidth") or sensor.get("referenceWidth") or 4032)
    ref_h = int(center.get("sensorHeight") or sensor.get("referenceHeight") or 3024)
    center_cx = float(center.get("cx") or ref_w / 2.0)
    center_cy = float(center.get("cy") or ref_h / 2.0)
    if not ref:
        ref = {
            "fx": 1545.0, "fy": 1543.0, "k1": -0.24, "k2": 0.12, "k3": -0.035,
            "p1": 0.001, "p2": -0.002, "readoutS": 0.0104, "logicalCameraId": "4",
        }
    nx = float(ref.get("fx") or 1545.0)
    ny = float(ref.get("fy") or 1543.0)
    model_cx = float(ref.get("cx") or center_cx)
    model_cy = float(ref.get("cy") or center_cy)
    catalog_model = next(
        (m for m in catalog_models() if m.get("buildModel") == build_model), {})
    logical = str(
        ref.get("logicalCameraId")
        or catalog_model.get("logicalCameraIdDefault")
        or "4")
    return {
        "distortion_model": "brown_conrady",
        "fx": round(nx * (1.0 + rng.uniform(-0.004, 0.004)), 6),
        "fy": round(ny * (1.0 + rng.uniform(-0.004, 0.004)), 6),
        "cx": round(model_cx + rng.uniform(-2.0, 2.0), 3),
        "cy": round(model_cy + rng.uniform(-2.0, 2.0), 3),
        "referenceWidth": ref_w,
        "referenceHeight": ref_h,
        "k1": float(ref.get("k1") or 0.0) * (1.0 + rng.uniform(-0.02, 0.02)),
        "k2": float(ref.get("k2") or 0.0) * (1.0 + rng.uniform(-0.02, 0.02)),
        "k3": float(ref.get("k3") or 0.0) * (1.0 + rng.uniform(-0.02, 0.02)),
        "p1": float(ref.get("p1") or 0.0) + rng.uniform(-0.0004, 0.0004),
        "p2": float(ref.get("p2") or 0.0) + rng.uniform(-0.0004, 0.0004),
        "readoutS": round(
            float(ref.get("readoutS") or 0.0105) * (1.0 + rng.uniform(-0.05, 0.05)), 6),
        "logicalCameraId": logical,
    }


def video_metrics(rng: random.Random) -> tuple[int, float]:
    video = load_catalog().get("videoDefaults") or {}
    gop_min = int(video.get("gopMin") or 28)
    gop_max = int(video.get("gopMax") or 32)
    br_min = float(video.get("bitrateMbpsMin") or 7.4)
    br_max = float(video.get("bitrateMbpsMax") or 8.8)
    return rng.randint(gop_min, gop_max), round(rng.uniform(br_min, br_max), 1)


def load_anchor(path: Path | None = None) -> dict[str, Any] | None:
    """Lê a âncora USB (adb). None se ausente/inválida."""
    target = Path(path) if path is not None else Path(config.DEVICE_ANCHOR_PATH)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 2:
        return None
    template = data.get("profileTemplate")
    if not isinstance(template, dict):
        template = data
    model = str(template.get("device_model") or data.get("device_model") or "")
    if not model:
        return None
    raw_id = data.get("minute_app_android_id")
    if raw_id is not None and (
        not isinstance(raw_id, str)
        or not re.fullmatch(r"(?:android\.ssaid:)?[0-9a-fA-F]{16}", raw_id)
    ):
        return None
    return data


def generate_identity(email: str, *, from_anchor: bool | None = None) -> dict[str, Any]:
    """Campos de identidade derivados do e-mail (reprodutível).

    Com propagação por âncora: trava modelo/OS/SDK na família do S22 USB e
    deriva SSAID por conta via HMAC(shell_android_id, email) — lastro no
    aparelho, sem promover o valor shell cru a SSAID do Minute.
    """
    seed = f"moneymin.android:{email}"
    rng = random.Random(seed)
    use_anchor = config.DEVICE_PROPAGATE_FROM_ANCHOR if from_anchor is None else from_anchor
    anchor = load_anchor() if use_anchor else None
    if anchor is not None:
        return _identity_from_anchor(email, anchor, rng)

    model = pick_model(rng)
    os_version, sdk_int = pick_os(model, rng)
    build_model = str(model["buildModel"])
    gop, bitrate = video_metrics(rng)
    calib = calibration_for_model(build_model, rng)
    return {
        "commercial": model.get("commercial"),
        "device_model": build_model,
        "sidecar_model": build_model,
        "os_version": os_version,
        "sdk_int": sdk_int,
        "sidecar_system_version": os_version,
        "logical_camera_id": str(calib.get("logicalCameraId") or "4"),
        "device_id": device_id_for_email(email),
        "frames_gop": gop,
        "video_bitrate_mbps": bitrate,
        "calib": calib,
        "codename": model.get("codename"),
        "chipset": model.get("chipset"),
        "from_anchor": False,
        "anchor_owner": False,
        "device_id_source": "email_only",
    }


def _identity_from_anchor(email: str, anchor: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    template = anchor.get("profileTemplate") if isinstance(anchor.get("profileTemplate"), dict) else {}
    build_model = str(template.get("device_model") or anchor.get("device_model") or "SM-S901E")
    os_version = str(template.get("os_version") or anchor.get("os_version") or "16")
    sdk_int = int(template.get("sdk_int") or anchor.get("sdk_int") or api_level_for_release(os_version))
    logical = str(template.get("logical_camera_id") or "3")
    base_gop = int(template.get("frames_gop") or 30)
    base_br = float(template.get("video_bitrate_mbps") or 8.0)
    gop = max(28, min(32, base_gop + rng.randint(-1, 1)))
    bitrate = round(max(7.4, min(8.8, base_br + rng.uniform(-0.3, 0.3))), 1)
    calib = calibration_for_model(build_model, rng)
    calib["logicalCameraId"] = logical

    # Prioridade do SSAID:
    # 1) minute_app_android_id declarado na âncora (dono), sem certificar origem
    # 2) derivado do shell_android_id + e-mail (lastro no aparelho, ainda sintético)
    # 3) hash só do e-mail (fallback)
    # Formato válido não prova origem. Nunca atribuir verificação automaticamente.
    reported = anchor.get("minute_app_android_id")
    reported = reported if isinstance(reported, str) and re.fullmatch(
        r"(?:android\.ssaid:)?[0-9a-fA-F]{16}", reported
    ) else ""
    shell_id = shell_android_id_value(anchor)
    owner = _email_key(config.DEVICE_ANCHOR_OWNER_EMAIL)
    if owner and owner == _email_key(email) and reported:
        device_id = (reported if reported.startswith("android.ssaid:")
                     else f"android.ssaid:{reported}")
        owned = True
        id_source = "anchor_reported"
    elif shell_id:
        device_id = device_id_from_shell_seed(shell_id, email)
        owned = False
        id_source = "shell_seeded"
    else:
        device_id = device_id_for_email(email)
        owned = False
        id_source = "email_only"

    catalog_hit = next((m for m in catalog_models() if m.get("buildModel") == build_model), {})
    return {
        "commercial": anchor.get("commercial") or catalog_hit.get("commercial") or "Galaxy S22",
        "device_model": build_model,
        "sidecar_model": build_model,
        "os_version": os_version,
        "sdk_int": sdk_int,
        "sidecar_system_version": os_version,
        "logical_camera_id": logical,
        "device_id": device_id,
        "frames_gop": gop,
        "video_bitrate_mbps": bitrate,
        "calib": calib,
        "codename": catalog_hit.get("codename") or anchor.get("product_device"),
        "chipset": catalog_hit.get("chipset"),
        "from_anchor": True,
        "anchor_owner": owned,
        "device_id_source": id_source,
    }
