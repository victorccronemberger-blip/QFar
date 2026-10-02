"""Index and browse the complete original Ego4D library without task filtering.

This research inventory preserves source metadata and annotations. It does not
submit media, infer task approval, fabricate sensors, or download recordings.
"""
from __future__ import annotations

import csv
from contextlib import closing
import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3
import uuid


def audit_sensor(path: Path) -> dict:
    """Measure the original CSV, not the interpolated output sample grid."""
    times = {'gyroscope': [], 'accelerometer': []}
    invalid = dict.fromkeys(times, 0)
    backwards = dict.fromkeys(times, 0)
    columns = {'gyroscope': ('gyro_x', 'gyro_y', 'gyro_z'),
               'accelerometer': ('accl_x', 'accl_y', 'accl_z')}
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        required = {'canonical_timestamp_ms', *(name for group in columns.values() for name in group)}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('Sensor sem colunas canônicas obrigatórias.')
        for row in reader:
            when = number(row.get('canonical_timestamp_ms'))
            for sensor, names in columns.items():
                values = [number(row.get(name)) for name in names]
                if when is None or any(value is None for value in values):
                    invalid[sensor] += 1
                    continue
                if times[sensor] and when < times[sensor][-1]:
                    backwards[sensor] += 1
                times[sensor].append(when)
    result = {'file': Path(path).name, 'sensors': {}}
    for sensor, samples in times.items():
        ordered = sorted(set(samples))
        span = ordered[-1] - ordered[0] if ordered else 0
        result['sensors'][sensor] = {
            'valid_rows': len(samples), 'unique_timestamps': len(ordered),
            'missing_or_invalid_rows': invalid[sensor], 'out_of_order_rows': backwards[sensor],
            'first_timestamp_ms': ordered[0] if ordered else None,
            'last_timestamp_ms': ordered[-1] if ordered else None,
            'mean_observed_rate_hz': (len(ordered) - 1) * 1000 / span if span > 0 else None,
            'maximum_gap_ms': max((b - a for a, b in zip(ordered, ordered[1:])), default=None),
        }
    return result


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def index_library(directory: Path, output: Path, progress=None) -> dict:
    directory, output = Path(directory), Path(output)
    if output.suffix != '.sqlite3':
        raise ValueError('O índice deve ser um arquivo .sqlite3 separado das fontes.')
    metadata = directory / 'ego4d.json'
    source_paths = (metadata, directory / 'clips.csv', directory / 'timed_narrations.jsonl')
    stamps = {path.name: (path.stat().st_size, path.stat().st_mtime_ns)
              for path in source_paths if path.exists()}
    if progress:
        progress('Lendo metadados originais…')
    source = json.loads(metadata.read_text(encoding='utf-8-sig'))
    videos = source.get('videos')
    if not isinstance(videos, list) or not videos:
        raise ValueError('Catálogo sem vídeos; índice anterior preservado.')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.' + uuid.uuid4().hex + '.tmp')
    report = {'videos': 0, 'clips': 0, 'annotations': 0, 'local_sensor_files': 0,
              'issues': {}, 'offline': True, 'dataset_version': source.get('version')}

    def issue(name):
        report['issues'][name] = report['issues'].get(name, 0) + 1

    db = sqlite3.connect(temporary)
    try:
        db.executescript('''
          PRAGMA user_version=2;
          CREATE TABLE source_file(name TEXT PRIMARY KEY, sha256 TEXT, bytes INTEGER, mtime_ns INTEGER);
          CREATE TABLE index_report(json TEXT NOT NULL);
          CREATE TABLE video(uid TEXT PRIMARY KEY, duration_s REAL, device TEXT,
            has_imu INTEGER, imu_local INTEGER, metadata_json TEXT);
          CREATE TABLE scenario(video_uid TEXT REFERENCES video(uid), name TEXT);
          CREATE INDEX scenario_name ON scenario(name);
          CREATE INDEX scenario_video ON scenario(video_uid);
          CREATE TABLE clip(uid TEXT PRIMARY KEY, parent_uid TEXT, start_s REAL,
            end_s REAL, duration_s REAL, valid_window INTEGER, metadata_json TEXT);
          CREATE INDEX clip_parent ON clip(parent_uid);
          CREATE TABLE annotation(video_uid TEXT, timestamp_s REAL, text TEXT);
          CREATE INDEX annotation_video ON annotation(video_uid, timestamp_s);
          CREATE VIRTUAL TABLE search USING fts5(video_uid UNINDEXED, text);
        ''')
        durations = {}
        for video in videos:
            if not isinstance(video, dict) or not isinstance(video.get('video_uid'), str):
                raise ValueError('Vídeo sem identidade; índice anterior preservado.')
            uid = video['video_uid']
            if uid in durations:
                raise ValueError('Identidade de vídeo duplicada; índice anterior preservado.')
            duration = number(video.get('duration_sec'))
            if duration is None or duration <= 0:
                issue('invalid_video_duration')
                duration = None
            durations[uid] = duration
            # Do not construct paths from unvalidated catalog identities.
            try:
                canonical_uid = str(uuid.UUID(uid))
            except ValueError:
                canonical_uid = None
            local = bool(canonical_uid and (directory / (canonical_uid + '_imu.csv')).is_file())
            declared = video.get('has_imu')
            has_imu = int(declared) if type(declared) is bool else None
            if local:
                report['local_sensor_files'] += 1
            db.execute('INSERT INTO video VALUES (?,?,?,?,?,?)',
                       (uid, duration, video.get('device'), has_imu, int(local), json.dumps(video)))
            scenarios = video.get('scenarios') or []
            if not isinstance(scenarios, list):
                issue('invalid_scenarios')
                scenarios = []
            for scenario in sorted({' '.join(str(x).split()) for x in scenarios if str(x).strip()}):
                db.execute('INSERT INTO scenario VALUES (?,?)', (uid, scenario))
                db.execute('INSERT INTO search VALUES (?,?)', (uid, scenario))
            report['videos'] += 1
        manifest = directory / 'clips.csv'
        with manifest.open(encoding='utf-8-sig', newline='') as stream:
            for clip in csv.DictReader(stream):
                uid, parent = clip.get('exported_clip_uid'), clip.get('parent_video_uid')
                if not uid:
                    raise ValueError('Clipe sem identidade; índice anterior preservado.')
                start, end = number(clip.get('parent_start_sec')), number(clip.get('parent_end_sec'))
                valid = (parent in durations and durations[parent] is not None
                         and start is not None and end is not None
                         and 0 <= start < end <= durations[parent] + 0.1)
                if not valid:
                    issue('invalid_clip_window')
                duration = end - start if valid else None
                db.execute('INSERT INTO clip VALUES (?,?,?,?,?,?,?)',
                           (uid, parent, start, end, duration, int(valid), json.dumps(clip)))
                report['clips'] += 1
        narrations = directory / 'timed_narrations.jsonl'
        if progress:
            progress('Indexando atividades e anotações…')
        if narrations.exists():
            with narrations.open(encoding='utf-8-sig') as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    uid = record.get('video_uid')
                    for event in record.get('events') or []:
                        when = number(event[0]) if isinstance(event, list) and len(event) >= 2 else None
                        text = event[1] if isinstance(event, list) and len(event) >= 2 else None
                        if (uid not in durations or durations[uid] is None or when is None
                                or not 0 <= when <= durations[uid] + 0.1
                                or not isinstance(text, str) or not text.strip()):
                            issue('invalid_annotation')
                            continue
                        db.execute('INSERT INTO annotation VALUES (?,?,?)', (uid, when, text))
                        db.execute('INSERT INTO search VALUES (?,?)', (uid, text))
                        report['annotations'] += 1
        for path in (metadata, manifest, narrations):
            if path.exists():
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                stamp = path.stat()
                if stamps.get(path.name) != (stamp.st_size, stamp.st_mtime_ns):
                    raise ValueError('Metadados mudaram durante a indexação; índice anterior preservado.')
                db.execute('INSERT INTO source_file VALUES (?,?,?,?)',
                           (path.name, digest.hexdigest(), stamp.st_size, stamp.st_mtime_ns))
        if set(stamps) != {path.name for path in source_paths if path.exists()}:
            raise ValueError('Fontes mudaram durante a indexação; índice anterior preservado.')
        db.execute('INSERT INTO index_report VALUES (?)', (json.dumps(report),))
        db.commit()
        db.close()
        temporary.replace(output)
    except BaseException:
        db.close()
        temporary.unlink(missing_ok=True)
        raise
    return report


