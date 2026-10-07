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
import re
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import config, ego4d, nymeria_vrs, task_matching
from .background_work import report_progress

_ALGORITHM = "nymeria-atomic-device-v4"
_PLANNED_ALGORITHM = _ALGORITHM + "-planned"
_HANGER_ADJECTIVE = re.compile(r"\bclothes\s+(hangers?)\b", re.IGNORECASE)
_DISHWASHER_OBJECT = (
    r"(?:dishes|plates?|cups?|bowls?|glasses|cutlery|utensils?|spoons?|forks?|"
    r"pans?|spatulas?|colanders?|strainers?|trays?|tongs|platters?|pots?|"
    r"chopping boards?|kitchenware)"
)
_DISHWASHER_MODIFIERS = task_matching._OBJECT_MODIFIERS
_DISHWASHER_MACHINE = (
    r"(?:(?:a|the|kitchen|top|bottom|upper|lower)\s+)*"
    r"(?:dish\s?washer(?:['’]s)?(?:\s+(?:(?:top|bottom|upper|lower)\s+)?rack)?|"
    r"rack\s+of\s+(?:(?:a|the|kitchen)\s+)*dish\s?washer)"
)
# The official task requires loading AND starting, or unloading AND putting
# dishes away. Appliance transfers alone do not complete either alternative.
_DISHWASHER_PHASE_PATTERNS = {
    "load": (
        r"\b(?:loads?|loading|loaded)\s+" + _DISHWASHER_MODIFIERS + r"dish\s?washer\b|"
        r"\b(?:loads?|loading|loaded|puts?|putting|plac\w*|insert\w*)\s+(?:down\s+)?" +
        _DISHWASHER_MODIFIERS + _DISHWASHER_OBJECT + r"\b"
        r"(?:(?!\b(?:beside|near|next)\b).){0,110}\b(?:in|into|on|onto)\s+" +
        _DISHWASHER_MACHINE + r"\b"
    ),
    "start": (
        r"\b(?:starts?|starting|started)\s+" + _DISHWASHER_MODIFIERS + r"dish\s?washer\b|"
        r"\b(?:press\w*|push\w*)\s+" + _DISHWASHER_MODIFIERS + r"(?:start|cycle)\s+button\b"
        r".{0,45}\b(?:on|of)\s+" + _DISHWASHER_MACHINE + r"\b|"
        r"\b(?:press\w*|push\w*)\s+" + _DISHWASHER_MODIFIERS +
        r"dish\s?washer(?:['’]s)?\s+(?:start|cycle)\s+button\b"
    ),
    "unload": (
        r"\b(?:unloads?|unloading|unloaded)\s+" + _DISHWASHER_MODIFIERS + r"dish\s?washer\b"
        r"(?:\s+(?:of|with)\s+" + _DISHWASHER_MODIFIERS + _DISHWASHER_OBJECT + r"\b)?|"
        r"\b(?:unloads?|unloading|unloaded|remov\w*|takes?|taking|picks? up|picking up)\s+" +
        _DISHWASHER_MODIFIERS + _DISHWASHER_OBJECT + r"\b"
        r"(?:(?!\b(?:beside|near|next)\b).){0,90}\b(?:from|out of)\s+" +
        _DISHWASHER_MACHINE + r"\b"
    ),
    "put_away": (
        r"\b(?:puts?|putting|plac\w*|return\w*|stor\w*)\s+" +
        _DISHWASHER_MODIFIERS + _DISHWASHER_OBJECT + r"\b"
        r"(?:(?!\b(?:beside|near|next)\b).){0,90}\b(?:in|into|inside|on|onto|to)\s+" +
        _DISHWASHER_MODIFIERS + r"(?:kitchen\s+)?(?:cabinet|cupboard|drawer|shelf|shelves)\b|"
        r"\b(?:puts?|putting)\s+away\s+" + _DISHWASHER_MODIFIERS + _DISHWASHER_OBJECT + r"\b|"
        r"\b(?:puts?|putting)\s+" + _DISHWASHER_MODIFIERS + _DISHWASHER_OBJECT + r"\s+away\b"
    ),
}
_DISHWASHER_PHASES = {name: re.compile(pattern, re.IGNORECASE)
                      for name, pattern in _DISHWASHER_PHASE_PATTERNS.items()}
