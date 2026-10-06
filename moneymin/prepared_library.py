"""Read-only inventory of locally prepared Ego4D media.

``ready`` describes a source/native content-bound cache and a structurally
valid local sensor file. It is not task approval, sensor-window validation,
an account/device profile, or an upload ZIP. Campaign effects retain their
own fresh validation. A snapshot is an observation, never a cleanup permit.

The web loader may run the first scan in its worker. Subsequent polls read
small JSON markers and file metadata, not MP4 bytes. Explicit refresh and a
five-minute snapshot lifetime renew hashes, including same-stat replacements.
No catalog installation, journal migration, lease, or account lookup occurs.
"""
from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from . import config
from .atomic_io import decode_json_state
from .campaign_history import is_campaign_history_name

_TTL_S = 300.0
_STATES = {'ready', 'partial', 'missing', 'stale'}
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_WINDOW = re.compile(r'([0-9a-fA-F-]{36})_([0-9]+(?:\.[0-9]+)?)_([0-9]+(?:\.[0-9]+)?)\Z')
_UID_QUANTIZATION_S = .000501
_MARKER_KEYS = {'version', 'source_size', 'source_mtime_ns', 'source_sha256',
                'start_s', 'dur_s', 'width', 'height', 'fps', 'prepared_size',
                'prepared_sha256'}
_LOCK = threading.RLock()
_SNAPSHOTS = {}


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _number(value):
    try:
        return (float(value) if type(value) in (int, float) and math.isfinite(value)
                else None)
    except OverflowError:
        return None


