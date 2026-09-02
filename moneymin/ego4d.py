"""
ego4d.py — Acesso ao dataset Ego4D (S3), seleção de clipes e conversão de IMU
para o sidecar nativo do app Minute.

Separação de responsabilidades:
  - Catálogo: `ego4d.json` (metadata dos vídeos) + `clips/manifest.csv` (clipes),
    baixados/cacheados em `data/ego4d/`.
  - Seleção: `list_clips()` / `find_clip()` com filtros (device, has_imu, duração,
    cenário).
  - IMU: baixa o `.csv` real do vídeo e reamostra para o formato Android do sidecar
    (`t,ax,ay,az,wx,wy,wz` em 500 Hz).

Requisitos:
  - Credenciais AWS do Ego4D em `~/.aws/credentials` (profile `default`) com
    permissão de `GetObject` (NÃO `ListObjects`).
  - boto3 (opcional; só é exigido ao usar as funções de S3).

A IMU real do Ego4D é o que destrava o catbear: fisicamente coerente com o vídeo
(gravado pelo mesmo dispositivo), ao contrário da sintética (`build_imu_csv`).
"""
from __future__ import annotations

import configparser
import csv
import hashlib
import hmac
import io
import json
import math
import os
import random
import re
import shutil
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

from . import config, tls

EGO4D_DIR = config.MEDIA_DATA_DIR / "ego4d"
MANIFEST_BUCKET = "ego4d-consortium-sharing"
METADATA_KEY = "public/v2/ego4d.json"
CLIPS_MANIFEST_KEY = "public/v2/clips/manifest.csv"
def timed_narrations_path() -> Path:
    return Path(config.MEDIA_DATA_DIR) / "ego4d" / "timed_narrations.jsonl"

# Header do csv que o sidecar espera (ver sidecar.build_imu_csv).
IMU_HDR = ["t", "ax", "ay", "az", "wx", "wy", "wz"]
FRAMES_HDR = ["i", "ptsNs", "dtNs", "tNs", "key"]


