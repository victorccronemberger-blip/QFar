"""Acesso estrito ao HoloAssist como fonte complementar de mídia.

Somente tarefas HoloAssist cujo objetivo inteiro coincide com uma tarefa Minute
entram no catálogo. A primeira integração é deliberadamente restrita a
montagem/desmontagem de móveis; tarefas de câmera, impressora, café, computador
e laboratório não são reinterpretadas como categorias domésticas.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import shutil
import threading
import urllib.request
from collections import Counter
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config, tls
from .remote_tar import RemoteTar

ANNOTATIONS_URL = (
    "https://hl2data.z5.web.core.windows.net/holoassist-data-release/"
    "data-annotation-trainval-v1_1.json"
)
SPLITS_URL = "https://holoassist.github.io/label_files/data-splits-v1_2.zip"
VIDEO_URL = (
    "https://hl2data.z5.web.core.windows.net/holoassist-data-release/"
    "video_pitch_shifted.tar"
)
COMPRESSED_VIDEO_URL = (
    "https://hl2data.z5.web.core.windows.net/holoassist-data-release/"
    "video_compress.tar"
)
IMU_URL = "https://hl2data.z5.web.core.windows.net/holoassist-data-release/imu.tar"

FURNITURE_TASK_TYPES = frozenset(
    {
        "assemble nightstand",
        "disassemble nightstand",
        "assemble stool",
        "disassemble stool",
        "assemble tray table",
        "disassemble tray table",
        "assemble utility cart",
        "disassemble utility cart",
    }
)

MINUTE_TASK_TYPES: dict[str, frozenset[str]] = {
    "Furniture Assembly/ Disassembly": FURNITURE_TASK_TYPES,
    "Furniture Assembly": frozenset(
        task for task in FURNITURE_TASK_TYPES if task.startswith("assemble ")
    ),
}
MIN_CORRECT_ACTION_RATIO = 0.95
_INDEX_SEED_DIR = Path(__file__).with_name("resources") / "holoassist"
_METADATA_LOCK = threading.Lock()


def data_dir() -> Path:
    return config.MEDIA_DATA_DIR / "holoassist"


def remote_archive(url: str, index_name: str) -> RemoteTar:
    """Abre um TAR usando o índice portátil distribuído com o projeto.

    Uma instalação nova não deve varrer milhares de cabeçalhos remotos antes
    do primeiro envio. O índice compactado acompanha o Git e é expandido uma
    única vez em ``data/holoassist``. O próprio ``RemoteTar`` invalida a cópia
    automaticamente se o ETag do arquivo oficial mudar.
    """
    index_path = data_dir() / index_name
    if not index_path.exists():
        seed = _INDEX_SEED_DIR / f"{index_name}.gz"
        if seed.exists():
            index_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = index_path.with_name(index_path.name + ".seed.tmp")
            try:
                with gzip.open(seed, "rb") as source, temporary.open("wb") as output:
                    shutil.copyfileobj(source, output)
                temporary.replace(index_path)
            finally:
                temporary.unlink(missing_ok=True)
    return RemoteTar(url, index_path)


def annotations_path() -> Path:
    return data_dir() / "annotations.trainval.v1_1.json"


def _download(
    url: str,
    destination: Path,
    progress: Callable[[str, int, int], None] | None = None,
) -> Path:
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    request = urllib.request.Request(url, headers={"User-Agent": "MoneyMin/0.3"})
    with tls.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        while block := response.read(4 * 1024 * 1024):
            output.write(block)
            downloaded += len(block)
            if progress:
                progress(destination.name, downloaded, total)
    temporary.replace(destination)
    return destination


def download_metadata(
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[Path, Path]:
    """Baixa somente anotações e splits (aprox. 117 MB), nunca os TARs pesados."""
    with _METADATA_LOCK:
        return (
            _download(ANNOTATIONS_URL, annotations_path(), progress),
            _download(SPLITS_URL, data_dir() / "data-splits-v1_2.zip", progress),
        )


@lru_cache(maxsize=4)
def _load_annotations(path: str, modified_ns: int) -> tuple[dict[str, Any], ...]:
    del modified_ns  # participa apenas da chave do cache
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("anotações HoloAssist deveriam ser uma lista")
    return tuple(record for record in raw if isinstance(record, dict))


def annotations() -> tuple[dict[str, Any], ...]:
    path = annotations_path()
    if not path.exists():
        download_metadata()
    if not path.exists():
        raise FileNotFoundError(
            "o QMoney não conseguiu preparar os metadados HoloAssist"
        )
    return _load_annotations(str(path), path.stat().st_mtime_ns)


@lru_cache(maxsize=6)
def _load_indexed_video_names(path: str, modified_ns: int) -> frozenset[str]:
    """Sessões que realmente possuem MP4 em um índice oficial de TAR."""
    del modified_ns  # participa somente da chave do cache
    source = Path(path)
    opener = gzip.open if source.suffix == ".gz" else open
    with opener(source, "rt", encoding="utf-8") as stream:
        raw = json.load(stream)
    members = raw.get("members", {}) if isinstance(raw, dict) else {}
    return frozenset(
        member.split("/", 1)[0]
        for member in members
        if isinstance(member, str) and member.lower().endswith(".mp4")
        and "/" in member
    )


def _indexed_video_names() -> frozenset[str] | None:
    """União pitch-shift/comprimido; ``None`` mantém compatibilidade sem índice.

    As anotações oficiais incluem algumas sessões sem vídeo em nenhum dos TARs.
    Filtrá-las aqui evita que uma campanha escolha um UID impossível de baixar.
    """
    names: set[str] = set()
    found_index = False
    for index_name in ("video.index.json", "video_compress.index.json"):
        local = data_dir() / index_name
        source = local if local.exists() else _INDEX_SEED_DIR / f"{index_name}.gz"
        if not source.exists():
            continue
        found_index = True
        try:
            names.update(_load_indexed_video_names(
                str(source), source.stat().st_mtime_ns))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return frozenset(names) if found_index and names else None


def _duration_s(record: dict[str, Any]) -> float:
    return float(
        (((record.get("videoMetadata") or {}).get("duration") or {}).get("seconds"))
        or 0
    )


def action_quality(record: dict[str, Any]) -> tuple[float, bool]:
    """Retorna proporção correta e presença de erro que ficou sem correção."""
    states = [
        str((event.get("attributes") or {}).get("Action Correctness") or "")
        for event in record.get("events") or []
        if event.get("label") == "Fine grained action"
    ]
    if not states:
        return 0.0, False
    ratio = states.count("Correct Action") / len(states)
    return ratio, "Wrong Action, not corrected" in states


def _clip_record(record: dict[str, Any]) -> dict[str, Any]:
    video_name = str(record.get("video_name") or "")
    task_type = str(record.get("taskType") or "")
    correct_ratio, has_uncorrected_error = action_quality(record)
    return {
        "clip_uid": f"holoassist:{video_name}",
        "video_uid": video_name,
        "video_name": video_name,
        "dur_s": _duration_s(record),
        "device": "Microsoft HoloLens 2",
        "scenarios": [task_type],
        "scenario": task_type,
        "source": "holoassist",
        "has_imu": True,
        "needs_cut": False,
        "task_type": task_type,
        "correct_action_ratio": correct_ratio,
        "has_uncorrected_error": has_uncorrected_error,
    }


def list_clips(
    task_name: str,
    *,
    min_dur_s: float = 60,
    max_dur_s: float = 1800,
) -> list[dict[str, Any]]:
    """Lista gravações cuja tarefa completa é compatível com a tarefa Minute."""
    accepted = MINUTE_TASK_TYPES.get(task_name)
    if not accepted:
        return []
    indexed_names = _indexed_video_names()
    clips = [
        _clip_record(record)
        for record in annotations()
        if str(record.get("taskType") or "") in accepted
        and min_dur_s <= _duration_s(record) <= max_dur_s
        and record.get("video_name")
        and action_quality(record)[0] >= MIN_CORRECT_ACTION_RATIO
        and not action_quality(record)[1]
        and (indexed_names is None or str(record.get("video_name")) in indexed_names)
    ]
    return sorted(clips, key=lambda clip: (-float(clip["dur_s"]), clip["clip_uid"]))


def find_clip(clip_uid: str) -> dict[str, Any] | None:
    """Localiza somente gravações elegíveis pela política global de qualidade.

    A compatibilidade com a tarefa Minute ainda deve ser conferida por
    ``list_clips(task_name)``; isso impede que seleção explícita contorne o
    limiar de qualidade, mas não adivinha a categoria de destino.
    """
    prefix = "holoassist:"
    video_name = clip_uid[len(prefix) :] if clip_uid.startswith(prefix) else clip_uid
    indexed_names = _indexed_video_names()
    if indexed_names is not None and video_name not in indexed_names:
        return None
    for record in annotations():
        ratio, has_uncorrected = action_quality(record)
        if (
            record.get("video_name") == video_name
            and str(record.get("taskType") or "") in FURNITURE_TASK_TYPES
            and ratio >= MIN_CORRECT_ACTION_RATIO
            and not has_uncorrected
        ):
            return _clip_record(record)
    return None


def report() -> dict[str, Any]:
    records = annotations()
    task_types = Counter(str(record.get("taskType") or "") for record in records)
    compatible = {
        task: sum(task_types[task_type] for task_type in accepted)
        for task, accepted in MINUTE_TASK_TYPES.items()
    }
    compatible_hours = {
        task: round(
            sum(
                _duration_s(record)
                for record in records
                if str(record.get("taskType") or "") in accepted
            )
            / 3600,
            2,
        )
        for task, accepted in MINUTE_TASK_TYPES.items()
    }
    strict_clips = {
        task: list_clips(task, min_dur_s=60, max_dur_s=1800)
        for task in MINUTE_TASK_TYPES
    }
    return {
        "recordings": len(records),
        "hours": round(sum(_duration_s(record) for record in records) / 3600, 2),
        "task_types": dict(sorted(task_types.items())),
        "compatible_recordings": compatible,
        "compatible_hours": compatible_hours,
        "strict_compatible_recordings": {
            task: len(clips) for task, clips in strict_clips.items()
        },
        "strict_compatible_hours": {
            task: round(sum(float(clip["dur_s"]) for clip in clips) / 3600, 2)
            for task, clips in strict_clips.items()
        },
        "quality_policy": {
            "min_correct_action_ratio": MIN_CORRECT_ACTION_RATIO,
            "reject_uncorrected_errors": True,
        },
    }


def _video_member_name(video_name: str, compressed: bool) -> str:
    filename = "Video_compress.mp4" if compressed else "Video_pitchshift.mp4"
    return f"{video_name}/Export_py/{filename}"


def download_video(
    video_name: str,
    *,
    compressed: bool = False,
    progress: Callable[[str, int, int], None] | None = None,
) -> Path:
    """Extrai por HTTP Range somente o MP4 pedido do TAR oficial."""
    filename = "Video_compress.mp4" if compressed else "Video_pitchshift.mp4"
    destination = data_dir() / "recordings" / video_name / filename
    if destination.exists():
        return destination
    url = COMPRESSED_VIDEO_URL if compressed else VIDEO_URL
    index_name = "video_compress.index.json" if compressed else "video.index.json"
    wanted = _video_member_name(video_name, compressed)
    archive = remote_archive(url, index_name)
    member = archive.find(
        lambda item: item.name.lstrip("./") == wanted,
        progress=(lambda current, total: progress("video_index", current, total))
        if progress else None,
    )
    return archive.extract(
        member,
        destination,
        progress=(lambda current, total: progress("video_download", current, total))
        if progress else None,
    )


def download_imu(
    video_name: str,
    *,
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Path]:
    """Extrai acelerômetro, giroscópio e magnetômetro de uma sessão."""
    destination = data_dir() / "recordings" / video_name / "IMU"
    names = ("Accelerometer_sync.txt", "Gyroscope_sync.txt", "Magnetometer_sync.txt")
    result: dict[str, Path] = {}
    archive = remote_archive(IMU_URL, "imu.index.json")
    for name in names:
        path = destination / name
        if not path.exists():
            wanted = f"{video_name}/Export_py/IMU/{name}"
            member = archive.find(
                lambda item, expected=wanted: item.name.lstrip("./") == expected,
                progress=(lambda current, total: progress("imu_index", current, total))
                if progress else None,
            )
            archive.extract(
                member,
                path,
                progress=(lambda current, total: progress(
                    "imu_download", current, total
                )) if progress else None,
            )
        result[name] = path
    return result


def _sensor_rows(path: Path) -> list[tuple[float, float, float, float]]:
    """Lê linhas PSI Studio aceitando tab, espaço ou vírgula como separador."""
    rows: list[tuple[float, float, float, float]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        parts = next(csv.reader([raw.replace("\t", ",").replace(" ", ",")]))
        values = [part for part in parts if part]
        try:
            numeric = [float(value) for value in values]
        except ValueError:
            continue
        if len(numeric) >= 4:
            rows.append((numeric[0], numeric[-3], numeric[-2], numeric[-1]))
    return rows


def build_imu_csv(
    accelerometer: Path,
    gyroscope: Path,
    *,
    duration_ms: int,
    sample_rate_hz: int = 100,
) -> str:
    """Converte sensores sincronizados do HoloLens para o sidecar iOS.

    A grade do giroscópio é a âncora. O vizinho de acelerômetro mais próximo é
    associado a cada amostra e o sinal é reamostrado para 100 Hz.
    """
    accel = _sensor_rows(accelerometer)
    gyro = _sensor_rows(gyroscope)
    if not accel or not gyro:
        raise RuntimeError("IMU HoloAssist sem amostras sincronizadas")
    # A primeira coluna pode estar em segundos ou em ticks de 100 ns. Normalize
    # pela própria duração observada, preservando a ordem e a forma do sinal.
    start = gyro[0][0]
    span = gyro[-1][0] - start
    scale = (duration_ms / 1000) / span if span > 0 else 1.0
    accel_times = [(row[0] - accel[0][0]) * scale for row in accel]
    gyro_times = [(row[0] - start) * scale for row in gyro]
    count = max(1, int(duration_ms / 1000 * sample_rate_hz) + 1)
    step_s = 1.0 / sample_rate_hz
    step_ns = 1_000_000_000 // sample_rate_hz
    ai = gi = 0
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["t", "ax", "ay", "az", "wx", "wy", "wz"])
    for index in range(count):
        target = index * step_s
        while ai + 1 < len(accel) and abs(accel_times[ai + 1] - target) <= abs(
            accel_times[ai] - target
        ):
            ai += 1
        while gi + 1 < len(gyro) and abs(gyro_times[gi + 1] - target) <= abs(
            gyro_times[gi] - target
        ):
            gi += 1
        _, ax, ay, az = accel[ai]
        _, gx, gy, gz = gyro[gi]
        writer.writerow(
            [
                index * step_ns,
                f"{ax:.6f}",
                f"{ay:.6f}",
                f"{-az:.6f}",
                f"{gx:.6f}",
                f"{gy:.6f}",
                f"{gz:.6f}",
            ]
        )
    return output.getvalue()


__all__ = [
    "FURNITURE_TASK_TYPES",
    "MIN_CORRECT_ACTION_RATIO",
    "MINUTE_TASK_TYPES",
    "annotations",
    "build_imu_csv",
    "download_imu",
    "download_metadata",
    "download_video",
    "find_clip",
    "list_clips",
    "report",
    "remote_archive",
]