def _uuid(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _confined(path, root):
    try:
        return not path.is_symlink() and path.resolve().parent == root
    except OSError:
        return False


def _stat(path):
    try:
        stamp = path.stat()
        return [stamp.st_dev, stamp.st_ino, stamp.st_size,
                stamp.st_mtime_ns, stamp.st_ctime_ns]
    except FileNotFoundError:
        return None


def _protection_files():
    paths = [config.DATA_DIR / name for name in (
        'original_capture_reservations.json', 'start_requests.json',
        'campaign_start_requests.json')]
    for base in {config.DATA_DIR, config.MEDIA_DATA_DIR}:
        if base.is_dir():
            paths.extend(p for p in base.glob('campaign_*.json') if is_campaign_history_name(p.name))
    for base in {config.DATA_DIR / 'sidecars', config.MEDIA_DATA_DIR / 'sidecars'}:
        if base.is_dir() and _confined(base, base.parent.resolve()):
            paths.extend(base.glob('*.json'))
        elif base.exists():
            paths.append(base)
    return sorted(set(paths))


def _small_signature(paths):
    result = []
    for path in paths:
        try:
            stamp = _stat(path)
            # These are authoritative small JSON documents, never media or tokens.
            digest = (_digest(path.read_bytes()) if stamp is not None and path.is_file()
                      and not path.is_symlink() else None)
            result.append([str(path), stamp, digest])
        except OSError:
            result.append([str(path), 'unreadable', None])
    return result


def inventory_signature(root):
    """Stable physical loader key; no media reads, writes, or provider calls.

    Source catalogs are stat-bound until the next snapshot renewal. Small markers
    and protection JSON are content-bound on every poll. Effects must not use
    this observation as current content authorization. Time is deliberately
    absent: a long hash scan remains the same loader job across TTL boundaries.
    A web loader should measure its cache TTL after the worker completes.
    """
    root = Path(root).resolve()
    entries, markers = [], []
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if path.name.startswith('holoassist_'):
                continue
            if (path.suffix.lower() == '.mp4' or path.name.endswith('_imu.csv')
                    or path.name.endswith('.mp4.source.json')
                    or path.name in {'ego4d.json', 'clips.csv', 'library.sqlite3'}):
                entries.append([path.name, path.is_symlink(), _stat(path)])
                if path.name.endswith('.mp4.source.json') and _confined(path, root):
                    markers.append(path)
    signature = [str(root), str(config.DATA_DIR.resolve()), entries,
                 _small_signature(markers), _small_signature(_protection_files())]
    return _digest(json.dumps(signature, sort_keys=True).encode())


def _protection():
    """Use existing decoders directly, bypassing migration and write leases."""
    from . import campaign_start_store, original_capture, recovery, upload
    from .campaign_evidence import publication_index, publication_registered
    from .content_provenance import canonical_digest
    from .upload_types import journal_delivery_confirmed, journal_flags_valid

    paths, hashes, referenced = {}, {}, set()

    def protect_path(value, reason):
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError('invalid media path')
        path = Path(value).resolve()
        paths.setdefault(os.path.normcase(str(path)), set()).add(reason)
        referenced.add(path)

    def protect_hash(value, reason):
        if not isinstance(value, str) or not _HASH.fullmatch(value):
            raise ValueError('invalid media digest')
        hashes.setdefault(value, set()).add(reason)

    try:
        local = config.DATA_DIR / 'sidecars'
        legacy = config.MEDIA_DATA_DIR / 'sidecars'
        for directory in (local, legacy):
            if directory.exists() and (not directory.is_dir()
                                       or not _confined(directory, directory.parent.resolve())):
                raise ValueError('invalid journal directory')
        consulted = _protection_files()
        if any(path.is_symlink() for path in consulted):
            raise ValueError('unsafe protection document')
        before = _small_signature(consulted)
        # Shared legacy journals cannot be assigned to this installation without
        # consulting tokens/migrating. Their presence is ambiguous, never empty.
        if legacy.resolve() != local.resolve() and legacy.is_dir():
            if any(path.suffix.lower() == '.json' for path in legacy.iterdir()):
                raise ValueError('unmigrated legacy journals')
        groups = recovery._groups(directory=local, include_reconciled=True) if local.is_dir() else []
        publications = None
        for rows in groups:
            if any(not journal_flags_valid(row) for row in rows):
                raise ValueError('invalid journal flags')
            releasable = recovery._complete_chunk_group(rows) and all(
                journal_delivery_confirmed(row) and row.get('campaign_reconciled') is True
                for row in rows)
            if releasable:
                if publications is None:
                    publications = publication_index()
                releasable = publication_registered(rows, publications)
            if releasable:
                continue
            for row in rows:
                context = row.get('campaign_context')
                lineage = context.get('content_provenance') if isinstance(context, dict) else None
                if lineage is not None:
                    if not isinstance(lineage, dict) or not isinstance(lineage.get('content'), dict):
                        raise ValueError('invalid content binding')
                    if canonical_digest({k: v for k, v in lineage.items()
                                         if k != 'delivery_binding_sha256'}) != lineage['delivery_binding_sha256']:
                        raise ValueError('invalid content binding')
                    assets = lineage['content'].get('assets')
                    if not isinstance(assets, dict) or any(not isinstance(asset, dict) for asset in assets.values()):
                        raise ValueError('invalid content assets')
                    for asset in assets.values():
                        protect_hash(asset['sha256'], 'pending_journal')
                for key in ('video_path', 'sidecar_data_path'):
                    if row.get(key) is not None:
                        protect_path(row[key], 'pending_journal')
        for row in original_capture._read_reservations()['bindings'].values():
            for digest in row['media_sha256'] + row['sidecar_sha256']:
                protect_hash(digest, 'original_reservation')
        for row in campaign_start_store._read()['requests'].values():
            for asset in row['protected_assets']:
                protect_path(asset['path'], 'start_request')
                protect_hash(asset['sha256'], 'start_request')
        if before != _small_signature(_protection_files()):
            raise ValueError('changing protection state')
        return {'status': 'verified', 'paths': paths, 'sha256': hashes,
                'referenced': referenced}
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return {'status': 'unknown', 'paths': {}, 'sha256': {}, 'referenced': set()}


def _catalog(root):
    """Only local identity/window metadata; never action ranking or installation."""
    videos, clips = {}, {}
    try:
        path = root / 'ego4d.json'
        if not _confined(path, root) or not path.is_file():
            return videos, clips
        metadata = decode_json_state(path.read_bytes())
        for row in metadata.get('videos', []):
            uid = _uuid(row.get('video_uid')) if isinstance(row, dict) else None
            duration = _number(row.get('duration_sec')) if isinstance(row, dict) else None
            if uid and duration is not None and duration > 0:
                videos[uid] = duration
        path = root / 'clips.csv'
        if not _confined(path, root) or not path.is_file():
            return videos, clips
        with path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                uid, parent = _uuid(row.get('exported_clip_uid')), _uuid(row.get('parent_video_uid'))
                try:
                    start, end = float(row['parent_start_sec']), float(row['parent_end_sec'])
                except (ValueError, TypeError, KeyError):
                    continue
                if uid and parent in videos and math.isfinite(start + end) and 0 <= start < end <= videos[parent] + .001:
                    clips[uid] = (parent, start, end)
    except (OSError, ValueError, TypeError, KeyError):
        return {}, {}
    return videos, clips


def _identity(uid, videos, clips):
    if uid in clips:
        return clips[uid], 'catalog_clip'
    match = _WINDOW.fullmatch(uid)
    if match:
        parent, start, end = _uuid(match[1]), float(match[2]), float(match[3])
        if parent in videos and math.isfinite(start + end) and 0 <= start < end <= videos[parent] + .001:
            return (parent, start, end), 'canonical_window'
    return None, 'unresolved'


def _valid_marker(saved):
    if type(saved) is not dict or set(saved) != _MARKER_KEYS:
        return False
    if any(type(saved.get(key)) is not int for key in
           ('version', 'source_size', 'source_mtime_ns', 'width', 'height', 'fps', 'prepared_size')):
        return False
    start, duration = saved['start_s'], _number(saved['dur_s'])
    return (saved['version'] == 5 and saved['width'] == 1440 and saved['height'] == 1080
            and saved['fps'] == 30 and saved['source_size'] > 1024 * 1024
            and saved['prepared_size'] > 1024 * 1024
            and duration is not None and duration > 0
            and (start is None or (_number(start) is not None and start >= 0))
            and all(isinstance(saved[key], str) and _HASH.fullmatch(saved[key])
                    for key in ('source_sha256', 'prepared_sha256')))


def _file(path, root):
    safe = _confined(path, root)
    present = safe and path.is_file()
    return {'name': path.name, 'path': str(path) if safe else None,
            'bytes': path.stat().st_size if present else 0, 'present': present}


def _scan(root):
    from . import ego4d
    from .campaign import _native_cache_fingerprint

    videos, clips = _catalog(root)
    protection = _protection()
    files = {p.name: p for p in root.iterdir()} if root.is_dir() else {}
    names = {name for name in files if name.endswith('_native.mp4') and not name.startswith('holoassist_')}
    names.update(name.removesuffix('.source.json') for name in files
                 if name.endswith('_native.mp4.source.json') and not name.startswith('holoassist_'))
    names.update(path.name for path in protection['referenced']
                 if _confined(path, root) and path.name.endswith('_native.mp4'))
    fingerprints, sensor_validity = {}, {}

    def fingerprint(path):
        if path not in fingerprints:
            fingerprints[path] = _native_cache_fingerprint(path)
        return fingerprints[path]

    items = []
    for name in sorted(names):
        native = root / name
        marker = native.with_name(name + '.source.json')
        uid = name.removesuffix('_native.mp4')
        identity, identity_status = _identity(uid, videos, clips)
        parent, start, end = identity or (None, None, None)
        saved, reasons, source, source_display = None, [], None, None
        if identity:
            source_display = root / ((uid if identity_status == 'catalog_clip' else parent) + '.mp4')
        marker_present = _confined(marker, root) and marker.is_file()
        try:
            if marker_present:
                saved = decode_json_state(marker.read_bytes())
        except (OSError, ValueError):
            reasons.append('marker_unreadable')
        valid_marker = _valid_marker(saved)
        native_info = _file(native, root)
        sensor = root / (parent + '_imu.csv') if parent else None
        sensor_info = _file(sensor, root) if sensor else None
        duration = end - start if identity else (_number(saved.get('dur_s')) if isinstance(saved, dict) else None)
        cache_state, binding = 'stale', 'unverified'
        if native.is_symlink():
            reasons.append('unsafe_native_path')
        elif not native_info['present']:
            cache_state = 'missing'
            reasons.append('native_missing')
        elif not _confined(native, root):
            reasons.append('unsafe_native_path')
        elif not valid_marker:
            reasons.append('marker_missing_or_unsupported')
        elif identity is None:
            reasons.append('identity_unresolved')
        elif abs(saved['dur_s'] - duration) > .001:
            reasons.append('window_marker_mismatch')
        else:
            candidates = []
            # Only an original parent or a catalog clip containing the exact
            # canonical window can be the source. Do not guess from arbitrary
            # MP4 names or identify clips merely by byte count.
            source_windows = [(parent, 0.0)]
            source_windows.extend((clip_uid, clip_start) for clip_uid, (p, clip_start, clip_end) in clips.items()
                                  if p == parent and clip_start <= start + .001 and end <= clip_end + .001)
            if identity_status == 'catalog_clip':
                source_windows.insert(0, (uid, start))
            for media_uid, offset in source_windows:
                expected_start = None if media_uid == uid and identity_status == 'catalog_clip' else round(start - offset, 6)
                same_start = (type(saved['start_s']) is type(expected_start)
                              and saved['start_s'] == expected_start)
                if (identity_status == 'canonical_window'
                        and type(saved['start_s']) is float and type(expected_start) is float):
                    # Ego4D window UIDs round endpoints to milliseconds;
                    # normalization markers retain six decimal places. Admit
                    # only this quantization, checking BOTH canonical ends.
                    actual_start = saved['start_s'] + offset
                    same_start = (abs(actual_start - start) <= _UID_QUANTIZATION_S
                                  and abs(actual_start + saved['dur_s'] - end) <= _UID_QUANTIZATION_S)
                if not same_start:
                    continue
                path = root / (media_uid + '.mp4')
                if _confined(path, root) and path.is_file():
                    source_display = path
                    stamp = path.stat()
                    if stamp.st_size == saved['source_size'] and stamp.st_mtime_ns == saved['source_mtime_ns']:
                        candidates.append(path)
            try:
                for path in dict.fromkeys(candidates):
                    if fingerprint(path) == (saved['source_size'], saved['source_sha256']):
                        source = path
                        source_display = path
                        break
                prepared_ok = fingerprint(native) == (saved['prepared_size'], saved['prepared_sha256'])
                if not prepared_ok:
                    reasons.append('native_digest_mismatch')
                elif source is None:
                    # A disappeared source is partial; contradictory extant
                    # bytes/window metadata are stale, never ready.
                    expected_names = {media_uid + '.mp4' for media_uid, _ in source_windows}
                    source_exists = any(_confined(root / n, root) and (root / n).is_file() for n in expected_names)
                    cache_state = 'stale' if source_exists else 'partial'
                    reasons.append('source_binding_mismatch' if source_exists else 'source_missing')
                else:
                    if sensor not in sensor_validity:
                        sensor_validity[sensor] = bool(sensor_info['present'] and ego4d._valid_imu_cache(sensor))
                    if sensor_validity[sensor]:
                        cache_state, binding = 'ready', 'verified'
                    else:
                        cache_state, binding = 'partial', 'source_native_verified'
                        reasons.append('sensor_missing_or_invalid')
            except (OSError, ValueError, RuntimeError):
                reasons.append('asset_unreadable_or_changed')
        source_info = _file(source_display, root) if source_display else None
        if source_info:
            source_info['binding_status'] = 'verified' if source else 'unverified'
        protection_reasons = set()
        # A shared original sensor can be reserved by another prepared clip.
        # Its schema/header establishes presence only, never its content hash.
        if protection['sha256'] and sensor_info and sensor_info['present']:
            try:
                fingerprint(sensor)
            except (OSError, ValueError, RuntimeError):
                protection_reasons.add('digest_protection_unverified')
        for asset in (native, source_display, sensor):
            if asset is not None:
                protection_reasons.update(protection['paths'].get(os.path.normcase(str(asset.resolve())), set()))
                if asset in fingerprints:
                    protection_reasons.update(protection['sha256'].get(fingerprints[asset][1], set()))
        # The prepared digest is usable for protection only after hashing the
        # actual file; a stale/forged marker cannot confer digest ownership.
        if protection['status'] == 'unknown':
            protection_reasons.add('protection_state_unknown')
        elif protection['sha256'] and any(
                asset_info and asset_info['present'] and asset not in fingerprints
                for asset, asset_info in ((native, native_info), (source_display, source_info),
                                         (sensor, sensor_info))):
            # Ambiguous old media must not be declared unprotected merely
            # because its own marker did not admit a content verification.
            protection_reasons.add('digest_protection_unverified')
        asset_names = {a['name']: a['bytes'] for a in (native_info, source_info, sensor_info) if a}
        if marker_present:
            asset_names[marker.name] = marker.stat().st_size
        items.append({'clip_uid': uid, 'parent_video_uid': parent,
                      'window_s': [start, end] if identity else None,
                      'duration_s': duration, 'duration_ms': round(duration * 1000) if duration is not None else None,
                      'source_duration_s': videos.get(parent), 'prepared_duration_ms': None,
                      'duration_basis': 'selected_window' if identity else 'marker_window',
                      'identity_status': identity_status, 'task_name': None,
                      'source': source_info, 'native': native_info, 'imu': sensor_info,
                      'marker': {'name': marker.name, 'present': marker_present,
                                 'version': saved.get('version') if isinstance(saved, dict) else None,
                                 'binding_status': binding},
                      'cache_state': cache_state, 'protected': bool(protection_reasons),
                      'protection_reasons': sorted(protection_reasons),
                      'local_bytes': sum(asset_names.values()), 'reasons': reasons})
    physical = {}
    for item in items:
        for asset in (item['source'], item['native'], item['imu']):
            if asset and asset['present']:
                physical[asset['name']] = asset['bytes']
        if item['marker']['present']:
            marker = root / item['marker']['name']
            physical[marker.name] = marker.stat().st_size
    return {'schema': 1, 'provider': 'ego4d', 'library_root': str(root),
            'inventory_scope': 'local_prepared_media', 'state': 'ready',
            'verified_at': time.time(), 'refreshing': False,
            'cache_validation': 'source_native_digest_and_sensor_structure',
            'task_approval': 'not_evaluated', 'zip_readiness': 'not_evaluated',
            'protection_status': protection['status'], 'local_bytes': sum(physical.values()),
            'physical_file_count': len(physical), 'items': items}


def list_prepared_clips(root, query='', *, state='all', minimum_s=0,
                        maximum_s=None, limit=50, offset=0, force_refresh=False):
    """List an observed local cache snapshot; no media or state is modified.

    ``counts`` describes physical cache states before pagination/filtering;
    ``protected`` is an additional overlay count. ``duration_ms`` is the
    requested canonical window, not a new ffprobe measurement. Paths are
    confined to the supplied library root. Unresolved fields remain null.
    """
    if (not isinstance(query, str) or len(query) > 200 or state not in _STATES | {'all'}
            or _number(minimum_s) is None or minimum_s < 0
            or (maximum_s is not None and (_number(maximum_s) is None or maximum_s < minimum_s))
            or type(limit) is not int or not 1 <= limit <= 100
            or type(offset) is not int or not 0 <= offset <= 100000
            or type(force_refresh) is not bool):
        raise ValueError('Filtros de acervo inválidos.')
    root = Path(root).resolve()
    signature = inventory_signature(root)
    key = (str(root), str(config.DATA_DIR.resolve()), str(config.MEDIA_DATA_DIR.resolve()))
    with _LOCK:
        cached = _SNAPSHOTS.get(key)
        now = time.monotonic()
        if (force_refresh or cached is None or cached[0] != signature
                or now - cached[2] >= _TTL_S):
            snapshot = _scan(root)
            if inventory_signature(root) != signature:
                raise ValueError('Acervo mudou durante a leitura; atualize a consulta.')
            generation = _digest(json.dumps(snapshot['items'], sort_keys=True).encode())
            snapshot['generation'] = generation
            # Freshness begins after verification, not before a potentially
            # multi-minute scan. Stable input is independent of elapsed time.
            _SNAPSHOTS[key] = (signature, snapshot, time.monotonic())
        payload = deepcopy(_SNAPSHOTS[key][1])
    items = payload.pop('items')
    payload['counts'] = {name: sum(item['cache_state'] == name for item in items) for name in sorted(_STATES)}
    payload['counts']['protected'] = sum(item['protected'] for item in items)
    payload['physical_clip_count'] = len(items)
    terms = query.casefold().split()
    selected = []
    for item in items:
        duration = item['duration_s']
        searchable = ' '.join(str(item.get(key) or '') for key in ('clip_uid', 'parent_video_uid'))
        searchable += ' ' + ' '.join(asset['name'] for asset in (item['source'], item['native'], item['imu']) if asset)
        if (state != 'all' and item['cache_state'] != state) or not all(term in searchable.casefold() for term in terms):
            continue
        if duration is None:
            if minimum_s or maximum_s is not None:
                continue
        elif duration < minimum_s or (maximum_s is not None and duration > maximum_s):
            continue
        selected.append(item)
    payload.update(total=len(selected), offset=offset, limit=limit,
                   items=selected[offset:offset + limit])
    return payload


_LOCAL_PROVIDERS = {'ego4d', 'holoassist', 'nymeria', 'local'}
_LOCAL_KINDS = {'video', 'sensor', 'sidecar', 'catalog', 'derivative'}
_MEDIA_TREES = {'ego4d', 'holoassist', 'nymeria', 'videos', 'manifests', 'original', 'sidecars'}
_PRIVATE_TREES = {
    'secrets', 'secret', 'accounts', 'account', 'tokens', 'credentials',
    'credential_store', 'token_store', 'device_state', 'device_profiles',
    'device_anchors', 'state', 'stores', 'store', 'config', 'configs', '__pycache__',
}
_VIDEO_SUFFIXES = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.vrs'}
_ORIGINAL_CATALOGS = {
    'ego4d.json', 'clips.csv', 'clip_narrations.json', 'timed_narrations.jsonl',
    'annotations.trainval.v1_1.json', 'data-splits-v1_2.zip',
    'video.index.json', 'video_compress.index.json', 'imu.index.json',
}


def _reparse(path, info=None):
    """Windows junctions/reparse directories are not ordinary directories."""
    info = info if info is not None else path.lstat()
    return (path.is_symlink()
            or bool(getattr(info, 'st_file_attributes', 0)
                    & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)))


