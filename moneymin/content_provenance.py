"""Byte-bound local dataset lineage for Ego4D prepare/upload audit.

Records fingerprints and IMU observation stats. Does not gate wire delivery —
Minute 1.29 accepts the sidecar envelope; campaign quality gates (real IMU,
measured PTS, auth) live in ``campaign.upload_to_account``.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
from typing import Any

INTEGRITY_ERROR = 'Conteúdo ou proveniência alterados; novo preparo necessário.'
# Kept for older tests/callers; delivery is no longer blocked by this module.
DATASET_POLICY_ERROR = INTEGRITY_ERROR
# EgoImu.SAMPLING_PERIOD_US = 2000 → 2_000_000 ns (smali Y / q0.1).
ANDROID_IMU_GRID_STEP_NS = 2_000_000


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def text_binding(value: str) -> dict[str, Any]:
    if type(value) is not str:
        raise ValueError(INTEGRITY_ERROR)
    raw = value.encode('utf-8')
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def bytes_binding(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes:
        raise ValueError(INTEGRITY_ERROR)
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def fingerprint(path: str | Path) -> dict[str, Any]:
    """Hash exactly read bytes and reject observed replacement during the read."""
    path = Path(path)
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    digest, size = hashlib.sha256(), 0
    try:
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block); size += len(block)
            after = os.fstat(stream.fileno())
        if size <= 0 or size != before.st_size or identity(before) != identity(after) or identity(after) != identity(path.stat()):
            raise ValueError(INTEGRITY_ERROR)
    except OSError as exc:
        raise ValueError(INTEGRITY_ERROR) from exc
    return {'path': str(path.resolve()), 'bytes': size, 'sha256': digest.hexdigest()}


def public_binding(row: dict[str, Any]) -> dict[str, Any]:
    return {'name': Path(row['path']).name, 'bytes': row['bytes'], 'sha256': row['sha256']}


def verify_input(row: dict[str, Any]) -> None:
    actual = fingerprint(row['path'])
    if (actual['bytes'], actual['sha256']) != (row['bytes'], row['sha256']):
        raise ValueError(INTEGRITY_ERROR)


def _window(window: Any) -> tuple[float, float]:
    if not isinstance(window, (tuple, list)) or len(window) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in window):
        raise ValueError(INTEGRITY_ERROR)
    start, end = map(float, window)
    if start < 0 or end <= start:
        raise ValueError(INTEGRITY_ERROR)
    return start, end


def observe_derived_imu(path: str | Path, window_s: tuple[float, float], output_csv: str) -> dict[str, Any]:
    """Actual source timestamp intervals/output rows, not native event metrics."""
    start, end = _window(window_s)
    expected = fingerprint(path)
    timestamps: list[set[int]] = [set(), set()]
    valid = [0, 0]
    rows = invalid_timestamp = outside = 0
    consumed = hashlib.sha256()
    with Path(path).open('rb') as stream:
        def lines():
            first = True
            for raw in stream:
                consumed.update(raw)
                yield raw.decode('utf-8-sig' if first else 'utf-8')
                first = False
        reader = csv.DictReader(lines())
        required = {'canonical_timestamp_ms', 'gyro_x', 'gyro_y', 'gyro_z', 'accl_x', 'accl_y', 'accl_z'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(INTEGRITY_ERROR)
        for row in reader:
            rows += 1
            try:
                timestamp = float(row['canonical_timestamp_ms'])
            except (ValueError, TypeError):
                invalid_timestamp += 1; continue
            if not math.isfinite(timestamp):
                invalid_timestamp += 1; continue
            if not start * 1000 <= timestamp <= end * 1000:
                outside += 1; continue
            for component, names in enumerate([('gyro_x', 'gyro_y', 'gyro_z'), ('accl_x', 'accl_y', 'accl_z')]):
                try:
                    numbers = [float(row[name]) for name in names]
                except (ValueError, TypeError):
                    continue
                if all(math.isfinite(v) for v in numbers):
                    valid[component] += 1
                    timestamps[component].add(round(timestamp * 1000000))
    verify_input(expected)
    if consumed.hexdigest() != expected['sha256']:
        raise ValueError(INTEGRITY_ERROR)
    count, first, last, step, uniform = 0, None, None, None, True
    for row in csv.DictReader(io.StringIO(output_csv)):
        if set(row) != {'t', 'ax', 'ay', 'az', 'wx', 'wy', 'wz'}:
            raise ValueError(INTEGRITY_ERROR)
        try:
            timestamp = int(row['t'])
            if timestamp < 0 or not all(math.isfinite(float(row[key])) for key in ['ax', 'ay', 'az', 'wx', 'wy', 'wz']):
                raise ValueError(INTEGRITY_ERROR)
        except (ValueError, TypeError) as exc:
            raise ValueError(INTEGRITY_ERROR) from exc
        if last is not None:
            delta = timestamp - last
            if delta <= 0: raise ValueError(INTEGRITY_ERROR)
            if step is None: step = delta
            elif delta != step: uniform = False
        if first is None: first = timestamp
        last, count = timestamp, count + 1
    if not count:
        raise ValueError(INTEGRITY_ERROR)
    def component(index):
        ordered = sorted(timestamps[index])
        intervals = [b - a for a, b in zip(ordered, ordered[1:])]
        return {'valid_rows': valid[index], 'distinct_recorded_timestamps': len(ordered),
                'max_recorded_interval_ns': max(intervals) if intervals else None}
    return {'schema': 1, 'kind': 'local_derived_dataset_observation',
        'source_clock': 'ego4d_canonical_timestamp_ms', 'output_clock': 'local_relative_csv_ns',
        'physical_provenance_verified': False, 'native_equivalence_verified': False,
        'source': {'rows_read': rows, 'invalid_timestamp_rows': invalid_timestamp, 'outside_declared_window_rows': outside,
                   'gyro': component(0), 'accel': component(1), 'sha256': expected['sha256']},
        'output': {'sample_count': count, 'first_timestamp_ns': first, 'last_timestamp_ns': last,
                   'uniform_grid_step_ns': step if uniform else None,
                   **text_binding(output_csv)},
        'native_reference_configuration': {'strategy': 'gyro_anchored_v1', 'nearestFallbackToleranceNs': '1000000',
                   'maxInterpolationSpanNs': '25000000', 'qualification': 'Static APK configuration, not applied or measured by this resampler.'},
        'native_metrics': {'status': 'unknown', 'droppedRowCount': None, 'interpolatedCount': None,
                   'nearestFallbackCount': None, 'maxAlignmentDeltaNs': None, 'p95AlignmentDeltaNs': None,
                   'maxInterpolationSpanNs': None,
                   'qualification': 'The reference span is configuration above; actual native event/counter semantics are unavailable for derived CSV.'}}


def prepare_content_provenance(source_video: str | Path, source_imu: str | Path, prepared_video: str | Path,
        imu_csv: str, frames_csv: str, *, clip_uid: str, parent_video_uid: str, media_uid: str,
        window_s: tuple[float, float], media_offset_s: float, normalization_start_s: float | None,
        selection_evidence: dict[str, Any] | None) -> dict[str, Any]:
    start, end = _window(window_s)
    if type(media_offset_s) not in (int, float) or not math.isfinite(media_offset_s) or media_offset_s < 0:
        raise ValueError(INTEGRITY_ERROR)
    if normalization_start_s is not None and (type(normalization_start_s) not in (int, float)
            or not math.isfinite(normalization_start_s) or normalization_start_s < 0):
        raise ValueError(INTEGRITY_ERROR)
    if any(type(value) is not str or not value.strip() for value in [clip_uid, parent_video_uid, media_uid]):
        raise ValueError(INTEGRITY_ERROR)
    inputs = {'source_video': fingerprint(source_video), 'source_imu': fingerprint(source_imu),
              'prepared_video': fingerprint(prepared_video)}
    diagnostics = observe_derived_imu(source_imu, (start, end), imu_csv)
    for row in inputs.values(): verify_input(row)
    public = {'schema': 1, 'dataset': 'ego4d', 'recording_origin': 'third_party_dataset',
        'physical_provenance_verified': False, 'receiver_derived_data_support_verified': False,
        'clip_uid': clip_uid, 'parent_video_uid': parent_video_uid, 'media_uid': media_uid,
        'window_s': [start, end], 'media_time_offset_s': float(media_offset_s),
        'transform': {'kind': 'local_window_and_encoding', 'media_seek_s': normalization_start_s,
                      'imu': 'canonical_source_bucket_resampling', 'frames_at_prepare': 'legacy_placeholder_not_acquired'},
        'assets': {**{key: public_binding(row) for key, row in inputs.items()},
                   'imu_csv': text_binding(imu_csv), 'prepared_frames_csv': text_binding(frames_csv)},
        'selection_evidence': copy.deepcopy(selection_evidence), 'derived_diagnostics': diagnostics}
    marker_path = Path(prepared_video).with_name(Path(prepared_video).name + '.source.json')
    if marker_path.exists():
        marker_binding = fingerprint(marker_path)
        if marker_binding['bytes'] > 65536: raise ValueError(INTEGRITY_ERROR)
        with marker_path.open('rb') as stream:
            raw = stream.read(marker_binding['bytes'] + 1)
        if bytes_binding(raw) != {'bytes': marker_binding['bytes'], 'sha256': marker_binding['sha256']}:
            raise ValueError(INTEGRITY_ERROR)
        marker = json.loads(raw)
        if (marker.get('source_sha256'), marker.get('prepared_sha256')) != (inputs['source_video']['sha256'], inputs['prepared_video']['sha256']):
            raise ValueError(INTEGRITY_ERROR)
        inputs['normalization_marker'] = marker_binding
        public['assets']['normalization_marker'] = public_binding(marker_binding)
        public['transform']['cache_identity'] = {key: marker.get(key) for key in ['version', 'start_s', 'dur_s', 'width', 'height', 'fps']}
        public['transform']['encoder_command_sha256'] = None
    else:
        public['transform']['cache_identity'] = None
        public['transform']['encoder_command_sha256'] = None
    public['lineage_sha256'] = canonical_digest(public)
    return {'content_provenance': public, 'derived_diagnostics': diagnostics,
            '_content_inputs': inputs, 'imu_csv': imu_csv, 'frames_csv': frames_csv}


def revalidate_content_provenance(item: dict[str, Any]) -> dict[str, Any]:
    try:
        public = item['content_provenance']
        body = {key: value for key, value in public.items() if key != 'lineage_sha256'}
        if type(public['schema']) is not int or public['schema'] != 1 or canonical_digest(body) != public['lineage_sha256']:
            raise ValueError(INTEGRITY_ERROR)
        for role in ['source_video', 'source_imu', 'prepared_video']:
            row = item['_content_inputs'][role]
            verify_input(row)
            if public_binding(row) != public['assets'][role]: raise ValueError(INTEGRITY_ERROR)
        if 'normalization_marker' in item['_content_inputs']:
            marker = item['_content_inputs']['normalization_marker']
            verify_input(marker)
            if public_binding(marker) != public['assets']['normalization_marker']: raise ValueError(INTEGRITY_ERROR)
        if text_binding(item['imu_csv']) != public['assets']['imu_csv'] or text_binding(item['frames_csv']) != public['assets']['prepared_frames_csv']:
            raise ValueError(INTEGRITY_ERROR)
        if item['derived_diagnostics'] != public['derived_diagnostics']:
            raise ValueError(INTEGRITY_ERROR)
        return copy.deepcopy(public)
    except (KeyError, TypeError, OSError) as exc:
        raise ValueError(INTEGRITY_ERROR) from exc


def require_dataset_native_delivery_support(diagnostics: Any = None) -> None:
    """No-op: production Ego4D delivery is gated by campaign/upload wire checks.

    Retained so older call sites and tests keep importing a stable name.
    ``diagnostics`` is ignored.
    """
    return None


def bind_content_delivery(item: dict[str, Any], session_id: str, task_id: str, org_key: str,
                          chunks: list[dict[str, Any]]) -> dict[str, Any]:
    public = revalidate_content_provenance(item)
    if any(type(value) is not str or not value.strip() for value in [session_id, task_id, org_key]) or not chunks:
        raise ValueError(INTEGRITY_ERROR)
    rows = []
    for index, chunk in enumerate(chunks):
        if type(chunk.get('index')) is not int or chunk['index'] != index or type(chunk.get('start_ms')) is not int or chunk['start_ms'] < 0 or type(chunk.get('duration_ms')) is not int or chunk['duration_ms'] <= 0:
            raise ValueError(INTEGRITY_ERROR)
        rows.append({'index': index, 'start_ms': chunk['start_ms'], 'duration_ms': chunk['duration_ms'],
            'video': public_binding(fingerprint(chunk['video_path'])), 'imu_csv': text_binding(chunk['imu_csv']),
            'frames_csv': text_binding(chunk['frames_csv']), 'sidecar_zip': bytes_binding(chunk['sidecar_bytes']),
            'frame_clock_grade': 'file_extracted_pts_with_local_envelope_offset_not_physical_clock',
            'recorded_at': chunk.get('recorded_at'), 'clock_origin': 'locally_assigned_not_source_acquisition'})
    result = {'schema': 1, 'content': public, 'session_id': session_id, 'task_id': task_id, 'org_key': org_key,
              'chunks': rows, 'confirmed_delivery': False, 'physical_provenance_verified': False}
    result['delivery_binding_sha256'] = canonical_digest(result)
    return result
