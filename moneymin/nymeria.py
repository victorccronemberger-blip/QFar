"""Read-only Nymeria selection from annotations and measured VRS clocks."""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import importlib
from importlib import metadata as importlib_metadata
import json
import math
import os
from bisect import bisect_left
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import config, nymeria_vrs, task_matching

_ALGORITHM = "nymeria-atomic-device-v1"
_SNAPSHOTS: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
_WINDOWS: OrderedDict[tuple, tuple[dict[str, Any], ...]] = OrderedDict()
_SDK_DIGESTS: OrderedDict[tuple, tuple[int, str]] = OrderedDict()
_SDK_WRAPPERS = (
    "__init__.py", "core/__init__.py", "core/calibration.py",
    "core/data_provider.py", "core/image.py", "core/sensor_data.py",
    "core/sophus.py", "core/stream_id.py", "core/vrs.py",
    "core/vrs_health_check.py",
)
_ANNOTATIONS = ("atomic_action.csv", "activity_summarization.csv", "motion_narration.csv")


def data_root() -> Path:
    override = os.environ.get("NYMERIA_ROOT", "").strip()
    return (Path(override).expanduser() if override else
            config.MEDIA_DATA_DIR / "nymeria").resolve()


def sequence_dirs(root: Path | None = None) -> list[Path]:
    base = Path(root or data_root()).resolve()
    if not base.is_dir():
        return []
    return [child for child in sorted(base.iterdir())
            if not child.is_symlink() and child.is_dir()
            and (child / "metadata.json").is_file()]