def _local_provider(relative):
    first, name = relative.parts[0].casefold(), relative.name.casefold()
    if name.startswith('holoassist_') and (first == 'ego4d' or len(relative.parts) == 1):
        return 'holoassist'
    return first if first in _LOCAL_PROVIDERS - {'local'} else 'local'


def _private_name(name):
    return name.startswith(('token_', 'credential', 'account_', 'device_profile',
                            'campaign_', 'start_requests', 'original_capture_reservations',
                            'firebase', '.env', 'webui_prefs', 'config.', 'settings.'))


def _local_kind(relative, provider):
    name = relative.name.casefold()
    if any(part.casefold() == 'sidecars' for part in relative.parts[:-1]):
        # Journal JSON is operational state. Only archive presence is inventoried;
        # archive contents/ZIP validators are deliberately never opened here.
        return 'sidecar' if name.endswith('.zip') else None
    if any(name.endswith(suffix) or name.endswith(suffix + '.part') for suffix in _VIDEO_SUFFIXES):
        return 'video'
    if name == 'imu.csv' or name.endswith('_imu.csv') or name.endswith('_imu.csv.part'):
        return 'sensor'
    if _private_name(name):
        return None
    if name in _ORIGINAL_CATALOGS or (provider == 'nymeria' and name == 'metadata.json'):
        return 'catalog'
    if (name in {'frames.csv', 'metadata.json', 'library.sqlite3'}
            or name.endswith(('.frames.csv', '.metadata.json', '.source.json', '.source.json.tmp',
                              '.mp4.ok', '.chunk.ok'))):
        return 'derivative'
    # In a known media tree, raw streams/unfinished downloads and auxiliary
    # formats remain visible without guessing their task or upload validity.
    return 'derivative' if len(relative.parts) > 1 and relative.parts[0].casefold() in _MEDIA_TREES else None