_DISHWASHER_DIRTY_OBJECT_PATTERN = (
    r"\b(?:dirty|soiled|unwashed|greasy)\s+" + _DISHWASHER_MODIFIERS + _DISHWASHER_OBJECT + r"\b|"
    r"\b" + _DISHWASHER_OBJECT + r"\s+(?:(?:that|which)\s+)?"
    r"(?:are|is|remain|remains)\s+(?:still\s+)?(?:dirty|soiled|unwashed|greasy)\b"
)
_DISHWASHER_DIRTY_OBJECT = re.compile(_DISHWASHER_DIRTY_OBJECT_PATTERN, re.IGNORECASE)
_DISHWASHER_OTHER_ACTOR_SUBJECT_PATTERN = (
    r"\b(?:peer|observer|another person|other person|someone)\s+"
    r"(?:(?:is|was|then|also|now|who|as|he|she|to)\s+){0,4}$"
)
_DISHWASHER_OTHER_ACTOR_SUBJECT = re.compile(
    _DISHWASHER_OTHER_ACTOR_SUBJECT_PATTERN, re.IGNORECASE)
# Nymeria captions name water while washing utensils. Broad yard categories
# may compete with a kitchen workflow only when a timed caption proves an
# actual yard action and its object, rather than merely mentioning water.
_GARDEN_OBJECT_MODIFIERS = r"(?:(?:a|an|the|some|his|her|their|small|large|green|outdoor)\s+)*"
_GARDEN_ACTION = (
    r"\b(?:waters?|watering|sprays?|spraying|sprinkles?|sprinkling)\s+" +
    _GARDEN_OBJECT_MODIFIERS + r"(?:plants?|flowers?|gardens?|grass|lawns?|crops?|seedlings?|soil)\b|"
    r"\b(?:pours?|pouring|sprays?|spraying|sprinkles?|sprinkling)\s+water\b.{0,55}"
    r"\b(?:on|onto|over|into|around)\s+" + _GARDEN_OBJECT_MODIFIERS +
    r"(?:plants?|flowers?|gardens?|grass|lawns?|crops?|seedlings?|soil)\b|"
    r"\b(?:mows?|mowing|trims?|trimming|prunes?|pruning|cuts?|cutting)\s+" +
    _GARDEN_OBJECT_MODIFIERS + r"(?:grass|lawns?|hedges?|shrubs?|bushes?|branches?)\b|"
    r"\b(?:pulls?|pulling|plucks?|plucking|uproots?|uprooting|removes?|removing)\s+" +
    _GARDEN_OBJECT_MODIFIERS + r"(?:weeds?|grass)\b|"
    r"\b(?:rakes?|raking|blows?|blowing)\s+" + _GARDEN_OBJECT_MODIFIERS +
    r"(?:leaves|grass clippings|lawn clippings)\b|"
    r"\b(?:plants?|planting|digs?|digging|fertilizes?|fertilizing|composts?|composting)\s+" +
    _GARDEN_OBJECT_MODIFIERS + r"(?:seeds?|seedlings?|plants?|flowers?|soil|garden)\b"
)
_SNAPSHOTS: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
_WINDOWS: OrderedDict[tuple, tuple[dict[str, Any], ...]] = OrderedDict()
_PLANNED: OrderedDict[tuple, tuple[dict[str, Any], ...]] = OrderedDict()
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