@lru_cache(maxsize=2)
def _action_index_cached(path: str, mtime_ns: int, size: int) -> dict[str, str]:
    """Metadados temporizados de ações Ego4D, indexados por clipe."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(uid): str(text) for uid, text in data.items() if text}


def _action_index() -> dict[str, str]:
    path = EGO4D_DIR / "clip_narrations.json"
    try:
        stat = path.stat()
    except OSError:
        return {}
    return _action_index_cached(str(path), stat.st_mtime_ns, stat.st_size)


def _action_units(text: str) -> list[str]:
    """Ações do camera-wearer (`#C`), sem atribuir ações de terceiros (`#O`)."""
    actor = re.compile(r"#\s*([co])\b", re.IGNORECASE)
    marks = list(actor.finditer(text))
    if not marks:
        cleaned = re.sub(r"\s+", " ", text.strip().casefold())
        return [cleaned] if cleaned else []
    out: list[str] = []
    for idx, mark in enumerate(marks):
        if mark.group(1).casefold() != "c":
            continue
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
        part = re.sub(r"\s+", " ", text[mark.end():end].strip().casefold())
        if part:
            out.append(part)
    return out


# --- Acesso S3 ----------------------------------------------------------------

def _s3():
    """Cliente S3 do perfil configurado para o Ego4D."""
    import boto3
    profile = config.EGO4D_AWS_PROFILE or None
    session = boto3.session.Session(
        profile_name=profile,
        region_name=config.EGO4D_AWS_REGION or None,
    )
    return session.resource("s3")


def _aws_creds() -> tuple[str, str, str | None]:
    """Credenciais do perfil Ego4D para o fallback SigV4 stdlib."""
    profile = config.EGO4D_AWS_PROFILE
    if not profile:
        access = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
        secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
        token = os.environ.get("AWS_SESSION_TOKEN", "").strip() or None
        if access and secret:
            return access, secret, token
        raise RuntimeError(
            "credenciais AWS ausentes no ambiente; configure "
            "EGO4D_AWS_PROFILE ou AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY")
    cred_path = Path.home() / ".aws" / "credentials"
    parser = configparser.RawConfigParser()
    try:
        with cred_path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error) as exc:
        raise RuntimeError(f"~/.aws/credentials inacessível: {exc}") from exc
    if not parser.has_section(profile):
        raise RuntimeError(
            f"perfil AWS [{profile}] ausente em ~/.aws/credentials")
    ak = parser.get(profile, "aws_access_key_id", fallback="").strip()
    sk = parser.get(profile, "aws_secret_access_key", fallback="").strip()
    token = parser.get(profile, "aws_session_token", fallback="").strip() or None
    if not ak or not sk:
        raise RuntimeError(
            f"credenciais AWS incompletas em ~/.aws/credentials [{profile}]")
    return ak, sk, token


def _aws_region() -> str:
    """Região do perfil Ego4D (o S3 corrige via x-amz-bucket-region)."""
    if config.EGO4D_AWS_REGION:
        return config.EGO4D_AWS_REGION
    profile = config.EGO4D_AWS_PROFILE
    if not profile:
        return (os.environ.get("AWS_REGION")
                or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
    cfg_path = Path.home() / ".aws" / "config"
    parser = configparser.RawConfigParser()
    try:
        with cfg_path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error):
        return "us-east-1"
    section = "default" if profile == "default" else f"profile {profile}"
    return parser.get(section, "region", fallback="us-east-1").strip()


def _s3_get_stdlib(bucket: str, key: str, dest: Path,
                   _region: str | None = None) -> Path:
    """GET do S3 com assinatura SigV4 usando só stdlib (sem boto3).

    Fallback acionado quando o boto3 não existe no interpretador em execução —
    mantém a promessa do docstring ("boto3 opcional"). GET sem query string e
    payload vazio, host virtual {bucket}.s3.{region}.amazonaws.com.

    Na primeira chamada o S3 pode responder 301/307 apontando a região REAL do
    bucket (x-amz-bucket-region) — re-assina com ela e tenta de novo (a região
    fica em cache).
    """
    access_key, secret_key, session_token = _aws_creds()
    region = _region or _S3_REGION_CACHE.get(bucket) or _aws_region()
    host = f"{bucket}.s3.{region}.amazonaws.com"
    uri = "/" + quote(key, safe="/")
    service, algorithm = "s3", "AWS4-HMAC-SHA256"
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()

    header_rows = [
        ("host", host),
        ("x-amz-content-sha256", payload_hash),
        ("x-amz-date", amz_date),
    ]
    if session_token:
        header_rows.append(("x-amz-security-token", session_token))
    canonical_headers = "".join(f"{name}:{value}\n" for name, value in header_rows)
    signed_headers = ";".join(name for name, _value in header_rows)
    canonical_request = (
        f"GET\n{uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"{algorithm}\n{amz_date}\n{scope}\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    auth = (f"{algorithm} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")

    headers = {name: value for name, value in header_rows if name != "host"}
    headers["Authorization"] = auth
    req = urllib.request.Request(f"https://{host}{uri}", headers=headers)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tls.urlopen(req, timeout=600) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
    except urllib.error.HTTPError as exc:
        new_region = exc.headers.get("x-amz-bucket-region") if exc.headers else None
        if new_region and new_region != region:
            _S3_REGION_CACHE[bucket] = new_region
            return _s3_get_stdlib(bucket, key, dest, _region=new_region)
        raise
    return dest


# Região REAL por bucket descoberta no primeiro acesso (301/307 do S3).
_S3_REGION_CACHE: dict[str, str] = {}


def _download_to(bucket: str, key: str, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Nunca exponha um download parcial como cache válido. Uma interrupção do
    # processo deixa somente o .part descartável; o nome definitivo aparece
    # atomicamente depois que o S3 conclui.
    part = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
    try:
        try:
            _s3().Bucket(bucket).download_file(key, str(part))
        except ImportError:
            # boto3 ausente no interpretador atual -> cliente SigV4 stdlib.
            _s3_get_stdlib(bucket, key, part)
        except Exception as exc:
            response = getattr(exc, "response", {}) or {}
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            code = str(error.get("Code") or "")
            if code in {"ExpiredToken", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
                raise RuntimeError(
                    "credenciais Ego4D inválidas ou expiradas; renove a licença "
                    "e atualize o perfil AWS configurado") from exc
            if code in {"AccessDenied", "403"}:
                raise RuntimeError(
                    f"acesso negado ao Ego4D para s3://{bucket}/{key}; "
                    "confirme a licença Ego4D e o perfil AWS") from exc
            raise
        part.replace(dest)
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
    return dest


def _valid_metadata(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        return (
            isinstance(data, dict)
            and str(data.get("version") or "").startswith("2")
            and isinstance(data.get("videos"), list)
            and len(data["videos"]) > 1000
        )
    except (OSError, ValueError, TypeError):
        return False


def _valid_clips_manifest(path: Path) -> bool:
    required = {
        "exported_clip_uid", "parent_video_uid", "parent_start_sec",
        "parent_end_sec", "s3_path",
    }
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            return required.issubset(reader.fieldnames or ()) and next(reader, None) is not None
    except OSError:
        return False


def _refresh_catalog_file(
    bucket: str, key: str, dest: Path, validator,
) -> None:
    staging = dest.with_name(f".{dest.name}.refresh")
    try:
        _download_to(bucket, key, staging)
        if not validator(staging):
            raise RuntimeError(
                f"arquivo Ego4D baixado é inválido: s3://{bucket}/{key}")
        staging.replace(dest)
    finally:
        staging.unlink(missing_ok=True)


def sync_meta(force: bool = False) -> tuple[Path, Path]:
    """Garante `ego4d.json` e `clips.csv` locais (cache). Devolve os caminhos."""
    EGO4D_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = EGO4D_DIR / "ego4d.json"
    clips_path = EGO4D_DIR / "clips.csv"
    meta_missing = not meta_path.exists() or meta_path.stat().st_size == 0
    clips_missing = not clips_path.exists() or clips_path.stat().st_size == 0
    if force or meta_missing:
        _refresh_catalog_file(
            MANIFEST_BUCKET, METADATA_KEY, meta_path, _valid_metadata)
    if force or clips_missing:
        _refresh_catalog_file(
            MANIFEST_BUCKET, CLIPS_MANIFEST_KEY, clips_path,
            _valid_clips_manifest)
    return meta_path, clips_path


def diagnostics(*, check_access: bool = False) -> dict[str, Any]:
    """Diagnóstico compacto do catálogo, índices e acesso AWS do Ego4D."""
    meta_path, clips_path = sync_meta()
    cat = _cat()
    imu_videos = [video for video in cat.videos.values()
                  if video.get("has_imu") is True]
    eligible = sum(
        _passes_filter(clip, cat.videos, None, True, None, 60, 1800)
        for clip in cat.clips
    )
    timed_path = timed_narrations_path()
    narration_path = EGO4D_DIR / "clip_narrations.json"
    result: dict[str, Any] = {
        "dataset_version": cat.meta.get("version"),
        "dataset_date": cat.meta.get("date"),
        "videos": len(cat.videos),
        "videos_with_imu": len(imu_videos),
        "clips": len(cat.clips),
        "eligible_clips": eligible,
        "aws_profile": config.EGO4D_AWS_PROFILE or "<ambiente>",
        "metadata_bytes": meta_path.stat().st_size,
        "clips_manifest_bytes": clips_path.stat().st_size,
        "clip_narrations_bytes": (
            narration_path.stat().st_size if narration_path.exists() else 0),
        "timed_narrations_bytes": (
            timed_path.stat().st_size if timed_path.exists() else 0),
    }
    if check_access:
        def _range_status(s3_path: str) -> str:
            try:
                raw = str(s3_path or "")
                if not raw.startswith("s3://"):
                    return "caminho ausente"
                bucket, key = raw[5:].split("/", 1)
                obj = _s3().meta.client.get_object(
                    Bucket=bucket, Key=key, Range="bytes=0-0")
                obj["Body"].close()
                return "ok"
            except ImportError:
                return "não verificado (boto3 ausente)"
            except Exception as exc:  # noqa: BLE001 — diagnóstico deve responder
                response = getattr(exc, "response", {}) or {}
                error = response.get("Error", {}) if isinstance(response, dict) else {}
                return str(error.get("Code") or type(exc).__name__)

        try:
            obj = _s3().meta.client.get_object(
                Bucket=MANIFEST_BUCKET, Key=METADATA_KEY, Range="bytes=0-0")
            obj["Body"].close()
            result["aws_access"] = "ok"
        except ImportError:
            result["aws_access"] = "não verificado (boto3 ausente)"
        except Exception as exc:
            response = getattr(exc, "response", {}) or {}
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            result["aws_access"] = str(error.get("Code") or type(exc).__name__)
        try:
            # O CLI oficial atual consulta o registro v2.1 antes do download.
            # O registro legado v2 pode responder 403 mesmo com acesso válido.
            obj = _s3().meta.client.get_object(
                Bucket=MANIFEST_BUCKET, Key="public/v2_1/datasets.csv",
                Range="bytes=0-0")
            obj["Body"].close()
            result["official_cli_access"] = "ok"
        except ImportError:
            result["official_cli_access"] = "não verificado (boto3 ausente)"
        except Exception as exc:
            response = getattr(exc, "response", {}) or {}
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            result["official_cli_access"] = str(
                error.get("Code") or type(exc).__name__)
        # Manifesto acessível não garante licença para os buckets de mídia.
        # Verifique um par real vídeo+IMU usado pelo motor da campanha.
        sample = next((
            (clip, cat.videos.get(str(clip.get("parent_video_uid") or ""), {}))
            for clip in cat.clips
            if _passes_filter(clip, cat.videos, None, True, None, 60, 1800)
        ), None)
        if sample is None:
            result["video_access"] = "sem amostra elegível"
            result["imu_access"] = "sem amostra elegível"
        else:
            clip, video = sample
            result["video_access"] = _range_status(str(clip.get("s3_path") or ""))
            result["imu_access"] = _range_status(str(
                (video.get("imu_metadata") or {}).get("s3_path") or ""))
    return result


class _Catalog(NamedTuple):
    meta: dict[str, Any]
    videos: dict[str, dict[str, Any]]
    clips: tuple[dict[str, Any], ...]
    by_uid: dict[str, dict[str, Any]]
    by_parent: dict[str, tuple[tuple[str, float, float], ...]]


@lru_cache(maxsize=2)
def _catalog(meta_path: str, clips_path: str,
             meta_mtime: float, clips_mtime: float) -> _Catalog:
    """Índice único do Ego4D (JSON grande). Chave inclui mtime p/ invalidar."""
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    videos = {v["video_uid"]: v for v in meta["videos"]}
    with open(clips_path, encoding="utf-8") as f:
        clips = tuple(csv.DictReader(f))
    by_uid: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    for row in clips:
        uid = row.get("exported_clip_uid") or ""
        parent = row.get("parent_video_uid") or ""
        if not uid or not parent or parent not in videos or not row.get("s3_path"):
            continue
        try:
            start, end = clip_window_s(row)
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end) and end > start):
            continue
        by_uid[uid] = row
        grouped[parent].append((uid, start, end))
    return _Catalog(
        meta, videos, clips, by_uid,
        {k: tuple(sorted(v, key=lambda row: (row[1], row[2], row[0])))
         for k, v in grouped.items()},
    )


def _cat() -> _Catalog:
    meta_path, clips_path = sync_meta()
    return _catalog(
        str(meta_path), str(clips_path),
        meta_path.stat().st_mtime, clips_path.stat().st_mtime,
    )


def _load() -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Carrega meta + indexa vídeos + lista clipes (cache em memória)."""
    cat = _cat()
    return cat.meta, cat.videos, list(cat.clips)