def load_metadata(seq_dir: Path) -> dict[str, Any]:
    value = json.loads((Path(seq_dir) / "metadata.json").read_text("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("metadata Nymeria inválido")
    return value


def _stat(path: Path) -> tuple:
    try:
        stat = path.stat()
    except OSError:
        return (str(path.resolve()), None)
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns,
            stat.st_ctime_ns, stat.st_dev, stat.st_ino)


def _signature(seq_dir: Path, *, fresh_sdk: bool = False) -> tuple:
    files = [seq_dir / "metadata.json", seq_dir / "recording_head/data/data.vrs",
             seq_dir / "recording_head/data/motion.vrs",
             *(seq_dir / "narration" / name for name in _ANNOTATIONS)]
    return (_ALGORITHM, str(seq_dir.resolve()), tuple(_stat(path) for path in files),
            _stat(Path(nymeria_vrs.__file__)), _stat(Path(task_matching.__file__)),
            os.environ.get("NYMERIA_VENV", ""), _sdk_signature(fresh=fresh_sdk), _rules_digest())


def _rules_digest() -> str:
    # Frozen modules may live inside PYZ; their declared rules remain available.
    rules = {name: vars(rule) for name, rule in task_matching.TASK_RULES.items()}
    value = json.dumps(rules, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def _sdk_content_digest(path: Path, *, fresh: bool = False) -> tuple[int, str]:
    marker = _stat(path)
    if not fresh and marker in _SDK_DIGESTS:
        return _SDK_DIGESTS[marker]
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    if _stat(path) != marker:
        raise ValueError("SDK Nymeria mudou durante a leitura")
    return _remember(_SDK_DIGESTS, marker, (size, digest.hexdigest()))


def _sdk_signature(*, fresh: bool = False) -> tuple:
    try:
        nymeria_vrs._bootstrap_projectaria()
    except (ImportError, OSError, RuntimeError):
        return (("unavailable", ()),)
    package = importlib.import_module("projectaria_tools")
    core = importlib.import_module("_core_pybinds")
    base = Path(package.__file__).resolve().parent
    native = Path(core.__file__).resolve()
    try:
        version = importlib_metadata.version("projectaria-tools")
    except importlib_metadata.PackageNotFoundError:
        version = "unknown"
    # PyInstaller extracts identical SDK bytes into a different _MEI directory
    # on each start. Persist content identity, keeping physical stats only in
    # the bounded digest cache. The fixed list also excludes incidental imports.
    files = [(f"projectaria_tools/{name}", base / name) for name in _SDK_WRAPPERS]
    files.append(("_core_pybinds", native))
    for prefix, folder in (("projectaria_tools", base),
                           ("projectaria_tools.libs", native.parent / "projectaria_tools.libs")):
        if folder.is_dir():
            files.extend((f"{prefix}/{path.relative_to(folder).as_posix()}", path)
                         for path in sorted(folder.rglob("*.dll")))
    return (("projectaria-tools.version", (version,)), *tuple(sorted(
        (name, _sdk_content_digest(path, fresh=fresh)) for name, path in files)))


def inventory_signature(root: Path | None = None) -> tuple:
    base = Path(root or data_root()).resolve()
    return (str(base), tuple(_signature(seq) for seq in sequence_dirs(base)))


def clear_caches() -> None:
    _SNAPSHOTS.clear()
    _WINDOWS.clear()


def _remember(cache: OrderedDict, key: tuple, value: Any) -> Any:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > 32:
        cache.popitem(last=False)
    return value


def _finite(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("tempo Nymeria inválido")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("tempo Nymeria inválido") from exc
    if not math.isfinite(result):
        raise ValueError("tempo Nymeria inválido")
    return result


def _continuous(values: tuple[int, ...], maximum_gap_ns: int) -> list[tuple[int, int]]:
    intervals = []
    left = values[0]
    for a, b in zip(values, values[1:]):
        if b - a > maximum_gap_ns:
            if a > left:
                intervals.append((left, a))
            left = b
    if values[-1] > left:
        intervals.append((left, values[-1]))
    return intervals


def _measured_streams(seq_dir: Path) -> dict[str, Any]:
    """Read SDK indexes, without decoding RGB or inventing metadata duration."""
    streams: dict[str, tuple[int, ...]] = {}
    serials: list[str] = []
    for kind, filename, label in (("rgb", "data.vrs", "camera-rgb"),
                                  ("imu", "motion.vrs", "imu-right")):
        path = seq_dir / "recording_head/data" / filename
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"{filename} Nymeria ausente")
        provider = nymeria_vrs._provider(path)
        if provider is None:
            raise ValueError("VRS Nymeria inválido")
        metadata_reader = getattr(provider, "get_metadata", None)
        if not callable(metadata_reader):
            raise ValueError("origem do dispositivo VRS Nymeria ausente")
        metadata = metadata_reader()
        serial = getattr(metadata, "device_serial", None)
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("origem do dispositivo VRS Nymeria ausente")
        serials.append(serial)
        sid = provider.get_stream_id_from_label(label)
        values = tuple(nymeria_vrs._stream_timestamps(provider, sid))
        if (len(values) < 2 or any(type(t) is not int or t < 0 for t in values)
                or any(b <= a for a, b in zip(values, values[1:]))):
            raise ValueError(f"índice DEVICE_TIME {kind} Nymeria inválido")
        streams[kind] = values
    if serials[0] != serials[1]:
        raise ValueError("RGB e IMU Nymeria pertencem a dispositivos diferentes")
    t0 = max(streams["rgb"][0], streams["imu"][0])
    t1 = min(streams["rgb"][-1], streams["imu"][-1])
    if t1 <= t0:
        raise ValueError("sem overlap DEVICE_TIME entre RGB e IMU Nymeria")
    # The IMU resampler has a real 25ms interpolation bound. RGB stays VFR:
    # absence of a capture never creates a duplicate frame or invented rate.
    intervals = []
    for ia, ib in _continuous(streams["imu"], 25_000_000):
        start, end = max(ia, t0), min(ib, t1)
        if end > start:
            intervals.append(((start - t0) / 1e9, (end - t0) / 1e9))
    return {"t0_ns": t0, "t1_ns": t1, "rgb": streams["rgb"],
            "imu_sample_count": len(streams["imu"]), "intervals": tuple(intervals),
            "head_device_sha256": hashlib.sha256(serials[0].encode()).hexdigest()}


def _annotation_rows(seq_dir: Path) -> tuple[list[tuple[float, float, str]], dict]:
    hashes: dict[str, str] = {}
    rows: list[tuple[float, float, str]] = []
    for filename in _ANNOTATIONS:
        path = seq_dir / "narration" / filename
        if not path.is_file():
            continue
        content = path.read_bytes()
        hashes[filename] = hashlib.sha256(content).hexdigest()
        # Summaries are lineage, not a replacement for atomic action evidence.
        if filename != "atomic_action.csv":
            continue
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
        required = {"start_time", "end_time", "Describe my atomic actions"}
        if not required.issubset(reader.fieldnames or ()):
            raise ValueError("formato de narração atômica Nymeria inválido")
        for row in reader:
            start, end = _finite(row["start_time"]), _finite(row["end_time"])
            text = str(row.get("Describe my atomic actions") or "").strip()
            if start < 0 or end <= start or not text:
                raise ValueError("intervalo de narração Nymeria inválido")
            rows.append((start, end, text))
    if not rows:
        raise ValueError("narrações atômicas Nymeria ausentes")
    return sorted(set(rows)), hashes


def _snapshot(seq_dir: Path, *, fresh: bool = False) -> dict[str, Any]:
    seq_dir = Path(seq_dir).resolve()
    signature = _signature(seq_dir, fresh_sdk=fresh)
    if not fresh and signature in _SNAPSHOTS:
        return _SNAPSHOTS[signature]
    metadata_bytes = (seq_dir / "metadata.json").read_bytes()
    metadata = json.loads(metadata_bytes.decode("utf-8-sig"))
    if not isinstance(metadata, dict):
        raise ValueError("metadata Nymeria inválido")
    rows, hashes = _annotation_rows(seq_dir)
    previous = _SNAPSHOTS.get(signature)
    measured = previous["measured"] if previous else _measured_streams(seq_dir)
    t0, t1 = measured["t0_ns"], measured["t1_ns"]
    rgb = measured["rgb"]
    relative: list[tuple[float, float, str]] = []
    for start, end, text in rows:
        # The real CSV truncates DEVICE seconds at microsecond precision:
        # all 470 pilot edges match RGB captures within 987ns. Require that
        # capture binding, rather than guessing offsets from metadata/scripts.
        for boundary in (start, end):
            tick = round(boundary * 1e9)
            index = bisect_left(rgb, tick)
            neighbors = rgb[max(0, index - 1): min(len(rgb), index + 1)]
            if (not neighbors or min(abs(tick - value) for value in neighbors)
                    > 1_001):
                raise ValueError("relógio de narração não comprovado pelo RGB DEVICE_TIME")
        start_ns, end_ns = round(start * 1e9), round(end * 1e9)
        if start_ns < t0 or end_ns > t1:
            continue
        relative.append(((start_ns - t0) / 1e9, (end_ns - t0) / 1e9, text))
    if not relative:
        raise ValueError("narração sem cobertura medida RGB/IMU Nymeria")
    events = tuple((start, text) for start, _end, text in relative)
    prepared = task_matching.prepare_span_events(events)
    labels = task_matching.label_span_events(prepared, task_matching.TASK_RULES.items())
    proof = {"metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
             "annotations_sha256": hashes,
             "source_signature": [list(item) for item in signature[2]],
             "rules_sha256": _rules_digest(),
             "source_time_domain": "DEVICE_TIME", "annotation_time_unit": "seconds",
             "head_device_sha256": measured["head_device_sha256"],
             "sdk_signature": [[name, list(marker)] for name, marker in _sdk_signature()
                               if isinstance(marker, tuple)],
             "measured_window_ns": [t0, t1]}
    if signature != _signature(seq_dir):
        raise ValueError("fontes Nymeria mudaram durante a leitura")
    return _remember(_SNAPSHOTS, signature, {
        "seq_id": seq_dir.name, "path": str(seq_dir), "metadata": metadata,
        "uid": str(metadata.get("uid") or seq_dir.name),
        "duration_s": (t1 - t0) / 1e9, "measured": measured,
        "rows": tuple(relative), "events": events, "prepared": prepared,
        "labels": labels, "proof": proof, "signature": signature})


def device_window_for_sequence(seq_dir: Path, *, start_s: float = 0.0,
                               end_s: float | None = None) -> tuple[int, int]:
    signature = _signature(Path(seq_dir))
    previous = _SNAPSHOTS.get(signature)
    measured = previous["measured"] if previous else _measured_streams(Path(seq_dir))
    t0, t1 = measured["t0_ns"], measured["t1_ns"]
    duration = (t1 - t0) / 1e9
    start = _finite(start_s)
    end = duration if end_s is None else _finite(end_s)
    if start < 0 or end <= start or end > duration:
        raise ValueError("janela Nymeria fora da cobertura medida")
    return t0 + round(start * 1e9), t0 + round(end * 1e9)


def measured_span_s(seq_dir: Path) -> float:
    t0, t1 = device_window_for_sequence(seq_dir)
    return (t1 - t0) / 1e9


def list_sequences(root: Path | None = None) -> list[dict[str, Any]]:
    items = []
    for seq_dir in sequence_dirs(root):
        try:
            metadata = load_metadata(seq_dir)
        except (OSError, ValueError, UnicodeError):
            continue
        motion = seq_dir / "recording_head/data/motion.vrs"
        video = seq_dir / "recording_head/data/data.vrs"
        try:
            snap = _snapshot(seq_dir)
        except (OSError, ValueError, RuntimeError, ImportError, UnicodeError):
            snap = None
        items.append({"seq_id": seq_dir.name, "uid": str(metadata.get("uid") or seq_dir.name),
                      "path": str(seq_dir), "script": str(metadata.get("script") or ""),
                      "location": str(metadata.get("location") or ""), "metadata": metadata,
                      "duration_s": snap["duration_s"] if snap else 0.0,
                      "has_motion_vrs": motion.is_file(), "has_data_vrs": video.is_file(),
                      "selection_ready": snap is not None})
    return items


def _bounds(min_dur_s: float, max_dur_s: float) -> tuple[float, float]:
    minimum, maximum = _finite(min_dur_s), _finite(max_dur_s)
    if minimum <= 0 or maximum < minimum:
        raise ValueError("faixa de duração Nymeria inválida")
    return minimum, maximum


def _windows(snap: dict, task_name: str, minimum: float, maximum: float) -> tuple[dict, ...]:
    key = (snap["signature"], json.dumps(snap["proof"], sort_keys=True),
           task_name, minimum, maximum)
    if key in _WINDOWS:
        return _WINDOWS[key]
    rule = task_matching.rule_for(task_name)
    if rule is None:
        return ()
    span_minimum = max(minimum, rule.min_span_s or 0.0)
    if span_minimum > maximum:
        return ()
    rivals = task_matching.competing_span_names(task_name, task_matching.TASK_RULES.items())
    windows = []
    components = []
    for a, b, _text in snap["rows"]:
        if components and a <= components[-1][1] + 1e-6:
            components[-1] = (components[-1][0], max(components[-1][1], b))
        else:
            components.append((a, b))
    for left, right in components:
        for imu_start, imu_end in snap["measured"]["intervals"]:
            lower, upper = max(left, imu_start), min(right, imu_end)
            if upper - lower < span_minimum:
                continue
            selected = [(event, label) for event, label in zip(snap["prepared"], snap["labels"])
                        if lower <= event[0] <= upper]
            prepared = tuple(event for event, _label in selected)
            labels = tuple(label for _event, label in selected)
            spans = task_matching.extract_spans(rule, (), min_s=span_minimum, max_s=maximum,
                prepared_events=prepared, task_name=task_name, event_task_names=labels,
                competing_task_names=rivals, video_duration_s=upper)
            for span in spans:
                start, end = max(lower, span["start"]), min(upper, span["end"])
                if not span_minimum <= end - start <= maximum:
                    continue
                uid = f"nymeria:{snap['seq_id']}:{start:.3f}:{end:.3f}"
                carrier = {"schema": 1, "dataset": "nymeria", "algorithm": _ALGORITHM,
                    "task": {"name": task_name, "id": None, "registry_key": None},
                    "window_s": [start, end], "duration_bounds_s": [minimum, maximum],
                    **copy.deepcopy(snap["proof"])}
                t0 = snap["measured"]["t0_ns"]
                windows.append({"clip_uid": uid, "exported_clip_uid": uid,
                    "seq_id": snap["seq_id"], "uid": snap["uid"], "path": snap["path"],
                    "window_s": [start, end], "device_window_ns": [t0 + round(start * 1e9), t0 + round(end * 1e9)],
                    "dur_s": end - start, "source": "nymeria", "needs_cut": True,
                    "parent_video_uid": snap["seq_id"], "scenario": rule.primary[0],
                    "task_name_authoritative": task_name, "selection_evidence": carrier,
                    "action_text": span["action_text"], "match_score": span["match_score"]})
    unique = {clip["clip_uid"]: clip for clip in windows}
    return _remember(_WINDOWS, key, tuple(sorted(unique.values(), key=lambda clip: -clip["dur_s"])))


def list_windows(seq: dict[str, Any], *, task_name: str | None = None,
                 min_dur_s: float = 60.0, max_dur_s: float = 1800.0) -> list[dict[str, Any]]:
    if not task_name or task_matching.rule_for(task_name) is None:
        return []
    minimum, maximum = _bounds(min_dur_s, max_dur_s)
    snap = _snapshot(Path(seq["path"]))
    name = task_matching.canonical_task_name(task_name)
    return copy.deepcopy(list(_windows(snap, name, minimum, maximum)))


def bind_task(clip: dict[str, Any], *, task_name: str, task_id: str | None = None,
              registry_key: str | None = None) -> dict[str, Any]:
    name = task_matching.canonical_task_name(task_name)
    result = copy.deepcopy(clip)
    carrier = result.get("selection_evidence")
    if (not isinstance(carrier, dict) or not isinstance(carrier.get("task"), dict)
            or carrier["task"].get("name") != name):
        raise ValueError("categoria Nymeria diverge da seleção")
    if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
        raise ValueError("identidade de tarefa Nymeria inválida")
    if registry_key is not None and (not isinstance(registry_key, str) or not registry_key.strip()):
        raise ValueError("registro de tarefa Nymeria inválido")
    if ((task_id is None) != (registry_key is None)
            or (task_id is not None and not registry_key.startswith(f"minute|{task_id}|"))):
        raise ValueError("registro e identidade da tarefa Nymeria divergem")
    carrier["task"].update(id=task_id, registry_key=registry_key)
    result.update(task_name_authoritative=name, task_id=task_id, registry_key=registry_key)
    return result


def automatic_candidates(*, task_name: str | None = None, task_id: str | None = None,
                         registry_key: str | None = None, min_dur_s: float = 60.0,
                         max_dur_s: float = 1800.0, root: Path | None = None,
                         max_results: int | None = None) -> list[dict[str, Any]]:
    if not task_name or task_matching.rule_for(task_name) is None:
        return []
    minimum, maximum = _bounds(min_dur_s, max_dur_s)
    out = []
    for seq_dir in sequence_dirs(root):
        try:
            snap = _snapshot(seq_dir)
            clips = _windows(snap, task_matching.canonical_task_name(task_name), minimum, maximum)
        except (OSError, ValueError, RuntimeError, ImportError, UnicodeError):
            continue
        out.extend(bind_task(clip, task_name=task_name, task_id=task_id,
                             registry_key=registry_key) for clip in clips)
    out.sort(key=lambda clip: -clip["dur_s"])
    return out if max_results is None else out[:max(0, int(max_results))]


def revalidate_candidate(clip: dict[str, Any], *, task_name: str | None = None,
                         task_id: str | None = None, registry_key: str | None = None,
                         fresh: bool = True) -> dict[str, Any]:
    carrier = clip.get("selection_evidence")
    if (not isinstance(carrier, dict) or carrier.get("schema") != 1
            or carrier.get("dataset") != "nymeria" or carrier.get("algorithm") != _ALGORITHM):
        raise ValueError("evidência Nymeria atual ausente")
    task = carrier.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("name"), str):
        raise ValueError("categoria Nymeria ausente")
    name = task_matching.canonical_task_name(task_name or task["name"])
    if name != task["name"] or task_matching.rule_for(name) is None:
        raise ValueError("categoria Nymeria diverge da seleção")
    for field, expected in (("id", task_id), ("registry_key", registry_key)):
        if expected is not None and expected != task.get(field):
            raise ValueError("identidade da tarefa Nymeria diverge da seleção")
    for field, expected in (("task_name_authoritative", name), ("task_id", task.get("id")),
                            ("registry_key", task.get("registry_key"))):
        if clip.get(field) != expected:
            raise ValueError("vínculo de tarefa Nymeria inválido")
    seq_dir = Path(str(clip.get("path") or "")).resolve()
    if seq_dir not in sequence_dirs():
        raise ValueError("sequência Nymeria fora da biblioteca atual")
    bounds = carrier.get("duration_bounds_s")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
        raise ValueError("faixa da evidência Nymeria inválida")
    minimum, maximum = _bounds(*bounds)
    snap = _snapshot(seq_dir, fresh=fresh)
    candidates = _windows(snap, name, minimum, maximum)
    match = next((item for item in candidates if item["clip_uid"] == clip.get("clip_uid")), None)
    if match is None:
        raise ValueError("janela sem evidência atual da tarefa Nymeria")
    match = bind_task(match, task_name=name, task_id=task.get("id"), registry_key=task.get("registry_key"))
    for field in ("seq_id", "parent_video_uid", "window_s", "device_window_ns", "dur_s",
                  "selection_evidence"):
        if clip.get(field) != match[field]:
            raise ValueError("evidência ou janela Nymeria desatualizada")
    return match