def library_summary(path: Path, directory: Path) -> dict:
    if not Path(path).is_file():
        return {'state': 'missing', 'needs_index': True}
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA user_version').fetchone()[0] != 2:
            return {'state': 'outdated', 'needs_index': True}
        stale = False
        stored = list(db.execute('SELECT name,bytes,mtime_ns FROM source_file'))
        if not {'ego4d.json', 'clips.csv'}.issubset({row[0] for row in stored}):
            raise ValueError('Índice sem proveniência das fontes obrigatórias.')
        for name, size, mtime in stored:
            if name not in {'ego4d.json', 'clips.csv', 'timed_narrations.jsonl'}:
                raise ValueError('Proveniência do índice inválida.')
            source = Path(directory) / name
            if not source.is_file() or (source.stat().st_size, source.stat().st_mtime_ns) != (size, mtime):
                stale = True
        optional = Path(directory) / 'timed_narrations.jsonl'
        if optional.exists() and optional.name not in {row[0] for row in stored}:
            stale = True
        row = db.execute('SELECT json FROM index_report LIMIT 1').fetchone()
        if row is None:
            raise ValueError('Índice sem relatório de construção.')
        report = json.loads(row[0])
        if (not isinstance(report, dict) or any(type(report.get(key)) is not int
                or report[key] < 0 for key in ('videos', 'clips', 'annotations'))):
            raise ValueError('Relatório de construção do índice inválido.')
        sensors = db.execute('SELECT SUM(has_imu=1),SUM(has_imu IS NULL) FROM video').fetchone()
        local_uids = {p.name.removesuffix('_imu.csv') for p in Path(directory).glob('*_imu.csv')}
        local_count = sum(uid in local_uids for (uid,) in db.execute('SELECT uid FROM video'))
        return {**report, 'state': 'stale' if stale else 'ready', 'needs_index': stale,
                'local_sensor_files': local_count,
                'videos_with_declared_imu': sensors[0] or 0, 'videos_with_unknown_imu': sensors[1] or 0,
                'scenarios': db.execute('SELECT COUNT(DISTINCT name) FROM scenario').fetchone()[0],
                'original_hours': (db.execute('SELECT SUM(duration_s) FROM video').fetchone()[0] or 0) / 3600,
                'provenance': 'ego4d_original', 'sensor_verification': 'not_implied_by_presence'}