# --- Seleção de clipes --------------------------------------------------------

def clip_window_s(c: dict[str, Any]) -> tuple[float, float]:
    """Janela temporal (s) do clipe dentro do vídeo canônico."""
    return float(c["parent_start_sec"]), float(c["parent_end_sec"])


def clip_duration_s(c: dict[str, Any]) -> float:
    s, e = clip_window_s(c)
    return e - s


def scenario_values(video: dict[str, Any]) -> list[str]:
    """Normaliza quebras de linha/whitespace presentes no metadata oficial."""
    return [
        re.sub(r"\s+", " ", str(value)).strip()
        for value in (video.get("scenarios") or [])
        if str(value).strip()
    ]


def _window_uid(parent_uid: str, start_s: float, end_s: float) -> str:
    """UID reversível de janela, preservando milissegundos."""
    return f"{parent_uid}_{float(start_s):.3f}_{float(end_s):.3f}"


def imu_coverage_intervals(
    video: dict[str, Any], *, max_gap_s: float = 0.050,
) -> list[tuple[float, float]]:
    """Intervalos contínuos cobertos pelo IMU segundo o metadata oficial."""
    components = (video.get("imu_metadata") or {}).get("component_metadata") or []
    intervals: list[tuple[float, float]] = []
    for component in components:
        try:
            start = float(component["canonical_video_start_ms"]) / 1000.0
            end = float(component["canonical_video_end_ms"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and end > start:
            intervals.append((start, end))
    intervals.sort()
    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + max_gap_s:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def imu_window_is_covered(
    video: dict[str, Any], window_s: tuple[float, float],
    *, tolerance_s: float = 0.050,
) -> bool:
    """Recusa janelas que atravessam componentes sem IMU."""
    if video.get("has_imu") is not True:
        return False
    metadata = video.get("imu_metadata") or {}
    if not metadata:
        # Fixtures e exports antigos só informam has_imu. O parser do CSV faz
        # a validação final durante o preparo.
        return True
    if not metadata.get("s3_path"):
        return False
    intervals = imu_coverage_intervals(video)
    if not metadata.get("component_metadata"):
        # Metadata antigo não expunha componentes; a validação do CSV ainda
        # protege o preparo.
        return True
    start, end = map(float, window_s)
    return any(
        start >= covered_start - tolerance_s
        and end <= covered_end + tolerance_s
        for covered_start, covered_end in intervals
    )


def humanize_window(
    start_s: float, end_s: float, seed: str,
    *,
    min_s: float = 60.0,
    max_s: float = 1796.4,
) -> tuple[float, float]:
    """Corta como humano: nunca 5:00.000 / 10:00.000 / 30:00.000.

    Atraso ao apertar gravar + soltar um pouco antes do fim, com fração de
    microssegundo. A IMU tem de usar a MESMA janela. Determinístico por seed
    (clip_uid) para o cache do encode bater.
    """
    start_s = float(start_s)
    end_s = float(end_s)
    span = end_s - start_s
    if span < min_s:
        return start_s, end_s
    if span <= min_s + 0.05:
        # Não existe folga suficiente para aparar as duas pontas sem violar o
        # mínimo. Preservar 60.000s é melhor que produzir 59.999s e falhar.
        return start_s, end_s

    rng = random.Random(f"moneymin.cut:{seed}")
    room = span - min_s
    trim_max = min(6.2, room * 0.85)
    trim_min = min(trim_max, max(0.002, min(0.50, room * 0.15)))
    total_trim = rng.uniform(trim_min, trim_max)
    head = total_trim * rng.uniform(0.20, 0.48)
    tail = total_trim - head
    ns = start_s + head
    ne = end_s - tail
    dur = ne - ns
    # Evita duração "redonda" (N segundos ou N.5).
    frac = dur - int(dur)
    if frac < 0.012 or abs(frac - 0.5) < 0.008 or abs(frac - 0.0) < 0.012:
        ne -= rng.uniform(0.017, 0.083)
        dur = ne - ns
    if dur > max_s:
        high = max(min_s, max_s - 0.083)
        low = max(min_s, min(high, max_s - 18.7))
        ne = ns + rng.uniform(low, high)
        dur = ne - ns
    if dur < min_s:
        ne = min(end_s, ns + min_s)
    return ns, ne


def find_clip(clip_uid: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Localiza um clipe pelo uid. Devolve (linha_clip, metadados_do_video_pai)."""
    cat = _cat()
    row = cat.by_uid.get(clip_uid)
    if row is not None:
        video = cat.videos.get(row["parent_video_uid"])
        return (row, video) if video is not None else (None, None)
    window = _window_row_from_uid(clip_uid, cat.videos)
    if window is not None:
        video = cat.videos.get(window["parent_video_uid"])
        return (window, video) if video is not None else (None, None)
    return None, None


def _window_row_from_uid(
    clip_uid: str, videos: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Reconstrói uma janela longa `{video_uid}_{start}_{end}`."""
    parts = str(clip_uid).rsplit("_", 2)
    if len(parts) != 3:
        return None
    video_uid, start_s, end_s = parts
    try:
        start = float(start_s)
        end = float(end_s)
    except ValueError:
        return None
    if end <= start:
        return None
    video = videos.get(video_uid)
    if not video or not video.get("s3_path"):
        return None
    return {
        "exported_clip_uid": clip_uid,
        "parent_video_uid": video_uid,
        "parent_start_sec": str(start),
        "parent_end_sec": str(end),
        "s3_path": video["s3_path"],
        "needs_cut": True,
    }


def _clip_record(c: dict[str, Any], pv: dict[str, Any]) -> dict[str, Any]:
    """Monta o dict de resultado para um clipe a partir da linha CSV e vídeo pai."""
    s, e = clip_window_s(c)
    dur = e - s
    dev = str(pv.get("device") or "")
    uid = c["exported_clip_uid"]
    action_text = _action_index().get(uid, "")
    scenarios = scenario_values(pv)
    return {
        "dur_s": round(dur, 1),
        "clip_uid": uid,
        "device": dev,
        "scenario": " | ".join(scenarios),
        "scenarios": scenarios,
        "window_s": (s, e),
        "s3_path": c["s3_path"],
        "action_text": action_text,
        "action_units": _action_units(action_text),
        "parent_video_uid": str(c.get("parent_video_uid") or ""),
        "needs_cut": bool(c.get("needs_cut")),
    }


def _passes_filter(
    c: dict[str, Any], videos: dict[str, dict[str, Any]],
    scenario: str | None, require_imu: bool, gopro_minor: int | None,
    min_dur_s: float, max_dur_s: float,
) -> bool:
    """Verifica se um clipe passa nos filtros sem construir o dict de resultado."""
    pv = videos.get(c["parent_video_uid"], {})
    try:
        window = clip_window_s(c)
    except (KeyError, TypeError, ValueError):
        return False
    if require_imu and not imu_window_is_covered(pv, window):
        return False
    dev = str(pv.get("device") or "")
    if gopro_minor is not None and (
        "gopro" not in dev.casefold()
        or re.search(rf"\b{int(gopro_minor)}\b", dev) is None
    ):
        return False
    scenarios = [str(x).strip().casefold() for x in (pv.get("scenarios") or [])]
    wanted = str(scenario or "").strip().casefold()
    if wanted and not any(wanted in actual for actual in scenarios):
        return False
    dur = window[1] - window[0]
    if not (min_dur_s <= dur <= max_dur_s):
        return False
    return True


def count_clips(
    *,
    scenario: str | None = None,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
    require_imu: bool = True,
    gopro_minor: int | None = None,
) -> int:
    """Conta clipes elegíveis sem limitar (percorre todos, sem montar dicts)."""
    cat = _cat()
    n = 0
    for c in cat.clips:
        if _passes_filter(c, cat.videos, scenario, require_imu, gopro_minor,
                          min_dur_s, max_dur_s):
            n += 1
    return n


def list_clips(
    *,
    scenario: str | None = None,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
    require_imu: bool = True,
    gopro_minor: int | None = None,
    max_results: int | None = 50,
) -> list[dict[str, Any]]:
    """Lista clipes elegíveis com filtros.

    - scenario: substring do cenário (ex.: "assembling furniture", "cooking").
    - require_imu: só clipes cujo vídeo pai tem IMU real.
    - gopro_minor: prefixo do dispositivo (8 => "GoPro Hero Black 8").
    - max_results: quantos retornar (None = sem limite). Default 50.
    Cada item traz dur_s, clip_uid, device, scenario, janela.
    """
    cat = _cat()
    videos = cat.videos
    out: list[dict[str, Any]] = []
    for c in cat.clips:
        pv = videos.get(c["parent_video_uid"], {})
        if not _passes_filter(c, videos, scenario, require_imu, gopro_minor,
                              min_dur_s, max_dur_s):
            continue
        out.append(_clip_record(c, pv))
    out.sort(key=lambda r: r["dur_s"])
    return out if max_results is None else out[:max_results]


# Janelas longas nos vídeos-pai (o clips.csv recorta ~5 min; o pai chega a 30+).
WINDOW_TARGET_S = 1800.0  # teto do Minute (maxDurationMs)
WINDOW_MIN_S = 60.0  # minDurationMs — aproveita o resto do vídeo-pai


def split_parent_windows(
    duration_s: float,
    *,
    target_s: float = WINDOW_TARGET_S,
    min_s: float = WINDOW_MIN_S,
) -> list[tuple[float, float]]:
    """Parte um vídeo-pai em janelas [start, end) de até `target_s`."""
    duration_s = float(duration_s or 0)
    if duration_s < min_s:
        return []
    out: list[tuple[float, float]] = []
    t = 0.0
    while duration_s - t >= min_s:
        remaining = duration_s - t
        take = min(target_s, remaining)
        leftover = remaining - take
        if 0 < leftover < min_s and remaining <= target_s:
            take = remaining
        out.append((t, t + take))
        t += take
        if t >= duration_s - 1e-6:
            break
    return out


def has_timed_narrations() -> bool:
    path = timed_narrations_path()
    return path.exists() and path.stat().st_size > 0


@lru_cache(maxsize=2)
def _load_timed_narrations_cached(
    path_str: str, mtime_ns: int, size: int,
) -> dict[str, tuple[tuple[float, str], ...]]:
    """Narrações com timestamp por vídeo-pai (jsonl). Vazio se o índice não existe."""
    path = Path(path_str)
    out: dict[str, tuple[tuple[float, str], ...]] = {}
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    # Uma linha incompleta não deve apagar milhares de vídeos
                    # válidos já lidos.
                    continue
                uid = str(rec.get("video_uid") or "")
                events = rec.get("events") or []
                if not uid or not events:
                    continue
                parsed: list[tuple[float, str]] = []
                for item in events:
                    if not isinstance(item, (list, tuple)) or len(item) < 2:
                        continue
                    try:
                        when = float(item[0])
                        text = str(item[1]).strip()
                    except (TypeError, ValueError):
                        continue
                    if (not math.isfinite(when) or when < 0 or not text
                            or re.search(r"#\s*summary\b", text, re.I)):
                        continue
                    parsed.append((when, text))
                if parsed:
                    parsed.sort(key=lambda row: row[0])
                    # Passes/annotadores podem repetir a mesma frase quase no
                    # mesmo instante; sem dedupe isso distorce o ratio da ação.
                    deduped: list[tuple[float, str]] = []
                    last_by_text: dict[str, float] = {}
                    for when, text in parsed:
                        key = re.sub(r"\s+", " ", text.strip().casefold())
                        previous = last_by_text.get(key)
                        if previous is not None and when - previous <= 0.250:
                            continue
                        last_by_text[key] = when
                        deduped.append((when, text))
                    if deduped:
                        out[uid] = tuple(deduped)
    except OSError:
        return {}
    return out


def load_timed_narrations() -> dict[str, tuple[tuple[float, str], ...]]:
    path = timed_narrations_path()
    try:
        stat = path.stat()
    except OSError:
        return {}
    return _load_timed_narrations_cached(
        str(path), stat.st_mtime_ns, stat.st_size)


def _best_media_source(
    parent_uid: str, start: float, end: float, fallback_s3: str,
) -> tuple[str, str, float]:
    """Prefere o clip exportado CRF 18 ao vídeo full-scale CRF 41.

    Os tempos continuam absolutos no vídeo canônico (inclusive para o IMU).
    ``media_time_offset_s`` só converte a busca para o relógio do MP4 clipado.
    """
    cat = _cat()
    by_parent = getattr(cat, "by_parent", {})
    containing = [
        (clip_end - clip_start, uid, clip_start)
        for uid, clip_start, clip_end in by_parent.get(parent_uid, ())
        if clip_start <= start + 1e-6 and clip_end >= end - 1e-6
    ]
    if containing:
        _duration, uid, offset = min(containing)
        row = getattr(cat, "by_uid", {}).get(uid) or {}
        s3_path = str(row.get("s3_path") or "")
        if s3_path:
            return s3_path, uid, offset
    return fallback_s3, parent_uid, 0.0


def _span_record(video: dict[str, Any], span: dict[str, Any]) -> dict[str, Any]:
    parent = str(video.get("video_uid") or "")
    start = float(span["start"])
    end = float(span["end"])
    uid = _window_uid(parent, start, end)
    s3_path, media_uid, media_offset = _best_media_source(
        parent, start, end, str(video.get("s3_path") or ""))
    scenarios = scenario_values(video)
    return {
        "dur_s": round(end - start, 1),
        "clip_uid": uid,
        "device": str(video.get("device") or ""),
        "scenario": " | ".join(scenarios),
        "scenarios": scenarios,
        "window_s": (start, end),
        "s3_path": s3_path,
        "media_uid": media_uid,
        "media_time_offset_s": media_offset,
        "action_text": str(span.get("action_text") or ""),
        "action_units": list(span.get("action_units") or []),
        "parent_video_uid": parent,
        "needs_cut": True,
        "match_score": int(span.get("match_score") or 0),
        "exported_clip_uid": uid,
        "parent_start_sec": str(start),
        "parent_end_sec": str(end),
    }


def list_task_spans(
    task_name: str,
    *,
    min_dur_s: float = WINDOW_MIN_S,
    max_dur_s: float = WINDOW_TARGET_S,
    require_imu: bool = True,
) -> list[dict[str, Any]]:
    """Trechos PUROS da tarefa no vídeo-pai, cortados pelas narrações temporizadas."""
    from . import task_matching
    rule = task_matching.rule_for(task_name)
    if rule is None:
        return []
    index = load_timed_narrations()
    if not index:
        return []
    videos = _cat().videos
    out: list[dict[str, Any]] = []
    for uid, events in index.items():
        video = videos.get(uid) or {}
        if require_imu and video.get("has_imu") is not True:
            continue
        scenarios = [str(x) for x in (video.get("scenarios") or [])]
        if task_matching.score_scenarios(rule, scenarios) is None:
            continue
        duration = float(video.get("duration_sec") or 0)
        search_text = task_matching.span_search_text(events)
        if not task_matching.span_evidence_possible(rule, search_text):
            continue
        activity_rules = [
            (name, candidate) for name, candidate in task_matching.TASK_RULES.items()
            if (task_matching.score_scenarios(candidate, scenarios) is not None
                and task_matching.span_evidence_possible(candidate, search_text))
        ]
        prepared = task_matching.prepare_span_events(events)
        event_labels = task_matching.label_span_events(prepared, activity_rules)
        rivals = task_matching.competing_span_names(task_name, activity_rules)
        for span in task_matching.extract_spans(
                rule, events,
                min_s=max(min_dur_s, rule.min_span_s or 0.0),
                max_s=max_dur_s,
                pad_s=0.0,
                video_duration_s=duration or None,
                prepared_events=prepared,
                activity_mode=True,
                task_name=task_name,
                event_task_names=event_labels,
                competing_task_names=rivals):
            rec = _span_record(video, span)
            rec["match_confidence"] = rule.confidence
            if (min_dur_s <= rec["dur_s"] <= max_dur_s + 1e-6
                    and (not require_imu or imu_window_is_covered(
                        video, tuple(rec["window_s"])))):
                out.append(rec)
    out.sort(key=lambda r: (-(r.get("match_score") or 0), -(r.get("dur_s") or 0),
                            str(r.get("clip_uid") or "")))
    return out


def rank_all_task_spans(
    *,
    min_dur_s: float = WINDOW_MIN_S,
    max_dur_s: float = WINDOW_TARGET_S,
    require_imu: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Uma passada nos vídeos-pai: trechos puros de cada tarefa Minute."""
    from . import task_matching
    index = load_timed_narrations()
    buckets: dict[str, list[dict[str, Any]]] = {
        name: [] for name in task_matching.TASK_RULES}
    if not index:
        return buckets
    videos = _cat().videos
    named_rules = list(task_matching.TASK_RULES.items())
    for uid, events in index.items():
        video = videos.get(uid) or {}
        if require_imu and video.get("has_imu") is not True:
            continue
        if not video.get("s3_path"):
            continue
        scenarios = [str(x) for x in (video.get("scenarios") or [])]
        duration = float(video.get("duration_sec") or 0)
        candidate_rules = [
            (name, rule) for name, rule in named_rules
            if task_matching.score_scenarios(rule, scenarios) is not None
        ]
        if not candidate_rules:
            continue
        search_text = task_matching.span_search_text(events)
        possible_rules = [
            (name, rule) for name, rule in candidate_rules
            if task_matching.span_evidence_possible(rule, search_text)
        ]
        if not possible_rules:
            continue
        activity_rules = possible_rules
        prepared = task_matching.prepare_span_events(events)
        event_labels = task_matching.label_span_events(prepared, activity_rules)
        for name, rule in possible_rules:
            rivals = task_matching.competing_span_names(name, activity_rules)
            for span in task_matching.extract_spans(
                    rule, events,
                    min_s=max(min_dur_s, rule.min_span_s or 0.0),
                    max_s=max_dur_s,
                    pad_s=0.0,
                    video_duration_s=duration or None,
                    prepared_events=prepared,
                    activity_mode=True,
                    task_name=name,
                    event_task_names=event_labels,
                    competing_task_names=rivals):
                rec = _span_record(video, span)
                rec["match_confidence"] = rule.confidence
                if (min_dur_s <= rec["dur_s"] <= max_dur_s + 1e-6
                        and (not require_imu or imu_window_is_covered(
                            video, tuple(rec["window_s"])))):
                    buckets[name].append(rec)
    for _name, items in buckets.items():
        items.sort(key=lambda r: (
            -(r.get("match_score") or 0), -(r.get("dur_s") or 0),
            str(r.get("clip_uid") or "")))
    for alias, canonical in task_matching.TASK_ALIASES.items():
        buckets[alias] = buckets.get(canonical, [])
    return buckets


def _narration_for_window(parent_uid: str, start: float, end: float) -> str:
    """Junta as anotações dos clipes oficiais que cruzam a janela."""
    parts: list[str] = []
    for uid, cs, ce in _cat().by_parent.get(parent_uid, ()):
        if ce <= start or cs >= end:
            continue
        text = _action_index().get(uid, "")
        if text:
            parts.append(text)
    return " ".join(parts)


def list_windows(
    *,
    min_dur_s: float = WINDOW_MIN_S,
    max_dur_s: float = WINDOW_TARGET_S,
    require_imu: bool = True,
    gopro_minor: int | None = None,
    scenario: str | None = None,
) -> list[dict[str, Any]]:
    """Janelas longas dos vídeos-pai com IMU — o que o clips.csv descarta.

    Cada item tem o mesmo formato de `list_clips`, com `needs_cut=True` e
    `s3_path` do vídeo pai (o download corta a janela localmente).
    """
    videos = _cat().videos
    want_sc = (scenario or "").strip().lower()
    out: list[dict[str, Any]] = []
    for video in videos.values():
        if require_imu and video.get("has_imu") is not True:
            continue
        dev = str(video.get("device") or "")
        if gopro_minor is not None and (
                str(gopro_minor) not in dev or "GoPro" not in dev):
            continue
        scenarios = [str(x) for x in (video.get("scenarios") or [])]
        sc_text = " ".join(s.strip().lower() for s in scenarios)
        if want_sc and want_sc not in sc_text:
            continue
        duration = float(video.get("duration_sec") or 0)
        parent_uid = str(video.get("video_uid") or "")
        s3_path = str(video.get("s3_path") or "")
        if not parent_uid or not s3_path:
            continue
        for start, end in split_parent_windows(
                duration, target_s=min(WINDOW_TARGET_S, max_dur_s),
                min_s=max(WINDOW_MIN_S, min_dur_s)):
            dur = end - start
            if not (min_dur_s <= dur <= max_dur_s + 1e-6):
                continue
            if require_imu and not imu_window_is_covered(video, (start, end)):
                continue
            uid = _window_uid(parent_uid, start, end)
            action_text = _narration_for_window(parent_uid, start, end)
            row = {
                "exported_clip_uid": uid,
                "parent_video_uid": parent_uid,
                "parent_start_sec": str(start),
                "parent_end_sec": str(end),
                "s3_path": s3_path,
                "needs_cut": True,
            }
            rec = _clip_record(row, video)
            rec["action_text"] = action_text
            rec["action_units"] = _action_units(action_text)
            rec["needs_cut"] = True
            rec["dur_s"] = round(dur, 1)
            rec["window_s"] = (start, end)
            out.append(rec)
    return out


def prefer_long_clips(clips: list[dict[str, Any]], *,
                     shuffle: bool = False) -> list[dict[str, Any]]:
    """Longos primeiro (≥10 min, depois ≥5 min), para encher horas com menos PUT."""
    long: list[dict[str, Any]] = []
    mid: list[dict[str, Any]] = []
    short: list[dict[str, Any]] = []
    for clip in clips:
        dur = float(clip.get("dur_s") or 0)
        if dur >= 600:
            long.append(clip)
        elif dur >= 300:
            mid.append(clip)
        else:
            short.append(clip)
    if shuffle:
        random.shuffle(long)
        random.shuffle(mid)
        random.shuffle(short)
    ordered = long + mid + short
    return cluster_by_parent(ordered)


# Só cola clipes colados de verdade. Gap de 30s + cobertura 0.7 mandava
# 20 min do vídeo-pai (carro, lab, quarto) no título de outra tarefa.
MERGE_MAX_GAP_S = 2.0
MERGE_MIN_COVERAGE = 0.95


def merge_ranked_spans(
    clips: list[dict[str, Any]],
    *,
    max_s: float = WINDOW_TARGET_S,
    max_gap_s: float = MERGE_MAX_GAP_S,
    min_coverage: float = MERGE_MIN_COVERAGE,
) -> list[dict[str, Any]]:
    """Junta clipes RANKEADOS consecutivos do mesmo pai (até 30 min).

    Só entra tempo que o matching da task já aprovou. Não corta o vídeo-pai
    inteiro — isso misturava ação da tarefa com o resto e o catbear rebaixa
    Task. Um clipe isolado permanece o mp4 oficial (sem needs_cut).
    """
    groups: dict[str, list[tuple[float, float, dict[str, Any]]]] = defaultdict(list)
    for clip in clips:
        parent = str(clip.get("parent_video_uid") or clip.get("clip_uid") or "")
        window = clip.get("window_s")
        if window and len(window) == 2:
            start, end = float(window[0]), float(window[1])
        else:
            start, end = 0.0, float(clip.get("dur_s") or 0)
        if end <= start:
            continue
        groups[parent].append((start, end, clip))
    out: list[dict[str, Any]] = []
    for parent, items in groups.items():
        items.sort(key=lambda row: (row[0], row[1]))
        i = 0
        while i < len(items):
            segs = [items[i]]
            j = i + 1
            while j < len(items):
                cur_end = max(seg[1] for seg in segs)
                nxt_start, nxt_end, _clip = items[j]
                if nxt_start - cur_end > max_gap_s:
                    break
                if max(cur_end, nxt_end) - segs[0][0] > max_s + 1e-6:
                    break
                segs.append(items[j])
                j += 1
            start = segs[0][0]
            end = max(seg[1] for seg in segs)
            if len(segs) == 1:
                out.append(segs[0][2])
            elif _span_coverage(segs, start, end) >= min_coverage:
                merged = _merged_span_record(parent, start, end, segs)
                if merged.get("needs_cut") and merged.get("s3_path"):
                    out.append(merged)
                else:
                    out.extend(seg[2] for seg in segs)
            else:
                out.extend(seg[2] for seg in segs)
            i = j
    return out


def _span_coverage(
    segs: list[tuple[float, float, dict[str, Any]]],
    start: float, end: float,
) -> float:
    span = end - start
    if span <= 0:
        return 0.0
    intervals = sorted((s, e) for s, e, _c in segs)
    covered = 0.0
    cs, ce = intervals[0]
    for s, e in intervals[1:]:
        if s <= ce:
            ce = max(ce, e)
        else:
            covered += ce - cs
            cs, ce = s, e
    covered += ce - cs
    return covered / span


def _merged_span_record(
    parent: str, start: float, end: float,
    segs: list[tuple[float, float, dict[str, Any]]],
) -> dict[str, Any]:
    base = dict(segs[0][2])
    uid = _window_uid(parent, start, end)
    video: dict[str, Any] = {}
    try:
        video = _cat().videos.get(parent) or {}
    except Exception:  # noqa: BLE001 — testes sem catálogo
        video = {}
    texts = [str(clip.get("action_text") or "") for _s, _e, clip in segs]
    text = " ".join(t for t in texts if t)
    s3, media_uid, media_offset = _best_media_source(
        parent, start, end, str(video.get("s3_path") or ""))
    base.update({
        "clip_uid": uid,
        "parent_video_uid": parent,
        "window_s": (start, end),
        "dur_s": round(end - start, 1),
        "s3_path": s3,
        "media_uid": media_uid,
        "media_time_offset_s": media_offset,
        "needs_cut": True,
        "action_text": text,
        "action_units": _action_units(text),
        "device": base.get("device") or str(video.get("device") or ""),
        "scenarios": base.get("scenarios") or scenario_values(video),
    })
    return base


def cluster_by_parent(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Agrupa janelas do mesmo pai (um download, vários cortes)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for clip in clips:
        parent = str(clip.get("parent_video_uid") or clip.get("clip_uid") or "")
        if parent not in groups:
            groups[parent] = []
            order.append(parent)
        groups[parent].append(clip)
    return [clip for parent in order for clip in groups[parent]]


# --- Download de vídeo e IMU --------------------------------------------------

def _extract_window(src: Path, start_s: float, dur_s: float, dest: Path) -> Path:
    """Corta [start, start+dur) com seek preciso e timestamps reiniciados.

    Stream-copy começava no keyframe anterior, enquanto a IMU começava no
    timestamp solicitado. Isso criava uma dessincronização silenciosa.
    """
    import subprocess
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".cut.tmp.mp4")
    from .sidecar import ffmpeg_bin
    ff = ffmpeg_bin()
    kwargs: dict[str, Any] = {
        "capture_output": True, "text": True, "timeout": 3600,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = [
        ff, "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-ss", f"{start_s:.6f}", "-t", f"{dur_s:.6f}", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264",
        "-preset", "ultrafast", "-c:a", "aac", "-avoid_negative_ts",
        "make_zero", "-reset_timestamps", "1", "-movflags", "+faststart",
        "-f", "mp4", str(tmp),
    ]
    res = subprocess.run(cmd, **kwargs)
    if res.returncode != 0:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(
            f"falha ao cortar janela Ego4D: {res.stderr.strip()[:400]}")
    tmp.replace(dest)
    return dest


def _valid_mp4_cache(path: Path) -> bool:
    try:
        if path.stat().st_size <= 1024:
            return False
        with path.open("rb") as stream:
            header = stream.read(64)
        return b"ftyp" in header
    except OSError:
        return False


def _valid_imu_cache(path: Path) -> bool:
    try:
        if path.stat().st_size <= 128:
            return False
        with path.open(encoding="utf-8", newline="") as stream:
            header = next(csv.reader(stream), [])
        required = {
            "canonical_timestamp_ms", "gyro_x", "gyro_y", "gyro_z",
            "accl_x", "accl_y", "accl_z",
        }
        return required.issubset(set(header))
    except (OSError, StopIteration, UnicodeError):
        return False


def download_clip(clip: dict[str, Any], dest: Path) -> Path:
    """Baixa o MP4 do clipe para `dest` (no-op se já existir).

    Janelas longas (`needs_cut`): baixa o vídeo-pai uma vez e corta localmente.
    """
    dest = Path(dest)
    if _valid_mp4_cache(dest):
        return dest
    s3_path = str(clip["s3_path"]).replace("s3://", "")
    bucket, _, key = s3_path.partition("/")
    needs_cut = bool(clip.get("needs_cut"))
    start = float(clip.get("parent_start_sec") or 0)
    end = float(clip.get("parent_end_sec") or 0)
    if needs_cut and end > start:
        media_uid = str(clip.get("media_uid") or clip.get("parent_video_uid")
                        or "parent")
        media_offset = float(clip.get("media_time_offset_s") or 0.0)
        parent_path = dest.parent / f"{media_uid}.mp4"
        if not _valid_mp4_cache(parent_path):
            _download_to(bucket, key, parent_path)
        return _extract_window(
            parent_path, start - media_offset, end - start, dest)
    _download_to(bucket, key, dest)
    return dest


def download_imu(video: dict[str, Any], dest: Path) -> Path | None:
    """Baixa o IMU real do vídeo pai para `dest`. None se o vídeo não tem IMU."""
    imu_meta = video.get("imu_metadata") or {}
    if video.get("has_imu") is not True or not imu_meta.get("s3_path"):
        return None
    if _valid_imu_cache(dest):
        return dest
    s3_path = imu_meta["s3_path"].replace("s3://", "")
    bucket, _, key = s3_path.partition("/")
    _download_to(bucket, key, dest)
    return dest


# --- Conversão da IMU para o sidecar ------------------------------------------

IMU_MIN_BUCKET_COVERAGE = 0.70
IMU_MAX_INTERPOLATION_GAP_MS = 50.0


def build_imu_csv(
    imu_csv_path: str | Path,
    window_s: tuple[float, float],
    sample_rate_hz: int = config.ANDROID_IMU_SAMPLE_RATE_HZ,
    duration_ms: int | None = None,
    seed: str | None = None,
) -> str:
    """Converte e valida a IMU oficial antes de gerar o sidecar ANDROID.

    Reamostra para 500 Hz (EgoImu.SAMPLING_PERIOD_US = 2000). A documentação
    do Ego4D registra timestamps não monotônicos, valores ausentes e
    componentes sem IMU. Por isso a entrada é lida por nome de coluna,
    ordenada pelo timestamp canônico e só interpola lacunas curtas. Uma janela
    sem cobertura confiável falha em vez de fabricar minutos com a última
    amostra observada.
    """
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz deve ser positivo")
    start_ms = float(window_s[0]) * 1000.0
    declared_end_ms = float(window_s[1]) * 1000.0
    if not (math.isfinite(start_ms) and math.isfinite(declared_end_ms)
            and declared_end_ms > start_ms):
        raise ValueError("janela de IMU inválida")
    if duration_ms is None:
        duration_ms = int(round(declared_end_ms - start_ms))
    duration_ms = int(duration_ms)
    if duration_ms <= 0:
        raise ValueError("duration_ms deve ser positivo")

    step_ms = 1000.0 / sample_rate_hz
    step_ns = int(round(1_000_000_000 / sample_rate_hz))
    n = max(1, int(duration_ms / 1000 * sample_rate_hz) + 1)
    sensor_end_ms = start_ms + duration_ms

    phase_ms = 0.0
    noise_rng: random.Random | None = None
    if seed:
        noise_rng = random.Random(seed)
        phase_ms = noise_rng.uniform(0.0, min(8.0, step_ms * 0.8))

    rows: list[tuple[float, tuple[float, float, float],
                     tuple[float, float, float]]] = []
    required = (
        "canonical_timestamp_ms", "gyro_x", "gyro_y", "gyro_z",
        "accl_x", "accl_y", "accl_z",
    )
    with open(imu_csv_path, encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = [name for name in required if name not in (reader.fieldnames or ())]
        if missing:
            raise RuntimeError(
                "CSV de IMU Ego4D sem colunas obrigatórias: " + ", ".join(missing))
        for row in reader:
            try:
                cms = float(row["canonical_timestamp_ms"])
                gyro = tuple(float(row[name]) for name in ("gyro_x", "gyro_y", "gyro_z"))
                accel = tuple(float(row[name]) for name in ("accl_x", "accl_y", "accl_z"))
            except (TypeError, ValueError):
                continue
            values = (cms, *gyro, *accel)
            if not all(math.isfinite(value) for value in values):
                continue
            # Não usamos `break`: o Ego4D documenta timestamps fora de ordem.
            if start_ms - step_ms <= cms <= sensor_end_ms + step_ms:
                rows.append((cms, gyro, accel))
    rows.sort(key=lambda item: item[0])
    if not rows:
        raise RuntimeError("janela do clipe sem amostras válidas de IMU")

    sums = [[0.0] * 6 for _ in range(n)]
    counts = [0] * n
    for cms, gyro, accel in rows:
        idx = math.floor((cms - start_ms - phase_ms) / step_ms)
        if not 0 <= idx < n:
            continue
        values = (*gyro, *accel)
        for col, value in enumerate(values):
            sums[idx][col] += value
        counts[idx] += 1

    valid = [idx for idx, count in enumerate(counts) if count]
    if not valid:
        raise RuntimeError("janela do clipe sem buckets válidos de IMU")
    max_gap_samples = max(
        1, int(math.ceil(IMU_MAX_INTERPOLATION_GAP_MS / step_ms)))
    leading_gap = valid[0]
    trailing_gap = (n - 1) - valid[-1]
    internal_gap = max(
        (right - left - 1 for left, right in zip(valid, valid[1:], strict=False)),
        default=0,
    )
    coverage = len(valid) / n
    minimum_coverage = (IMU_MIN_BUCKET_COVERAGE if duration_ms >= 1000 else 0.0)
    if (coverage < minimum_coverage
            or leading_gap > max_gap_samples
            or trailing_gap > max_gap_samples
            or internal_gap > max_gap_samples):
        raise RuntimeError(
            "cobertura IMU insuficiente para a janela "
            f"(buckets={coverage:.1%}, lacuna máxima="
            f"{max(leading_gap, trailing_gap, internal_gap) * step_ms:.0f}ms)")

    samples: list[tuple[tuple[float, float, float],
                        tuple[float, float, float]] | None] = [None] * n
    for idx in valid:
        values = [value / counts[idx] for value in sums[idx]]
        samples[idx] = ((values[0], values[1], values[2]),
                        (values[3], values[4], values[5]))

    previous: list[int | None] = [None] * n
    following: list[int | None] = [None] * n
    last: int | None = None
    for idx in range(n):
        if samples[idx] is not None:
            last = idx
        previous[idx] = last
    last = None
    for idx in range(n - 1, -1, -1):
        if samples[idx] is not None:
            last = idx
        following[idx] = last
    for idx, sample in enumerate(samples):
        if sample is not None:
            continue
        left, right = previous[idx], following[idx]
        if left is None:
            samples[idx] = samples[right]  # type: ignore[index]
        elif right is None:
            samples[idx] = samples[left]
        else:
            ratio = (idx - left) / (right - left)
            lg, la = samples[left]  # type: ignore[misc]
            rg, ra = samples[right]  # type: ignore[misc]
            gyro = tuple(
                a + (b - a) * ratio for a, b in zip(lg, rg, strict=True))
            accel = tuple(
                a + (b - a) * ratio for a, b in zip(la, ra, strict=True))
            samples[idx] = (gyro, accel)

    # Decorelação POR CONTA: o mesmo clipe injetado em N contas não pode sair
    # byte-idêntico (antes só havia ±0.002 de ruído). Ganhos/offsets pequenos e
    # determinísticos (via seed da conta) preservam a forma real do movimento.
    if noise_rng is not None:
        accel_gain = tuple(noise_rng.uniform(0.985, 1.015) for _ in range(3))
        gyro_gain = tuple(noise_rng.uniform(0.98, 1.02) for _ in range(3))
        accel_bias = tuple(noise_rng.uniform(-0.012, 0.012) for _ in range(3))
        gyro_bias = tuple(noise_rng.uniform(-0.002, 0.002) for _ in range(3))
    else:
        accel_gain = gyro_gain = (1.0, 1.0, 1.0)
        accel_bias = gyro_bias = (0.0, 0.0, 0.0)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(IMU_HDR)
    for idx, sample in enumerate(samples):
        assert sample is not None
        gyro, accel = sample
        # Android: grava o sensor COMO LIDO (sem negar o eixo z — convenção
        # de gravidade +z do Android; um aparelho real não inverte o sinal).
        accel = tuple(v * g + b for v, g, b in zip(accel, accel_gain, accel_bias))
        gyro = tuple(v * g + b for v, g, b in zip(gyro, gyro_gain, gyro_bias))
        if noise_rng is not None:
            accel = tuple(value + noise_rng.gauss(0, 0.002) for value in accel)
            gyro = tuple(value + noise_rng.gauss(0, 0.0002) for value in gyro)
        writer.writerow([
            idx * step_ns,
            f"{accel[0]:.6f}", f"{accel[1]:.6f}", f"{accel[2]:.6f}",
            f"{gyro[0]:.6f}", f"{gyro[1]:.6f}", f"{gyro[2]:.6f}",
        ])
    return out.getvalue()