def _local_stage(name, kind):
    if kind == 'sensor':
        return 'sensores'
    if kind != 'video':
        return 'arquivo auxiliar'
    if re.search(r'_native(?:_acc[0-9a-f]+)?(?:_part\d+_\d+_\d+)?\.mp4\Z', name.casefold()):
        return 'normalizado'
    return 'baixado/original'


def list_local_media(root, query='', *, provider='all', kind='all', limit=50, offset=0):
    """Inventory local media files by presence/stat, independent of campaigns.

    Only known media trees and recognizable media at the root are enumerated.
    Symlinks, Windows junctions/reparse entries and private state stores are
    pruned before recursion. Hardlinks are represented once, in stable path
    order, so physical files/bytes are not multiplied. This function does not
    read media bytes, infer tasks, validate archives, or make device profiles.

    ``file_count``, ``total_bytes`` and ``by_provider`` describe the complete
    physical inventory before filters. ``total`` describes the filtered rows.
    ``modified_at`` and ``verified_at`` are Unix seconds. Stage is a physical
    file role; ``normalizado`` based on a known native name is not approval.
    Protected digests stay conservative without hashing media in this view.
    """
    if (not isinstance(query, str) or len(query) > 200
            or not isinstance(provider, str) or provider not in _LOCAL_PROVIDERS | {'all'}
            or not isinstance(kind, str) or kind not in _LOCAL_KINDS | {'all'}
            or type(limit) is not int or not 1 <= limit <= 100
            or type(offset) is not int or not 0 <= offset <= 100000):
        raise ValueError('Filtros de armazenamento local inválidos.')
    supplied = Path(root)
    if supplied.exists() and (_reparse(supplied) or not supplied.is_dir()):
        raise ValueError('Raiz de armazenamento local inválida.')
    root = supplied.resolve()
    protection = _protection()
    items, seen_paths, seen_files, errors = [], set(), {}, 0
    pending = [root] if root.is_dir() else []
    while pending:
        directory = pending.pop()
        try:
            if _reparse(directory) or not directory.resolve().is_relative_to(root):
                continue
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError:
            errors += 1
            continue
        descendants = []
        for entry in entries:
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
                if _reparse(path, info):
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    continue
                relative = resolved.relative_to(root)
                name = entry.name.casefold()
                if entry.is_dir(follow_symlinks=False):
                    if name in _PRIVATE_TREES:
                        continue
                    if directory == root and name not in _MEDIA_TREES:
                        continue
                    if any(part.casefold() == 'sidecars' for part in relative.parts[:-1]):
                        continue
                    descendants.append(resolved)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                # Windows DirEntry enumeration may return zero device/inode
                # even on NTFS. Query the file identity without following a
                # reparse point before deduplicating physical hardlinks.
                if not info.st_ino:
                    info = path.stat(follow_symlinks=False)
                    if _reparse(path, info):
                        continue
                origin = _local_provider(relative)
                file_kind = _local_kind(relative, origin)
                if file_kind is None:
                    continue
                path_key = os.path.normcase(str(resolved))
                file_key = (info.st_dev, info.st_ino) if info.st_ino else ('path', path_key)
                reasons = set(protection['paths'].get(path_key, set()))
                if protection['status'] == 'unknown':
                    reasons.add('protection_state_unknown')
                elif protection['sha256']:
                    reasons.add('digest_protection_unverified')
                if path_key in seen_paths or file_key in seen_files:
                    if file_key in seen_files:
                        # A pending path may identify a different hardlink to
                        # the same physical file. Preserve that protection on
                        # its representative instead of discarding the alias.
                        existing = items[seen_files[file_key]]
                        combined = set(existing['protection_reasons']) | reasons
                        existing['protection_reasons'] = sorted(combined)
                        existing['protected'] = bool(combined)
                    continue
                seen_paths.add(path_key)
                seen_files[file_key] = len(items)
                items.append({'name': entry.name, 'relative_path': relative.as_posix(),
                              'path': str(resolved), 'provider': origin, 'kind': file_kind,
                              'stage': _local_stage(entry.name, file_kind),
                              'size_bytes': info.st_size, 'modified_at': info.st_mtime,
                              'protected': bool(reasons), 'protection_reasons': sorted(reasons)})
            except OSError:
                errors += 1
        # Reverse push gives deterministic lexicographic depth-first order.
        pending.extend(reversed(descendants))
    items.sort(key=lambda item: item['relative_path'].casefold())
    by_provider = {name: {'file_count': 0, 'total_bytes': 0} for name in sorted(_LOCAL_PROVIDERS)}
    for item in items:
        totals = by_provider[item['provider']]
        totals['file_count'] += 1
        totals['total_bytes'] += item['size_bytes']
    terms = query.casefold().split()
    selected = [item for item in items
                if (provider == 'all' or item['provider'] == provider)
                and (kind == 'all' or item['kind'] == kind)
                and all(term in item['relative_path'].casefold() for term in terms)]
    return {'schema': 1, 'inventory_scope': 'local_media_files', 'state': 'ready',
            'library_root': str(root), 'total': len(selected), 'offset': offset, 'limit': limit,
            'file_count': len(items), 'total_bytes': sum(item['size_bytes'] for item in items),
            'verified_at': time.time(), 'by_provider': by_provider,
            'protection_status': protection['status'], 'scan_complete': errors == 0,
            'scan_errors': errors, 'items': selected[offset:offset + limit]}