def browse_library(path: Path, query='', *, minimum_s=0, maximum_s=None,
                   imu_only=False, limit=50, offset=0) -> dict:
    if (not isinstance(query, str) or len(query) > 200 or type(limit) is not int
            or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000
            or type(minimum_s) not in (int, float) or number(minimum_s) is None or minimum_s < 0
            or (maximum_s is not None and (type(maximum_s) not in (int, float)
                or number(maximum_s) is None or maximum_s < minimum_s))):
        raise ValueError('Filtros de biblioteca inválidos.')
    # User text is literal; quotation/operators cannot inject FTS syntax.
    terms = re.findall(r'\w+', query, re.UNICODE)
    match = ' AND '.join('"' + term.replace('"', '""') + '"*' for term in terms)
    predicates = ['v.duration_s >= ?']
    arguments = [minimum_s]
    if maximum_s is not None:
        predicates.append('v.duration_s <= ?')
        arguments.append(maximum_s)
    if imu_only:
        predicates.append('v.has_imu=1')
    if match:
        predicates.append('v.uid IN (SELECT video_uid FROM search WHERE search MATCH ?)')
        arguments.append(match)
    where = ' AND '.join(predicates)
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        total = db.execute('SELECT COUNT(*) FROM video v WHERE ' + where, arguments).fetchone()[0]
        rows = db.execute('''SELECT v.uid,v.duration_s,v.device,v.has_imu,
            (SELECT GROUP_CONCAT(name, ' · ') FROM scenario s WHERE s.video_uid=v.uid) AS scenarios,
            (SELECT COUNT(*) FROM annotation a WHERE a.video_uid=v.uid) AS annotation_count
            FROM video v WHERE ''' + where + ' ORDER BY v.uid LIMIT ? OFFSET ?', [*arguments, limit, offset])
        items = [dict(row) for row in rows]
        for item in items:
            try:
                canonical_uid = str(uuid.UUID(item['uid']))
            except ValueError:
                canonical_uid = None
            item['imu_local'] = bool(canonical_uid and (Path(path).parent / (canonical_uid + '_imu.csv')).is_file())
        return {'total': total, 'offset': offset, 'limit': limit, 'provenance': 'ego4d_original',
                'items': items}


def search_library(path: Path, query: str, limit: int = 20) -> list[dict]:
    # Read-only connection cannot change the inventory or original metadata.
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute('''
          SELECT v.uid, v.duration_s, v.device, v.has_imu, v.imu_local
          FROM video v JOIN (SELECT DISTINCT video_uid FROM search WHERE search MATCH ?) hits
            ON hits.video_uid=v.uid ORDER BY v.uid LIMIT ?
        ''', (query, limit))]