def _rules_digest(rules=None) -> str:
    # Frozen modules may live inside PYZ; their declared rules remain available.
    rules = {name: vars(rule) for name, rule in
             (selection_rules() if rules is None else rules).items()}
    value = json.dumps({"algorithm": _ALGORITHM, "rules": rules,
                       "dishwasher_phase_patterns": _DISHWASHER_PHASE_PATTERNS,
                       "dishwasher_dirty_object": _DISHWASHER_DIRTY_OBJECT_PATTERN,
                       "dishwasher_other_actor_subject": _DISHWASHER_OTHER_ACTOR_SUBJECT_PATTERN},
                       sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def selection_rule_for(task_name: str):
    """Atomic captions need task context absent from Nymeria's script label.

    Ego4D's human car-washing scene can contextualize a caption such as
    ``vacuum the seat``. Nymeria's generic housekeeping script cannot prove
    that the room being vacuumed is a car interior.
    """
    name = task_matching.canonical_task_name(task_name)
    rule = task_matching.rule_for(name)
    if rule is None:
        return None
    if name in {"Gardening", "Full Yard Maintenance"}:
        return replace(rule, required_action_pattern=_GARDEN_ACTION)
    if name == "Clean Appliance":
        # Retrieving a pan from the stove and rinsing that pan at the sink
        # does not prove that the stove itself is being cleaned.
        return replace(rule, required_action_pattern=(
            r"\b(?:cleans?|cleaning|cleaned|scrubs?|scrubbing|scrubbed|wipes?|wiping|wiped|"
            r"rinses?|rinsing|rinsed|descales?|descaling|descaled)\s+(?:down\s+|off\s+)?" +
            task_matching._OBJECT_MODIFIERS + r"(?:kitchen\s+)?"
            r"(?:oven|fridge|refrigerator|freezer|microwave|coffee maker|washing machine|"
            r"washer|dishwasher|appliance|air fryer|cooker|stove|cooktop)\b"))
    if name == "Cleaning Out Car" and rule is not None:
        return replace(rule, evidence=(*rule.evidence,
            ("car", "vehicle", "automobile", "van", "truck", "dashboard", "car seat")),
            min_evidence_groups=None, unit_min_evidence_groups=None)
    if name == "Organize the Garage":
        actions = ("sorts", "sorting", "sort items", "sort boxes", "sort tools", "sort the", "sort a",
                   "organizes", "organises", "organizing", "organising", "organize the", "organise the",
                   "arranges", "arranging", "arrange the", "arrange a", "arrange items", "arrange tools",
                   "rearranges", "rearranging", "tidies", "tidying", "tidy the", "tidy a")
        return replace(rule, evidence=(*rule.evidence[:-1], actions),
                       min_evidence_groups=None, unit_min_evidence_groups=None)
    if name == "Stack firewood":
        return replace(rule, evidence=(("firewood", "log", "woodpile", "wood pile"),
                                      *rule.evidence[1:]),
                       action_excluded=(*rule.action_excluded, "jenga", "puzzle", "board game", "game pieces"),
                       min_evidence_groups=None, unit_min_evidence_groups=None)
    if name in {"Watering Outdoor Plants", "Water Houseplants"}:
        actions = ("waters", "watering the", "watering a", "watering plants", "watering flowers",
                   "watering crops", "watering seedlings", "water the", "water a", "water plants",
                   "water flowers", "water crops", "pours water", "pouring water", "pour water",
                   "sprays water", "spraying water", "sprinkles water", "sprinkling water",
                   "sprays the plants", "spraying the plants", "sprays the flowers", "spraying the flowers",
                   "turns on the sprinkler", "starts the sprinkler", "runs the sprinkler")
        context = (("outdoor", "outside", "yard", "backyard", "garden", "patio", "porch", "balcony", "field", "crop")
                   if name == "Watering Outdoor Plants" else
                   ("indoor", "inside", "houseplant", "living room", "bedroom", "kitchen", "room", "windowsill", "home"))
        objects = (*rule.evidence[1], "houseplant") if name == "Water Houseplants" else rule.evidence[1]
        return replace(rule, evidence=(actions, objects, context),
                       min_evidence_groups=None, unit_min_evidence_groups=None)
    if name == "Holiday Decoration Setup":
        actions = ("hangs", "hanging a decoration", "hanging the decoration", "hang a decoration",
                   "hang the decoration", "decorates", "decorating", "sets up", "setting up", "set up",
                   "unpacks", "unpacking", "attaches", "attaching", "installs", "installing",
                   "removes", "removing", "takes down", "taking down", "packs", "packing",
                   "unhooks", "unhooking", "detaches", "detaching")
        return replace(rule, evidence=(*rule.evidence[:-1], actions),
                       action_excluded=rule.action_excluded,
                       min_evidence_groups=None, unit_min_evidence_groups=None)
    return rule


def selection_rules():
    return {name: selection_rule_for(name) for name in task_matching.TASK_RULES}


def selection_activity_mode(task_name: str) -> bool:
    # Loading/unloading includes recurring rack transfers and preparation
    # between them. Use the existing activity proof; it still stops at
    # hygiene, a real competing action, or sustained foreign activity.
    return task_matching.canonical_task_name(task_name) == "Using the Dishwasher"


def selection_window_complete(task_name: str, rows, start: float, end: float) -> bool:
    """Require both official phases entirely inside the actual selected cut."""
    if task_matching.canonical_task_name(task_name) != "Using the Dishwasher":
        return True
    observed = {name: [] for name in _DISHWASHER_PHASES}
    for left, right, text in rows:
        # Atomic CSV endpoints have microsecond precision. Do not borrow a
        # phase that begins in this window but finishes beyond its boundary.
        if left < start - 1e-6 or right > end + 1e-6:
            continue
        for unit_index, unit in enumerate(task_matching._camera_wearer_segments(text)):
            for phase, pattern in _DISHWASHER_PHASES.items():
                for match in pattern.finditer(unit):
                    # Explicitly dirty dishes cannot prove the official clean
                    # unloading alternative. Scope this to the transferred
                    # object; loading dirty dishes remains valid.
                    if (phase in {"unload", "put_away"}
                            and _DISHWASHER_DIRTY_OBJECT.search(match.group(0))):
                        continue
                    # Nymeria also spells the other participant as "peer",
                    # rather than Ego's #O. Their action cannot finish C's task.
                    if _DISHWASHER_OTHER_ACTOR_SUBJECT.search(unit[:match.start()]):
                        continue
                    observed[phase].append((left, unit_index, match.start()))
    # Ordering also matters: starting before loading cannot finish the load
    # performed later; another actor's #O phase is never a wearer completion.
    return bool(
        (observed["load"] and observed["start"]
         and max(observed["load"]) < max(observed["start"]))
        or (observed["unload"] and observed["put_away"]
            and max(observed["unload"]) < max(observed["put_away"]))
    )


def selection_text(text: str) -> str:
    """A clothes hanger names the hanger; it does not prove a garment.

    Keep the original caption and its timestamps/hashes as source evidence.
    This normalization affects only action matching, including catalog plans.
    """
    return _HANGER_ADJECTIVE.sub(r"\1", text)


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
    _PLANNED.clear()


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


def _annotation_rows(seq_dir: Path, *, catalog_only: bool = False) -> tuple[list[tuple[float, float, str]], dict]:
    hashes: dict[str, str] = {}
    rows: list[tuple[float, float, str]] = []
    for filename in _ANNOTATIONS:
        path = seq_dir / "narration" / filename
        if not path.is_file():
            continue
        if catalog_only:
            content, _size, digest = ego4d._selection_source(path)
            if content is None:
                continue
        else:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
        hashes[filename] = digest
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
    # Importing the complete source catalog creates metadata/annotations for
    # recordings whose media has not been downloaded. Fail before hashing the
    # SDK and parsing their CSV on every campaign task lookup.
    for name in ("recording_head/data/data.vrs", "recording_head/data/motion.vrs",
                 "narration/atomic_action.csv"):
        required = seq_dir / name
        if not required.is_file() or required.stat().st_size <= 0:
            raise ValueError(f"{required.name} Nymeria ausente")
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
    prepared = task_matching.prepare_span_events((start, selection_text(text)) for start, text in events)
    labels = task_matching.label_span_events(prepared, selection_rules().items())
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
    rule = selection_rule_for(task_name)
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
                competing_task_names=rivals, video_duration_s=upper,
                activity_mode=selection_activity_mode(task_name))
            for span in spans:
                start, end = max(lower, span["start"]), min(upper, span["end"])
                if not span_minimum <= end - start <= maximum:
                    continue
                if not selection_window_complete(task_name, snap["rows"], start, end):
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
                    "source_clock_domain": "aria_DEVICE_TIME_ns", "window_origin_device_timestamp_ns": t0,
                    "dur_s": end - start, "source": "nymeria", "needs_cut": True,
                    "parent_video_uid": snap["seq_id"], "scenario": rule.primary[0],
                    "task_name_authoritative": task_name, "selection_evidence": carrier,
                    "capacity_kind": "measured_rgb_imu_window", "readiness": "measured",
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
                         max_results: int | None = None,
                         include_planned: bool = False,
                         catalog_only: bool = False) -> list[dict[str, Any]]:
    if not task_name or task_matching.rule_for(task_name) is None:
        return []
    minimum, maximum = _bounds(min_dur_s, max_dur_s)
    out = (planned_candidates(task_name=task_name, task_id=task_id, registry_key=registry_key,
                             min_dur_s=minimum, max_dur_s=maximum, root=root)
           if include_planned else [])
    planned_sequences = {clip["seq_id"] for clip in out}
    if catalog_only:
        # Reuse measured in-memory evidence when already acquired in this
        # process. Listing categories must never open the SDK or sensor media.
        base = Path(root or data_root()).resolve()
        for snap in tuple(_SNAPSHOTS.values()):
            seq_dir = Path(snap["path"])
            if seq_dir.parent != base or seq_dir.name in planned_sequences:
                continue
            current = tuple(_stat(seq_dir / name) for name in
                ("metadata.json", "recording_head/data/data.vrs", "recording_head/data/motion.vrs",
                 *("narration/" + value for value in _ANNOTATIONS)))
            if current != snap["signature"][2] or snap["proof"]["rules_sha256"] != _rules_digest():
                continue
            clips = _windows(snap, task_matching.canonical_task_name(task_name), minimum, maximum)
            out.extend(bind_task(clip, task_name=task_name, task_id=task_id,
                                 registry_key=registry_key) for clip in clips)
        out.sort(key=lambda clip: -clip["dur_s"])
        return out if max_results is None else out[:max(0, int(max_results))]
    for seq_dir in sequence_dirs(root):
        # A queued annotation window is measured when actually prepared.
        # Existing sources without an acquisition plan retain the old API.
        if seq_dir.name in planned_sequences:
            continue
        try:
            snap = _snapshot(seq_dir)
            clips = _windows(snap, task_matching.canonical_task_name(task_name), minimum, maximum)
        except (OSError, ValueError, RuntimeError, ImportError, UnicodeError):
            continue
        out.extend(bind_task(clip, task_name=task_name, task_id=task_id,
                             registry_key=registry_key) for clip in clips)
    out.sort(key=lambda clip: -clip["dur_s"])
    return out if max_results is None else out[:max(0, int(max_results))]


