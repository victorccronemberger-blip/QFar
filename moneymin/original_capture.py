"""Opt-in delivery of a reviewed original capture, without rewriting its bytes.

Inspection establishes local consistency and declared bindings only. It never
establishes physical acquisition, ownership, attestation or remote acceptance.
One campaign contains one SID, its supplied 0..N-1 chunks, and one owner/task.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

from .capture_import import CaptureDescriptor, CaptureImportError, CaptureLimits, inspect_original_capture
from .campaign_state import campaign_state_lease, campaign_state_operation

_ID = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_NATIVE_WALL = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z')
_MAX_CHUNKS = 64
_GROUP_ARCHIVE_BYTES = 128 * 1024**2
_MESSAGES = {
    'invalid_plan': 'O plano da captura original está inválido; revise a seleção.',
    'source_changed': 'Os arquivos ou metadados originais mudaram; revise a captura antes de continuar.',
    'invalid_capture': 'A captura original não passou na validação local de mídia, ZIP e CSV.',
    'identity_conflict': 'A captura tem identidade, conta, organização ou tarefa conflitante.',
    'incomplete_group': 'Forneça todas as partes da sessão original, de zero até a última parte declarada.',
    'invalid_clock': 'A cronologia original está inválida ou fora dos limites atuais; os horários foram preservados.',
    'policy': 'A captura original não atende aos limites atuais da gravação ou da tarefa.',
    'incompatible_config': 'A captura original exige uma conta, uma tarefa e preservação dos arquivos e da identidade.',
    'existing_review': 'Há uma tentativa ou reserva existente que exige revisão; nenhum novo envio foi criado.',
    'busy': 'Uma operação de captura original já está reservada; preserve a reserva existente.',
    'local_state': 'Não foi possível validar ou persistir a reserva local da captura; preserve os registros para revisão.',
    'preparation_stopped': 'A inspeção da captura foi interrompida antes do envio.',
}


class OriginalCaptureError(ValueError):
    """Public error codes are fixed and contain no original paths or payloads."""

    def __init__(self, code: str):
        self.code = code
        self.review_required = code in {'source_changed', 'existing_review', 'local_state', 'identity_conflict'}
        super().__init__(_MESSAGES[code])


def _fail(code: str):
    raise OriginalCaptureError(code) from None


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError, UnicodeError):
        _fail('invalid_plan')


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _context(account_email, org_key, task_id):
    from .token_store import TokenStoreError, email_key
    try:
        owner = email_key(account_email)
    except TokenStoreError:
        _fail('invalid_plan')
    if any(not isinstance(value, str) or not _ID.fullmatch(value) for value in (org_key, task_id)):
        _fail('invalid_plan')
    return owner, org_key, task_id


def _binding(metadata, keys, expected, *, owner=False):
    observed = []
    for source in (metadata, metadata.get('session', {})):
        for key in keys:
            if key in source:
                value = source[key]
                if not isinstance(value, str) or not value.strip():
                    _fail('identity_conflict')
                observed.append(value.strip().casefold() if owner else value)
    if any(value != expected for value in observed):
        _fail('identity_conflict')
    return 'source' if observed else 'declared'


def _wall_ms(value: Any) -> int:
    if not isinstance(value, str) or not _NATIVE_WALL.fullmatch(value):
        _fail('invalid_clock')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _fail('invalid_clock')
        # Integer arithmetic retains all original decimal text separately.
        delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000
    except (TypeError, ValueError, OverflowError):
        _fail('invalid_clock')


def _read_archive(capture: CaptureDescriptor) -> bytes:
    try:
        with capture.sidecar.path.open('rb') as stream:
            payload = stream.read(capture.sidecar.bytes + 1)
    except OSError:
        _fail('source_changed')
    if len(payload) != capture.sidecar.bytes or hashlib.sha256(payload).hexdigest() != capture.sidecar.sha256:
        _fail('source_changed')
    return payload


def _capture_signature(capture: CaptureDescriptor):
    return {'media': [str(capture.media.path), capture.media.bytes, capture.media.sha256],
            'sidecar': [str(capture.sidecar.path), capture.sidecar.bytes, capture.sidecar.sha256],
            'members': [[member.name, member.bytes, member.sha256] for member in capture.members],
            'metadata': capture.metadata, 'imu_rows': capture.imu_rows, 'frames_rows': capture.frames_rows}


@dataclass(frozen=True)
class OriginalCapturePlan:
    account_email: str
    org_key: str
    task_id: str
    session_id: str
    captures: tuple[CaptureDescriptor, ...]
    recorded_at: tuple[str, ...]
    durations_ms: tuple[int, ...]
    digest: str
    content_digest: str
    owner_binding: str
    org_binding: str
    task_binding: str
    completeness_binding: str
    source_chunk_count_verified: bool
    physical_provenance_verified: bool = False

    def public_summary(self):
        return {'source_mode': 'original', 'plan_digest': self.digest,
                'content_digest': self.content_digest, 'session_id': self.session_id,
                'chunks': len(self.captures), 'duration_ms': sum(self.durations_ms),
                'owner_binding': self.owner_binding, 'org_binding': self.org_binding,
                'task_binding': self.task_binding, 'completeness_binding': self.completeness_binding,
                'source_chunk_count_verified': self.source_chunk_count_verified,
                'physical_provenance_verified': False, 'provider_acceptance_verified': False,
                'media_probe_verified': True, 'csv_temporal_consistency_verified': True,
                'clock_domains_preserved': True, 'cross_clock_conversion_verified': False,
                'files': [{'media': {'bytes': c.media.bytes, 'sha256': c.media.sha256},
                           'sidecar': {'bytes': c.sidecar.bytes, 'sha256': c.sidecar.sha256},
                           'log_id': c.metadata['logId'], 'chunk_index': i,
                           'recorded_at': self.recorded_at[i], 'duration_ms': self.durations_ms[i]}
                          for i, c in enumerate(self.captures)]}


@dataclass(frozen=True)
class OriginalCaptureInspection:
    """Organization-independent first phase; never a delivery authorization."""
    account_email: str
    task_id: str
    session_id: str
    captures: tuple[CaptureDescriptor, ...]
    recorded_at: tuple[str, ...]
    durations_ms: tuple[int, ...]
    declared_org_key: str | None
    owner_binding: str
    task_binding: str
    completeness_binding: str
    source_chunk_count_verified: bool
    physical_provenance_verified: bool = False

    def public_summary(self):
        return {'source_mode': 'original', 'session_id': self.session_id,
                'chunks': len(self.captures), 'duration_ms': sum(self.durations_ms),
                'owner_binding': self.owner_binding, 'task_binding': self.task_binding,
                'completeness_binding': self.completeness_binding,
                'source_chunk_count_verified': self.source_chunk_count_verified,
                'organization_binding_pending': True,
                'physical_provenance_verified': False, 'provider_acceptance_verified': False,
                'media_probe_verified': True, 'csv_temporal_consistency_verified': True,
                'clock_domains_preserved': True, 'cross_clock_conversion_verified': False}


def inspect_original_capture_group(file_pairs, *, account_email: str, task_id: str,
                                   expected_chunk_count=None, probe=None, now=None, limits=None) -> OriginalCaptureInspection:
    """Validate every local invariant before discovering/authenticating an org."""
    return _prepare_group(file_pairs, account_email=account_email, org_key=None, task_id=task_id,
                          expected_chunk_count=expected_chunk_count, probe=probe, now=now, limits=limits)


def prepare_original_capture_plan(file_pairs, *, account_email: str, org_key: str, task_id: str,
                                  expected_chunk_count: int | None = None,
                                  probe: Callable | None = None, now: float | None = None,
                                  limits: dict[str, int] | None = None,
                                  should_stop: Callable[[], bool] | None = None) -> OriginalCapturePlan:
    """Pure preflight of explicitly supplied files; no auth, journal or writes."""
    _context(account_email, org_key, task_id)
    return _prepare_group(file_pairs, account_email=account_email, org_key=org_key, task_id=task_id,
                          expected_chunk_count=expected_chunk_count, probe=probe, now=now, limits=limits, should_stop=should_stop)


def _prepare_group(file_pairs, *, account_email, org_key, task_id,
                   expected_chunk_count=None, probe=None, now=None, limits=None, should_stop=None):
    from . import config, sidecar, upload
    # An unbound first-phase inspection has no placeholder organization, plan
    # digest or upload authorization. The final public preparer rejects None.
    from .token_store import TokenStoreError, email_key
    try:
        owner = email_key(account_email)
    except TokenStoreError:
        _fail('invalid_plan')
    if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
        _fail('invalid_plan')
    org, task = org_key, task_id
    if (not isinstance(file_pairs, (list, tuple)) or not 1 <= len(file_pairs) <= _MAX_CHUNKS
            or any(not isinstance(pair, (list, tuple)) or len(pair) != 2 for pair in file_pairs)):
        _fail('invalid_plan')
    if expected_chunk_count is not None and (type(expected_chunk_count) is not int or expected_chunk_count != len(file_pairs)):
        _fail('incomplete_group')
    effective = config.recording_limits() if limits is None else limits
    if (not isinstance(effective, dict) or any(type(effective.get(key)) is not int or effective[key] <= 0
            for key in ('min_duration_ms', 'max_duration_ms', 'backlog_cap_ms'))
            or effective['max_duration_ms'] < effective['min_duration_ms']):
        _fail('invalid_plan')
    current = time.time() if now is None else now
    if type(current) not in (int, float) or not math.isfinite(current):
        _fail('invalid_plan')
    captures, recorded, durations, source_counts = [], [], [], []
    bindings = {'owner': [], 'org': [], 'task': []}
    sid = None
    declared_orgs = set()
    identity_meta = None
    previous = None
    total_archive = 0
    seen_paths = set()
    for index, (media, archive) in enumerate(file_pairs):
        if should_stop and should_stop():
            _fail('preparation_stopped')
        try:
            capture = inspect_original_capture(media, archive, limits=CaptureLimits())
        except CaptureImportError as exc:
            _fail('source_changed' if exc.code == 'source_changed' else 'invalid_capture')
        total_archive += capture.sidecar.bytes
        if total_archive > _GROUP_ARCHIVE_BYTES:
            _fail('invalid_plan')
        paths = (capture.media.path, capture.sidecar.path)
        if any(path in seen_paths for path in paths):
            _fail('incomplete_group')
        seen_paths.update(paths)
        meta = capture.metadata
        # These original source fields also drive the short POST. No profile,
        # current config default or captured historical network is inserted.
        source_platform = meta.get('platform', {})
        source_device = meta.get('device', {})
        cameras = meta.get('cameras')
        if (not isinstance(source_device, dict) or not isinstance(source_device.get('model'), str) or not source_device['model'].strip()
                or not isinstance(source_platform, dict) or source_platform.get('os', source_platform.get('type')) != 'android'
                or not isinstance(meta.get('appVersion'), str) or not meta['appVersion'].strip()
                or not isinstance(cameras, list) or not cameras
                or any(not isinstance(camera, dict) or camera.get('source') not in {'builtin', 'built-in', 'external'}
                       for camera in cameras)):
            _fail('invalid_capture')
        observed_identity = _canonical([source_device, source_platform, meta['appVersion']])
        if identity_meta is not None and observed_identity != identity_meta:
            _fail('identity_conflict')
        identity_meta = observed_identity
        current_sid = meta['session']['id']
        if sid is None:
            sid = current_sid
        if current_sid != sid or meta['chunk']['index'] != index:
            _fail('incomplete_group')
        duration = meta.get('durationMs')
        if type(duration) is not int or not effective['min_duration_ms'] <= duration <= effective['max_duration_ms']:
            _fail('policy')
        try:
            observed = (probe or sidecar.probe_video)(capture.media.path)
        except Exception:
            _fail('invalid_capture')
        if (not isinstance(observed, dict) or observed.get('has_video') is not True
                or type(observed.get('duration_ms')) is not int or observed['duration_ms'] <= 0
                or abs(observed['duration_ms'] - duration) > max(500, int(duration * .01))):
            _fail('invalid_capture')
        try:
            validated = upload._validate_sidecar_zip(_read_archive(capture), log_id=f'{sid}_{index}', duration_ms=observed['duration_ms'])
        except upload.UploadError:
            _fail('invalid_capture')
        if _canonical(validated) != _canonical(meta):
            _fail('source_changed')
        for key in ('width', 'height'):
            if type(observed.get(key)) is int and observed[key] > 0 and meta['video'][key] != observed[key]:
                _fail('invalid_capture')
        rec = meta.get('createdAt')
        wall = _wall_ms(rec)
        clock = meta['timebase']
        if clock['clockDomain'] not in {'android_elapsedRealtimeNanos', 'trinet_camera_monotonic'}:
            _fail('invalid_clock')
        start_ns, end_ns = int(clock['startNs']), int(clock['endNs'])
        start_wall, end_wall = meta['chunk']['startTimeMs'], meta['chunk']['endTimeMs']
        if end_ns <= start_ns or end_wall < start_wall:
            _fail('invalid_clock')
        if previous is not None:
            prev_wall, prev_duration, prev_clock, prev_ns, prev_chunk_wall = previous
            if (wall < prev_wall + prev_duration or clock['clockDomain'] != prev_clock
                    or start_ns < prev_ns or start_wall < prev_chunk_wall):
                _fail('invalid_clock')
        previous = (wall, duration, clock['clockDomain'], end_ns, end_wall)
        bindings['owner'].append(_binding(meta, ('account_email', 'accountEmail', 'ownerEmail'), owner, owner=True))
        org_keys = ('org_key', 'orgKey', 'organizationResourceKey')
        declared = [section[key] for section in (meta, meta['session']) for key in org_keys if key in section]
        if any(not isinstance(value, str) or not _ID.fullmatch(value) for value in declared):
            _fail('identity_conflict')
        declared_orgs.update(declared)
        if len(declared_orgs) > 1:
            _fail('identity_conflict')
        bindings['org'].append(_binding(meta, org_keys, org) if org is not None else ('source' if declared else 'declared'))
        bindings['task'].append(_binding(meta, ('task_id', 'taskId'), task))
        for source, keys in ((meta, ('expected_chunk_count', 'expectedChunkCount', 'chunkCount', 'totalChunks')),
                             (meta['session'], ('chunkCount', 'expectedChunkCount', 'totalChunks')),
                             (meta['chunk'], ('totalChunks', 'total'))):
            for key in keys:
                if key in source:
                    count = source[key]
                    if type(count) is not int or count != len(file_pairs):
                        _fail('incomplete_group')
                    source_counts.append(count)
        captures.append(capture)
        recorded.append(rec)
        durations.append(duration)
        if should_stop and should_stop():
            _fail('preparation_stopped')
    now_ms = int(current * 1000)
    if _wall_ms(recorded[0]) < now_ms - effective['backlog_cap_ms'] or _wall_ms(recorded[-1]) + durations[-1] > now_ms + 5000:
        _fail('invalid_clock')
    payload = {'session_id': sid, 'captures': [{'media_sha256': c.media.sha256, 'media_bytes': c.media.bytes,
                'sidecar_sha256': c.sidecar.sha256, 'sidecar_bytes': c.sidecar.bytes} for c in captures]}
    content_digest = _sha(payload)
    owner_binding, org_binding, task_binding = ('source' if all(value == 'source' for value in bindings[key]) else 'declared'
                                               for key in ('owner', 'org', 'task'))
    complete = 'source' if source_counts else 'declared'
    if org is None:
        return OriginalCaptureInspection(owner, task, sid, tuple(captures), tuple(recorded), tuple(durations),
                                          next(iter(declared_orgs), None), owner_binding, task_binding, complete, bool(source_counts))
    digest = _sha({**payload, 'account_email': owner, 'org_key': org, 'task_id': task,
                   'owner_binding': owner_binding, 'org_binding': org_binding, 'task_binding': task_binding,
                   'completeness_binding': complete})
    return OriginalCapturePlan(owner, org, task, sid, tuple(captures), tuple(recorded), tuple(durations),
                               digest, content_digest, owner_binding, org_binding, task_binding,
                               complete, bool(source_counts))


def verify_original_capture_plan(plan: OriginalCapturePlan, *, account_email=None, org_key=None, task_id=None,
                                 probe=None, now=None, limits=None, should_stop=None) -> OriginalCapturePlan:
    """Re-read all sources; a frozen dataclass does not freeze nested metadata."""
    if type(plan) is not OriginalCapturePlan or not plan.captures or type(plan.physical_provenance_verified) is not bool or plan.physical_provenance_verified:
        _fail('invalid_plan')
    context = _context(plan.account_email if account_email is None else account_email,
                       plan.org_key if org_key is None else org_key, plan.task_id if task_id is None else task_id)
    if context != (plan.account_email, plan.org_key, plan.task_id):
        _fail('identity_conflict')
    try:
        pairs = [(c.media.path, c.sidecar.path) for c in plan.captures]
        expected = [_capture_signature(c) for c in plan.captures]
    except (AttributeError, TypeError):
        _fail('invalid_plan')
    fresh = prepare_original_capture_plan(pairs, account_email=context[0], org_key=context[1], task_id=context[2],
                  expected_chunk_count=len(pairs), probe=probe, now=now, limits=limits, should_stop=should_stop)
    if (_canonical(expected) != _canonical([_capture_signature(c) for c in fresh.captures])
            or _canonical({k: v for k, v in plan.__dict__.items() if k != 'captures'}) !=
               _canonical({k: v for k, v in fresh.__dict__.items() if k != 'captures'})):
        _fail('source_changed')
    return fresh


def _read_journals():
    """Read both roots without invoking migration, mkdir or token discovery."""
    from . import config, upload
    rows = []
    roots = {config.DATA_DIR / 'sidecars', config.MEDIA_DATA_DIR / 'sidecars'}
    try:
        for directory in roots:
            if not directory.exists():
                continue
            for path in sorted(directory.iterdir()):
                if path.name.lower().endswith('.json'):
                    row = upload._read_sidecar_file(path)
                    if not isinstance(row, dict):
                        _fail('existing_review')
                    rows.append((row, directory != config.DATA_DIR / 'sidecars'))
    except (OSError, ValueError, upload.UploadError):
        _fail('existing_review')
    return rows


def _known_group(plan, registry_key):
    from . import upload
    from .upload_types import journal_delivery_confirmed
    rows = []
    for row, legacy in _read_journals():
        context = row.get('campaign_context')
        original_hash = context.get('original_content_digest') if isinstance(context, dict) else None
        source_hashes = {c.media.sha256 for c in plan.captures} | {c.sidecar.sha256 for c in plan.captures}
        collision = (row.get('session_id') == plan.session_id or original_hash == plan.content_digest
                     or any(isinstance(row.get(key), str) and row[key] in source_hashes
                            for key in ('sidecar_sha256', 'video_sha256', 'original_media_sha256', 'original_sidecar_sha256')))
        if not collision:
            continue
        if legacy or row.get('session_id') != plan.session_id:
            _fail('existing_review')
        rows.append(row)
    if not rows:
        return [], 'fresh'
    if len(rows) != len(plan.captures) or {row.get('chunk_index') for row in rows if type(row.get('chunk_index')) is int} != set(range(len(plan.captures))):
        _fail('existing_review')
    expected_context = {'registry_key': registry_key, 'clip_uid': plan.session_id, 'task_id': plan.task_id,
                        'source_mode': 'original', 'original_capture_digest': plan.digest,
                        'original_content_digest': plan.content_digest}
    for row in rows:
        index = row['chunk_index']
        c = plan.captures[index]
        context = row.get('campaign_context')
        if (row.get('account_email') != plan.account_email or row.get('org_key') != plan.org_key
                or row.get('task_id') != plan.task_id or row.get('log_id') != f'{plan.session_id}_{index}'
                or type(row.get('expected_chunk_count')) is not int or row['expected_chunk_count'] != len(plan.captures)
                or row.get('recorded_at') != plan.recorded_at[index] or row.get('duration_ms') != plan.durations_ms[index]
                or type(row.get('size_bytes')) is not int or row['size_bytes'] != c.media.bytes
                or row.get('sidecar_sha256') != c.sidecar.sha256
                or row.get('original_media_sha256') != c.media.sha256 or row.get('original_sidecar_sha256') != c.sidecar.sha256
                or not isinstance(context, dict) or any(context.get(key) != value for key, value in expected_context.items())):
            _fail('existing_review')
        if journal_delivery_confirmed(row):
            continue
        if row.get('state') not in upload.TRANSIENT_STATES | {upload.STATE_LOSS, upload.STATE_DONE}:
            _fail('existing_review')
        try:
            if (row.get('transport_artifact') == 'sidecar' and row.get('phase') in
                    {'registered', 'sas', 'sas_ready', 'sas_reminted', 'transport', 'sidecar_preflight'}):
                # The normal helper resolves the journal directory and may
                # migrate library state. Pre-auth classification must be pure.
                _validate_zip_receipt_before_auth(row, c)
                stage = 'sidecar'
            else:
                stage = upload._pending_recovery_stage(row)
        except upload.UploadError:
            _fail('existing_review')
        if stage not in {'sidecar', 'complete', 'finalize'} or row.get('crash_resumes', 0) >= upload.MAX_CRASH_RESUMES:
            _fail('existing_review')
    return rows, 'confirmed' if all(journal_delivery_confirmed(row) for row in rows) else 'resume'


def _validate_zip_receipt_before_auth(row, capture):
    from . import config, upload
    from .upload_types import journal_flags_valid
    from .device_profile import recorded_at_to_wall_ms
    if (not journal_flags_valid(row) or upload._journal_creation_uncertain(row)
            or row.get('conflict_action') != 'complete' or row.get('register_first') is not True
            or any(type(row.get(key, 0)) is not int or row.get(key, 0) < 0 for key in ('attempts', 'crash_resumes'))
            or recorded_at_to_wall_ms(row.get('recorded_at')) is None
            or type(row.get('sidecar_size_bytes')) is not int or row['sidecar_size_bytes'] != capture.sidecar.bytes):
        _fail('existing_review')
    try:
        upload._response_upload_id({'id': row.get('upload_id')})
        expected = config.DATA_DIR / 'sidecars' / upload._sidecar_filename(row['session_id'], row['chunk_index'])
        expected = expected.with_suffix('.data.zip').resolve()
        if not isinstance(row.get('sidecar_data_path'), str) or Path(row['sidecar_data_path']).resolve() != expected:
            _fail('existing_review')
        with expected.open('rb') as stream:
            payload = stream.read(capture.sidecar.bytes + 1)
        if len(payload) != capture.sidecar.bytes or hashlib.sha256(payload).hexdigest() != capture.sidecar.sha256:
            _fail('existing_review')
        upload._validate_sidecar_zip(payload, log_id=row['log_id'], duration_ms=row['duration_ms'])
    except (OSError, ValueError, upload.UploadError):
        _fail('existing_review')


def _reservation(plan):
    return {'account_email': plan.account_email, 'org_key': plan.org_key, 'task_id': plan.task_id,
            'session_id': plan.session_id, 'digest': plan.digest, 'content_digest': plan.content_digest,
            'media_sha256': [c.media.sha256 for c in plan.captures],
            'sidecar_sha256': [c.sidecar.sha256 for c in plan.captures]}


def _read_reservations():
    from . import config
    from .atomic_io import load_json_state
    try:
        value = load_json_state(config.DATA_DIR / 'original_capture_reservations.json', {'version': 1, 'bindings': {}})
        if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1
                or not isinstance(value.get('bindings'), dict)):
            _fail('local_state')
        for sid, row in value['bindings'].items():
            if (not isinstance(sid, str) or not _ID.fullmatch(sid) or not isinstance(row, dict)
                    or row.get('session_id') != sid or row.get('phase') not in {'reserved', 'creation_attempted', 'confirmed'}
                    or any(not isinstance(row.get(key), str) or not _HASH.fullmatch(row[key]) for key in ('digest', 'content_digest'))
                    or any(not isinstance(row.get(key), list) or not row[key] or any(not isinstance(v, str) or not _HASH.fullmatch(v) for v in row[key])
                           for key in ('media_sha256', 'sidecar_sha256'))
                    or len(row['media_sha256']) != len(row['sidecar_sha256']) or len(row['media_sha256']) > _MAX_CHUNKS):
                _fail('local_state')
            _context(row.get('account_email'), row.get('org_key'), row.get('task_id'))
    except (OSError, ValueError):
        _fail('local_state')
    return value


def _check_reservation(plan, store, mode):
    expected = _reservation(plan)
    hashes = set(expected['media_sha256'] + expected['sidecar_sha256'])
    for sid, row in store['bindings'].items():
        if sid == plan.session_id or row['content_digest'] == plan.content_digest or hashes.intersection(row['media_sha256'] + row['sidecar_sha256']):
            if sid != plan.session_id or any(row.get(key) != value for key, value in expected.items()):
                _fail('existing_review')
            if row['phase'] != 'reserved' and mode == 'fresh':
                _fail('existing_review')


@campaign_state_operation
def _persist_reservation(plan, store, phase):
    from . import config
    from .atomic_io import save_json
    from .media_lifecycle import media_state_lease
    store['bindings'][plan.session_id] = {**_reservation(plan), 'phase': phase}
    try:
        with media_state_lease(wait=True):
            save_json(config.DATA_DIR / 'original_capture_reservations.json', store)
    except (OSError, ValueError):
        _fail('local_state')


@contextmanager
def _exclusive_operation():
    from . import config
    from .operation_lease import operation_lease, OperationLeaseError
    try:
        with campaign_state_lease(), operation_lease(config.DATA_DIR / '.original_capture.lock'):
            yield
    except OperationLeaseError:
        _fail('busy')


def _result(plan, rows, mode):
    from .upload_types import journal_delivery_confirmed
    return {'email': plan.account_email, 'org_key': plan.org_key, 'session_id': plan.session_id,
            'ok': bool(rows) and all(journal_delivery_confirmed(row) for row in rows),
            'finalized': bool(rows) and all(journal_delivery_confirmed(row) for row in rows),
            'uploads': [row['upload_id'] for row in sorted(rows, key=lambda r: r['chunk_index'])],
            'recovered': True, 'skipped': mode == 'confirmed',
            'evidence_source': 'existing_original_journal'}


def validate_original_capture_session_policy(plan: OriginalCapturePlan, session) -> None:
    """Apply the authentic session's existing write guard before journal intent.

    Local structural inspection cannot authorize a camera, organization, geo,
    version or provider policy. This helper never supplies a missing observation.
    """
    if type(plan) is not OriginalCapturePlan:
        _fail('invalid_plan')
    owner = _context(getattr(session, 'email', None), plan.org_key, plan.task_id)[0]
    if owner != plan.account_email:
        _fail('identity_conflict')
    guard = getattr(session, '_check_write_policy', None)
    if not callable(guard):
        _fail('policy')
    for index, capture in enumerate(plan.captures):
        meta = dict(capture.metadata)
        meta.update(chunk_index=index, size_bytes=capture.media.bytes,
                    device={'model': meta['device']['model']},
                    platform={'os': meta['platform'].get('os', meta['platform'].get('type'))})
        guard('POST', f'/api/v1/uploads?org_key={plan.org_key}',
              {'session_id': plan.session_id, 'log_id': meta['logId'], 'task_id': plan.task_id,
               'duration_ms': plan.durations_ms[index], 'recorded_at': plan.recorded_at[index], 'meta': meta})


@campaign_state_operation
def run_original_capture_campaign(cfg, log=None, *, progress=None, should_stop=None):
    """Use the normal campaign event contract and drain an active upload on Stop."""
    from . import campaign, config, sent_registry, upload
    from .campaign_types import CampaignLog
    from .minute_api import Session
    if (len(cfg.accounts) != 1 or len(cfg.tasks) != 1 or type(cfg.tasks[0].count) is not int or cfg.tasks[0].count != 1
            or cfg.unique_video is not False or cfg.realistic_timeline is not False or cfg.cleanup_after_upload is not False
            or cfg.share_clips is not False or cfg.target_hours_per_account != 0 or cfg.candidate_plan is not None
            or type(cfg.evaluate) is not bool or type(cfg.finalize) is not bool):
        _fail('incompatible_config')
    account, task = cfg.accounts[0], cfg.tasks[0]
    if should_stop and should_stop():
        # A request already stopped must not inspect a potentially large group.
        owner = _context(account.email, account.org_key, task.task_id)[0]
        early_log = log or CampaignLog(campaign._recorded_at_now(), [owner])
        early_log.start_request_id = cfg.start_request_id
        early_log.status = 'stopped'
        early_log.save()
        if progress:
            progress('campaign_stopped', {'reason': 'parada pelo usuário'})
        return early_log
    try:
        plan = verify_original_capture_plan(cfg.original_capture_plan, account_email=account.email,
                                            org_key=account.org_key, task_id=task.task_id, should_stop=should_stop)
    except OriginalCaptureError as exc:
        if exc.code != 'preparation_stopped':
            raise
        early_log = log or CampaignLog(campaign._recorded_at_now(), [_context(account.email, account.org_key, task.task_id)[0]])
        early_log.start_request_id = cfg.start_request_id
        early_log.status = 'stopped'
        early_log.save()
        if progress:
            progress('campaign_stopped', {'reason': 'parada pelo usuário'})
        return early_log
    if (type(task.min_dur_s) not in (int, float) or type(task.max_dur_s) not in (int, float)
            or not math.isfinite(task.min_dur_s) or not math.isfinite(task.max_dur_s)
            or not task.min_dur_s <= sum(plan.durations_ms) / 1000 <= task.max_dur_s):
        _fail('policy')
    log = log or CampaignLog(campaign._recorded_at_now(), [plan.account_email])
    log.start_request_id = cfg.start_request_id
    if log.items or log.accounts != [plan.account_email]:
        _fail('incompatible_config')
    def emit(kind, **payload):
        if progress:
            progress(kind, payload)
    def stopped():
        return bool(should_stop and should_stop())
    def finish(stop=False):
        log.status = 'stopped' if stop else ('done' if log.items and log.items[0]['accounts'][0].get('ok') else 'error')
        log.save()
        attempts = log.items[0]['accounts'] if log.items else []
        emit('campaign_done', log_path=log._path.name, status=log.status,
             ok_sends=sum(bool(a.get('ok') and not a.get('skipped')) for a in attempts),
             failed_sends=sum(not a.get('ok') for a in attempts),
             skipped_sends=sum(bool(a.get('skipped')) for a in attempts), issues=log.issues)
        if stop:
            emit('campaign_stopped', reason='parada pelo usuário')
        return log
    if stopped():
        # Stop before auth/reservation never creates a transport journal.
        return finish(True)
    rows, mode = _known_group(plan, task.registry_key)
    _check_reservation(plan, _read_reservations(), mode)
    try:
        with _exclusive_operation():
            try:
                plan = verify_original_capture_plan(plan, account_email=account.email, org_key=account.org_key, task_id=task.task_id,
                                                     should_stop=should_stop)
            except OriginalCaptureError as exc:
                if exc.code != 'preparation_stopped':
                    raise
                return finish(True)
            rows, mode = _known_group(plan, task.registry_key)
            store = _read_reservations()
            _check_reservation(plan, store, mode)
            if stopped():
                return finish(True)
            _persist_reservation(plan, store, 'confirmed' if mode == 'confirmed' else 'creation_attempted' if mode == 'resume' else 'reserved')
            log.save()
            emit('campaign_start', accounts=[plan.account_email], tasks=[task.task_name or task.scenario],
                 dataset='original_capture', content_mode='original', cleanup_after_upload=False)
            emit('task_start', task_id=task.task_id, scenario=task.scenario, task_name=task.task_label or task.task_name or task.scenario, count=1)
            item = {'clip_uid': plan.session_id, 'registry_key': task.registry_key, 'task_id': task.task_id,
                    'task_name': task.task_name, 'task_scenario': task.scenario, 'source': 'original_capture',
                    'duration_ms': sum(plan.durations_ms), 'source_provenance': plan.public_summary(), 'accounts': []}
            log.add_item(item)
            emit('clip_ready', clip_uid=plan.session_id, duration_ms=item['duration_ms'],
                 source_provenance=item['source_provenance'], imu_real=False)
            if mode == 'confirmed':
                result = _result(plan, rows, mode)
            else:
                if stopped():
                    log.items.clear()
                    return finish(True)
                # Loading saved tokens must not refresh before owner validation.
                session = Session.from_email(plan.account_email, live=False)
                if _context(getattr(session, 'email', None), plan.org_key, plan.task_id)[0] != plan.account_email:
                    _fail('identity_conflict')
                session.ensure_auth(org_key=plan.org_key)
                limits = session.recording_policy.limits() if getattr(session, 'recording_policy', None) is not None else config.recording_limits()
                try:
                    plan = verify_original_capture_plan(plan, account_email=session.email, org_key=account.org_key,
                                                         task_id=task.task_id, limits=limits, should_stop=should_stop)
                except OriginalCaptureError as exc:
                    if exc.code != 'preparation_stopped':
                        raise
                    log.items.clear()
                    return finish(True)
                validate_original_capture_session_policy(plan, session)
                if stopped():
                    log.items.clear()
                    return finish(True)
                def account_progress(phase, state, attempt, **details):
                    # The active protocol must drain; checkpoint still handles
                    # Pause, and Stop releases Pause in CampaignRunner.
                    stopped()
                    public_details = {}
                    artifact = details.get('artifact')
                    if type(artifact) is str and artifact == 'sidecar':
                        public_details['artifact'] = artifact
                    for key in ('sent_bytes', 'total_bytes', 'percent', 'speed_bps', 'eta_s'):
                        value = details.get(key)
                        if key == 'eta_s' and key in details and value is None:
                            public_details[key] = None
                            continue
                        expected_types = (int,) if key in ('sent_bytes', 'total_bytes') else (int, float)
                        if type(value) not in expected_types:
                            continue
                        try:
                            if not math.isfinite(value) or value < 0 or (key == 'percent' and value > 100):
                                continue
                        except OverflowError:
                            continue
                        public_details[key] = value
                    emit('account_progress', clip_uid=plan.session_id, email=plan.account_email,
                         phase=phase, state=state, attempt=attempt, **public_details)
                emit('account_start', clip_uid=plan.session_id, email=plan.account_email, task=task.scenario)
                context = {'registry_key': task.registry_key, 'clip_uid': plan.session_id, 'task_id': plan.task_id,
                           'source_mode': 'original', 'original_capture_digest': plan.digest,
                           'original_content_digest': plan.content_digest}
                if mode == 'fresh':
                    context['history_name'] = log._path.name
                # Revalidate receipts again after auth, before any create. Never
                # turn an intervening known receipt into a fresh upload.
                new_rows, new_mode = _known_group(plan, task.registry_key)
                if new_mode != mode or _canonical(new_rows) != _canonical(rows):
                    _fail('existing_review')
                if mode == 'resume':
                    upload.pump_pending(session, account_email=plan.account_email, required_org_key=plan.org_key,
                                        session_ids={plan.session_id}, on_progress=account_progress, timeout_blob=cfg.timeout_blob)
                    rows, after_mode = _known_group(plan, task.registry_key)
                    result = _result(plan, rows, 'resume')
                    if after_mode != 'confirmed':
                        result.update(ok=False, finalized=False, error='A retomada original permanece pendente; preserve os recibos.')
                else:
                    archives = [_read_archive(c) for c in plan.captures]
                    _persist_reservation(plan, store, 'creation_attempted')
                    res = upload.upload_session(session, [c.media.path for c in plan.captures], plan.org_key,
                          task_id=plan.task_id, session_id=plan.session_id, recorded_at=list(plan.recorded_at),
                          sidecar=True, sidecar_data=archives, normalize=False, register_first=True,
                          persist_sidecar=True, profile=None, ego_meta=None, chunk_index_start=0,
                          evaluate=cfg.evaluate, finalize=cfg.finalize, timeout_blob=cfg.timeout_blob,
                          suppress_per_chunk_catbear=True, original_captures=list(plan.captures),
                          on_progress=account_progress, campaign_context=context)
                    quality_fails = [check.get('id', '?') for c in res.chunks for check in (c.evaluate_result or {}).get('checks', [])
                                     if isinstance(check, dict) and check.get('status') == 'fail']
                    valid = (res.session_id == plan.session_id and res.org_key == plan.org_key and res.task_id == plan.task_id
                             and len(res.chunks) == len(plan.captures)
                             and all(type(c.chunk_index) is int and c.chunk_index == i and c.log_id == f'{plan.session_id}_{i}'
                                     and isinstance(c.upload_id, str) and c.upload_id.strip() and c.state == 'done'
                                     and c.duration_ms == plan.durations_ms[i] for i, c in enumerate(res.chunks))
                             and res.finalized is True and not quality_fails)
                    result = {'email': plan.account_email, 'org_key': plan.org_key, 'session_id': res.session_id,
                              'ok': bool(valid), 'finalized': res.finalized, 'finalize_status': res.finalize_status,
                              'uploads': [c.upload_id for c in res.chunks], 'evaluate_fails': quality_fails,
                              'source_mode': 'original'}
                    if not valid:
                        result['error'] = 'Entrega original sem confirmação integral; preserve os recibos para recuperação.'
            item['accounts'].append(result)
            # Publish immutable attempt before index/ACK. Confirmed outcomes
            # remain attached even if an observer or index later fails.
            log.save()
            publication_error = None
            if result.get('ok') and not result.get('skipped'):
                _persist_reservation(plan, store, 'confirmed')
                try:
                    sent_registry.mark_sent(task.registry_key, plan.session_id, plan.account_email)
                    campaign._acknowledge_campaign_upload(plan.session_id)
                except Exception as exc:
                    publication_error = exc
            emit('account_done', **result, clip_uid=plan.session_id, registry_key=task.registry_key, task=task.scenario,
                 credited_seconds=item['duration_ms'] / 1000 if result.get('ok') and not result.get('skipped') else 0)
            emit('item_done', clip_uid=plan.session_id, accounts=item['accounts'])
            if publication_error:
                raise publication_error
            return finish(stopped())
    except Exception as exc:
        try:
            exc._moneymin_campaign_log = log
        except Exception:
            pass
        log.status = 'error'
        # Parser/OS/provider exception text may contain source paths or tokens.
        log.issues.append({'kind': 'original_capture_error', 'error': 'A captura original foi preservada; revise a tentativa e os recibos.'})
        try:
            log.save()
        except OSError:
            pass
        raise


__all__ = ['OriginalCaptureError', 'OriginalCaptureInspection', 'OriginalCapturePlan',
           'inspect_original_capture_group', 'prepare_original_capture_plan',
           'verify_original_capture_plan', 'validate_original_capture_session_policy',
           'run_original_capture_campaign']