def _planned_sequence(seq_dir: Path, groups: dict, task_name: str,
                      minimum: float, maximum: float) -> list[dict[str, Any]]:
    """Bind annotation estimates without opening, downloading or measuring VRS."""
    return _planned_sequence_batch(seq_dir, groups, [task_name], minimum, maximum)


def _planned_sequence_batch(seq_dir: Path, groups: dict, names,
                            minimum: float, maximum: float, *, rules=None,
                            rules_digest=None, rivals=None,
                            catalog_only=False) -> list[dict[str, Any]]:
    """Classify a recording once for every requested task, with identical proof."""
    from . import nymeria_library as library
    metadata_bytes = (ego4d._selection_source(seq_dir / "metadata.json")[0] if catalog_only
                      else (seq_dir / "metadata.json").read_bytes())
    if metadata_bytes is None:
        raise ValueError("metadata Nymeria ausente")
    metadata = json.loads(metadata_bytes.decode("utf-8-sig"))
    if not isinstance(metadata, dict):
        raise ValueError("metadata Nymeria inválido")
    rows, hashes = _annotation_rows(seq_dir, catalog_only=catalog_only)
    digest = rules_digest or _rules_digest()
    evidence_key = (tuple(sorted(hashes.items())), digest)
    windows = library._catalog_windows(rows, names, minimum, maximum, evidence_key,
                                        rules=rules, rivals=rivals)
    assets = library._asset_identity(groups)
    proof = {"schema": 1, "dataset": "nymeria", "algorithm": _PLANNED_ALGORITHM,
             "duration_bounds_s": [minimum, maximum], "rules_sha256": digest,
             "asset_identity": assets, "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
             "annotations_sha256": hashes, "source_time_domain": "DEVICE_TIME",
             "annotation_time_unit": "seconds", "sensor_coverage": "unmeasured"}
    result = []
    for window in windows:
        task_name = window["task_name"]
        rule = (rules[task_name] if rules is not None else selection_rule_for(task_name))
        start_ns, end_ns = (round(value * 1e9) for value in window["device_seconds"])
        uid = f"nymeria-planned:{seq_dir.name}:{start_ns}:{end_ns}"
        carrier = {**copy.deepcopy(proof),
                   "task": {"name": task_name, "id": None, "registry_key": None},
                   "planned_device_window_ns": [start_ns, end_ns]}
        result.append({"clip_uid": uid, "exported_clip_uid": uid,
            "seq_id": seq_dir.name, "uid": str(metadata.get("uid") or seq_dir.name),
            "path": str(seq_dir), "parent_video_uid": seq_dir.name,
            # Annotation-origin coordinates support preview/dedup only.
            # The preparation resolver replaces them with the measured origin.
            "window_s": [(start_ns / 1e9) - rows[0][0], (end_ns / 1e9) - rows[0][0]],
            "planned_device_window_ns": [start_ns, end_ns], "device_window_ns": [start_ns, end_ns],
            "source_clock_domain": "aria_DEVICE_TIME_ns",
            "window_origin_device_timestamp_ns": round(rows[0][0] * 1e9), "dur_s": (end_ns - start_ns) / 1e9,
            "source": "nymeria", "needs_cut": True, "scenario": rule.primary[0],
            "task_name_authoritative": task_name, "selection_evidence": carrier,
            "acquisition_required": True, "selection_ready": False,
            "readiness": "pending_acquisition", "capacity_kind": "annotation_estimate",
            "requires_measured_validation": True})
    return result


@ego4d.selection_boundary
def planned_candidates(*, task_name: str | None = None, task_id: str | None = None,
                       registry_key: str | None = None, min_dur_s: float = 60.0,
                       max_dur_s: float = 1800.0, root: Path | None = None,
                       max_results: int | None = None) -> list[dict[str, Any]]:
    """Return a download queue from atomic annotations, never measured capacity."""
    if not task_name or selection_rule_for(task_name) is None:
        return []
    from . import nymeria_library as library
    name = task_matching.canonical_task_name(task_name)
    minimum, maximum = _bounds(min_dur_s, max_dur_s)
    base = library._root(root)
    operation = ego4d._SELECTION_SNAPSHOT.get()
    source_key = ("nymeria-planned-source", str(base))
    if source_key not in operation:
        sequences = library._load(base)["sequences"]
        rules = selection_rules()
        digest = _rules_digest(rules)
        rivals = {task: task_matching.competing_span_names(task, task_matching.TASK_RULES.items())
                  for task in rules}
        signatures = tuple((sid, tuple(_stat(library._path(base, sid, filename)) for filename in
            ("metadata.json", *("narration/" + value for value in _ANNOTATIONS)))) for sid in sequences)
        identity = json.dumps({sid: library._asset_identity(groups) for sid, groups in sequences.items()},
                              sort_keys=True)
        operation[source_key] = (sequences, rules, digest, rivals, signatures, identity)
    sequences, rules, digest, rivals, signatures, identity = operation[source_key]
    key = (str(base), minimum, maximum, digest, identity, signatures)
    if key in _PLANNED:
        candidates = _PLANNED[key]
    else:
        out = []
        for sequence_index, (sid, groups) in enumerate(sequences.items()):
            report_progress(f"Classificando Nymeria: {sequence_index}/{len(sequences)} sequências concluídas",
                            phase="nymeria_annotations")
            seq_dir = library._path(base, sid)
            try:
                out.extend(_planned_sequence_batch(seq_dir, groups, list(rules), minimum, maximum,
                    rules=rules, rules_digest=digest, rivals=rivals, catalog_only=True))
            except (OSError, ValueError, UnicodeError):
                continue
        candidates = _remember(_PLANNED, key, tuple(sorted(out, key=lambda clip: -clip["dur_s"])))
    out = [bind_task(clip, task_name=name, task_id=task_id, registry_key=registry_key)
           for clip in candidates if clip["task_name_authoritative"] == name]
    return out if max_results is None else out[:max(0, int(max_results))]


def revalidate_planned_candidate(clip: dict[str, Any], *, task_name: str | None = None,
                                task_id: str | None = None, registry_key: str | None = None,
                                root: Path | None = None) -> dict[str, Any]:
    """A plan authorizes acquiring one source; it cannot authorize a send."""
    from . import nymeria_library as library
    carrier = clip.get("selection_evidence")
    if (not isinstance(carrier, dict) or carrier.get("schema") != 1
            or carrier.get("dataset") != "nymeria" or carrier.get("algorithm") != _PLANNED_ALGORITHM
            or clip.get("acquisition_required") is not True
            or carrier.get("sensor_coverage") != "unmeasured"):
        raise ValueError("plano de aquisição Nymeria atual ausente")
    task = carrier.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("name"), str):
        raise ValueError("categoria do plano Nymeria ausente")
    name = task_matching.canonical_task_name(task_name or task["name"])
    if name != task["name"] or selection_rule_for(name) is None:
        raise ValueError("categoria Nymeria diverge do plano")
    for field, expected in (("id", task_id), ("registry_key", registry_key)):
        if expected is not None and task.get(field) != expected:
            raise ValueError("identidade da tarefa Nymeria diverge do plano")
    bounds = carrier.get("duration_bounds_s")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
        raise ValueError("faixa do plano Nymeria inválida")
    minimum, maximum = _bounds(*bounds)
    base = library._root(root)
    sid = clip.get("seq_id")
    groups = library._load(base)["sequences"].get(sid) if isinstance(sid, str) else None
    if groups is None:
        raise ValueError("sequência do plano Nymeria fora do manifesto atual")
    seq_dir = library._path(base, sid)
    if Path(str(clip.get("path") or "")).resolve() != seq_dir:
        raise ValueError("sequência Nymeria fora da biblioteca atual")
    candidates = _planned_sequence(seq_dir, groups, name, minimum, maximum)
    match = next((item for item in candidates if item["clip_uid"] == clip.get("clip_uid")), None)
    if match is None:
        raise ValueError("janela sem evidência atômica atual do plano Nymeria")
    match = bind_task(match, task_name=name, task_id=task.get("id"), registry_key=task.get("registry_key"))
    for field in match:
        if clip.get(field) != match[field]:
            raise ValueError("evidência ou janela do plano Nymeria desatualizada")
    return match


def _resolved_planned_window(snap: dict, planned: dict) -> dict[str, Any]:
    """Reclassify the exact absolute planned cut inside measured source coverage."""
    carrier = planned["selection_evidence"]
    minimum, maximum = _bounds(*carrier["duration_bounds_s"])
    task = carrier["task"]
    left_ns, right_ns = planned["planned_device_window_ns"]
    measured = snap["measured"]
    start, end = ((value - measured["t0_ns"]) / 1e9 for value in (left_ns, right_ns))
    if (left_ns < measured["t0_ns"] or right_ns > measured["t1_ns"]
            or not minimum <= end - start <= maximum
            or not any(a <= start and b >= end for a, b in measured["intervals"])
            or not selection_window_complete(task["name"], snap["rows"], start, end)):
        raise ValueError("janela planejada sem cobertura RGB/IMU e tarefa completa medidas pelo SDK Nymeria")
    # This classifier sees only observations within the requested cut. A
    # wider SDK window cannot lend an absent phase or hide a rival activity.
    selected = [(event, label) for event, label in zip(snap["prepared"], snap["labels"])
                if start <= event[0] <= end]
    rule = selection_rule_for(task["name"])
    rivals = task_matching.competing_span_names(task["name"], task_matching.TASK_RULES.items())
    spans = task_matching.extract_spans(rule, (), min_s=max(minimum, rule.min_span_s or 0),
        max_s=maximum, prepared_events=tuple(event for event, _ in selected),
        event_task_names=tuple(label for _, label in selected), task_name=task["name"],
        competing_task_names=rivals, video_duration_s=end,
        activity_mode=selection_activity_mode(task["name"]))
    span = next((item for item in spans if item["start"] <= start + 1e-6
                 and item["end"] >= end - 1e-6), None)
    if span is None:
        raise ValueError("ações medidas não comprovam toda a janela planejada Nymeria")
    uid = f"nymeria:{snap['seq_id']}:{start:.3f}:{end:.3f}"
    proof = {"schema": 1, "dataset": "nymeria", "algorithm": _ALGORITHM,
             "task": copy.deepcopy(task), "window_s": [start, end],
             "duration_bounds_s": [minimum, maximum], **copy.deepcopy(snap["proof"]),
             "acquisition_plan": copy.deepcopy(planned)}
    return {"clip_uid": uid, "exported_clip_uid": uid, "seq_id": snap["seq_id"],
            "uid": snap["uid"], "path": snap["path"], "window_s": [start, end],
            "device_window_ns": [left_ns, right_ns], "dur_s": end - start,
            "source_clock_domain": "aria_DEVICE_TIME_ns",
            "window_origin_device_timestamp_ns": measured["t0_ns"],
            "source": "nymeria", "needs_cut": True, "parent_video_uid": snap["seq_id"],
            "scenario": rule.primary[0], "task_name_authoritative": task["name"],
            "task_id": task.get("id"), "registry_key": task.get("registry_key"),
            "capacity_kind": "measured_rgb_imu_window", "readiness": "measured",
            "selection_evidence": proof, "action_text": span["action_text"],
            "match_score": span["match_score"],
            "acquired_from_planned_clip_uid": planned["clip_uid"],
            "dedup_clip_uids": [planned["clip_uid"]]}


def resolve_planned_candidate(clip: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    """After acquisition, prove the same complete activity against real SDK clocks."""
    planned = revalidate_planned_candidate(clip, root=root)
    snap = _snapshot(Path(planned["path"]), fresh=True)
    result = _resolved_planned_window(snap, planned)
    # Only this standard measured carrier may reach prepare/upload revalidation.
    result = revalidate_candidate(result, fresh=True, root=root)
    result["acquired_from_planned_clip_uid"] = planned["clip_uid"]
    result["dedup_clip_uids"] = list(dict.fromkeys([planned["clip_uid"],
                                                 *clip.get("dedup_clip_uids", [])]))
    return result


def revalidate_candidate(clip: dict[str, Any], *, task_name: str | None = None,
                         task_id: str | None = None, registry_key: str | None = None,
                         fresh: bool = True, root: Path | None = None) -> dict[str, Any]:
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
    if seq_dir not in sequence_dirs(root):
        raise ValueError("sequência Nymeria fora da biblioteca atual")
    bounds = carrier.get("duration_bounds_s")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
        raise ValueError("faixa da evidência Nymeria inválida")
    minimum, maximum = _bounds(*bounds)
    snap = _snapshot(seq_dir, fresh=fresh)
    acquisition_plan = carrier.get("acquisition_plan")
    if acquisition_plan is not None:
        if not isinstance(acquisition_plan, dict):
            raise ValueError("plano de aquisição da evidência Nymeria inválido")
        planned = revalidate_planned_candidate(acquisition_plan, task_name=name,
            task_id=task.get("id"), registry_key=task.get("registry_key"), root=root)
        match = _resolved_planned_window(snap, planned)
    else:
        candidates = _windows(snap, name, minimum, maximum)
        match = next((item for item in candidates if item["clip_uid"] == clip.get("clip_uid")), None)
    if match is None:
        raise ValueError("janela sem evidência atual da tarefa Nymeria")
    match = bind_task(match, task_name=name, task_id=task.get("id"), registry_key=task.get("registry_key"))
    for field in ("clip_uid", "exported_clip_uid", "seq_id", "parent_video_uid", "window_s", "device_window_ns", "dur_s",
                  "source_clock_domain", "window_origin_device_timestamp_ns", "capacity_kind", "readiness",
                  "selection_evidence"):
        if clip.get(field) != match[field]:
            raise ValueError("evidência ou janela Nymeria desatualizada")
    return match
