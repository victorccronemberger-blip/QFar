"""Acquire and browse the complete Nymeria source library.

Catalog actions only plan downloads. A recording becomes a campaign candidate
through ``nymeria`` after its actual RGB/IMU clocks have been read by the SDK.
This module never submits content or creates account delivery archives.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
from urllib.parse import urlsplit
import uuid
import zipfile

import requests

from . import nymeria, task_matching
from .media_lifecycle import (cleanup_operation, cleanup_managed_media,
                              record_managed_media)

_GROUPS = ("metadata_json", "narration", "timesync_and_imu", "recording_head_data_data_vrs")
_IDENTITY = re.compile(r"[A-Za-z0-9_-]+\Z")
_LOCK = threading.RLock()
_PLAN_CACHE = {}
_INVENTORY_CACHE = {}
_EVENT_CACHE = {}
_CATALOG_INPUT_CACHE = {}


class NymeriaCancelled(RuntimeError):
    """A requested pause retains original files and bound partial downloads."""


class NymeriaLibraryError(RuntimeError):
    """A safe operational message that can be displayed without signed URLs."""
    def __init__(self, message, code):
        super().__init__(message)
        self.safe_message, self.code = message, code


def _check_stop(should_stop):
    if should_stop is not None and should_stop():
        raise NymeriaCancelled("Operação Nymeria interrompida; arquivos e downloads parciais preservados.")


def _root(root=None):
    supplied = Path(root) if root is not None else nymeria.data_root()
    if supplied.exists() and _reparse(supplied):
        raise ValueError("A raiz Nymeria não pode ser um atalho de diretório.")
    return supplied.resolve()


def data_root():
    return _root()


def _reparse(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _path(root, *parts):
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Caminho Nymeria fora da Biblioteca.")
    for current in (path, *path.parents):
        if current.exists() and _reparse(current):
            raise ValueError("Atalho de diretório na Biblioteca Nymeria.")
        if current == root:
            break
    return path


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), "utf8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _hash(path, algorithm="sha1", should_stop=None):
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            _check_stop(should_stop)
            digest.update(block)
    return digest.hexdigest()


def _manifest(value):
    sequences = value.get("sequences") if isinstance(value, dict) else None
    if not isinstance(sequences, dict) or not sequences:
        raise ValueError("Manifesto Nymeria sem sequências.")
    normalized = {}
    for sid, groups in sequences.items():
        if not isinstance(sid, str) or not _IDENTITY.fullmatch(sid) or not isinstance(groups, dict):
            raise ValueError("Identidade de sequência Nymeria inválida.")
        selected = {}
        for name in _GROUPS:
            asset = groups.get(name)
            if not isinstance(asset, dict):
                raise ValueError("Manifesto Nymeria sem um arquivo essencial.")
            filename, digest = asset.get("filename"), asset.get("sha1sum")
            size, url = asset.get("file_size_bytes"), asset.get("download_url")
            parsed = urlsplit(url) if isinstance(url, str) else None
            if (not isinstance(filename, str) or Path(filename).name != filename
                    or "/" in filename or "\\" in filename or filename in {".", ".."}
                    or not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", digest)
                    or type(size) is not int or size <= 0 or parsed is None
                    or parsed.scheme != "https" or parsed.username or parsed.password
                    or parsed.port not in (None, 443) or not parsed.hostname
                    or not parsed.hostname.endswith(".fbcdn.net")):
                raise ValueError("Arquivo ou URL de origem Nymeria inválido.")
            selected[name] = {"filename": filename, "sha1sum": digest.lower(),
                              "file_size_bytes": size, "download_url": url}
        normalized[sid] = selected
    return {"schema": 1, "sequences": normalized}


def import_manifest(path, root=None, *, summarize=True):
    """Persist every recording and its official asset integrity information."""
    value = _manifest(json.loads(Path(path).read_text("utf-8-sig")))
    base = _root(root)
    with _LOCK:
        destination = _path(base, "_catalog", "download_urls.json")
        _write_json(destination, value)
        _PLAN_CACHE.clear()
        _INVENTORY_CACHE.clear()
        _EVENT_CACHE.clear()
        _CATALOG_INPUT_CACHE.clear()
    return summary(base) if summarize else {"sequence_count": len(value["sequences"])}


def initialize_portable_catalog(app_root, root=None):
    """Adopt an explicitly supplied private companion through the normal importer.

    Never scan a user's Downloads, copy credentials, or replace their existing
    catalog. No inventory, SDK bootstrap, classification or network at startup.
    """
    if not app_root:
        return {"state": "not_supplied"}
    try:
        base = _root(root)
        destination = _path(base, "_catalog", "download_urls.json")
        if destination.exists():
            return {"state": "preserved"}
        portable = _root(app_root)
        companion = _path(portable, "nymeria_plus_download_urls.json")
        if not companion.is_file():
            return {"state": "not_supplied"}
        if companion.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Manifesto acima de 32 MiB.")
        # Serialize the existence check with an explicit import in this process.
        with _LOCK:
            if destination.exists():
                return {"state": "preserved"}
            result = import_manifest(companion, base, summarize=False)
        return {"state": "imported", **result,
                "message": "NymeriaPlus configurado automaticamente. Vídeos e IMU serão baixados durante a campanha."}
    except (OSError, ValueError, RuntimeError):
        return {"state": "error", "message":
                "Não foi possível importar a configuração portátil NymeriaPlus. Na Biblioteca, use Importar manifesto JSON para escolher um JSON válido."}


def _load(root):
    path = _path(root, "_catalog", "download_urls.json")
    if not path.is_file():
        return {"schema": 1, "sequences": {}}
    return _manifest(json.loads(path.read_text("utf8")))


def _valid_asset(path, asset, should_stop=None):
    return (path.is_file() and path.stat().st_size == asset["file_size_bytes"]
            and _hash(path, should_stop=should_stop) == asset["sha1sum"])


def _asset_identity(groups):
    return {name: {key: groups[name][key] for key in ("sha1sum", "file_size_bytes")}
            for name in _GROUPS}


class _RemoteZipFile(io.RawIOBase):
    """Read the original ZIP directory with bounded HTTP Range requests."""
    def __init__(self, asset, session, should_stop=None):
        self.asset, self.session, self.should_stop = asset, session, should_stop
        self.position = 0

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        position = offset + (self.position if whence == 1 else self.asset["file_size_bytes"] if whence == 2 else 0)
        if position < 0:
            raise OSError("Posição inválida no índice ZIP Nymeria.")
        self.position = position
        return position

    def read(self, size=-1):
        _check_stop(self.should_stop)
        total = self.asset["file_size_bytes"]
        size = total - self.position if size < 0 else min(size, total - self.position)
        if size <= 0:
            return b""
        if size > 16 * 1024 * 1024:
            raise ValueError("Índice ZIP Nymeria acima do tamanho suportado.")
        start, end = self.position, self.position + size - 1
        headers = {"Accept-Encoding": "identity", "Range": f"bytes={start}-{end}"}
        with self.session.get(self.asset["download_url"], stream=True, headers=headers,
                              timeout=(20, 60), allow_redirects=False) as response:
            if (response.status_code != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{total}"
                    or int(response.headers.get("Content-Length", -1)) != size):
                raise NymeriaLibraryError("A origem não forneceu o intervalo exato do índice ZIP. "
                    "Nenhum VRS grande foi baixado; tente verificar o espaço novamente.", "zip_index_range_invalid")
            blocks, received = [], 0
            for block in response.iter_content(64 * 1024):
                _check_stop(self.should_stop)
                received += len(block)
                if received > size:
                    raise NymeriaLibraryError("O índice ZIP excedeu o intervalo solicitado. "
                        "Nenhum VRS grande foi baixado.", "zip_index_range_invalid")
                blocks.append(block)
        if received != size:
            raise NymeriaLibraryError("O índice ZIP chegou incompleto. "
                "Nenhum VRS grande foi baixado; tente verificar o espaço novamente.", "zip_index_incomplete")
        self.position += received
        return b"".join(blocks)


def _extraction_bytes(root, sid, groups, *, fetch=False, should_stop=None):
    target = _path(root, sid, "recording_head", "data", "motion.vrs")
    if target.is_file():
        return 0
    asset = groups["timesync_and_imu"]
    marker = _path(root, "_catalog", "archive_index", sid + ".json")
    identity = {key: asset[key] for key in ("sha1sum", "file_size_bytes")}
    if marker.is_file():
        try:
            record = json.loads(marker.read_text("utf8"))
            if (record.get("asset_identity") == identity
                    and type(record.get("head_motion_bytes")) is int and record["head_motion_bytes"] > 0):
                return record["head_motion_bytes"]
        except (OSError, ValueError, AttributeError):
            pass
    archive = _path(root, "_catalog", "archives", sid, "timesync_and_imu.zip")
    if archive.is_file() and _valid_asset(archive, asset, should_stop):
        with zipfile.ZipFile(archive) as bundle:
            size = bundle.getinfo("recording_head/data/motion.vrs").file_size
        verified = True
    elif fetch:
        try:
            with requests.Session() as session:
                session.trust_env = False
                with zipfile.ZipFile(_RemoteZipFile(asset, session, should_stop)) as bundle:
                    matches = [item for item in bundle.infolist() if item.filename == "recording_head/data/motion.vrs"]
                    if len(matches) != 1 or matches[0].is_dir() or stat.S_ISLNK(matches[0].external_attr >> 16):
                        raise ValueError("Índice Nymeria sem um único arquivo IMU da cabeça.")
                    size = matches[0].file_size
        except (requests.RequestException, zipfile.BadZipFile, KeyError, ValueError) as exc:
            raise NymeriaLibraryError("Não foi possível ler o tamanho original do IMU no índice ZIP. "
                "Nenhum VRS grande foi baixado; confira a conexão e atualize o manifesto se necessário.",
                "zip_index_unavailable") from exc
        verified = False
    else:
        return None
    if type(size) is not int or size <= 0:
        raise ValueError("Tamanho de extração IMU Nymeria inválido.")
    _write_json(marker, {"asset_identity": identity, "head_motion_bytes": size,
                         "source": "original_zip_directory", "full_archive_sha1_verified": verified})
    return size


def _download(asset, target, root, progress=None, should_stop=None, min_free_bytes=0):
    """Resume the bound original bytes and publish only after official SHA-1."""
    target = _path(root, *target.relative_to(root).parts)
    _check_stop(should_stop)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if _valid_asset(target, asset, should_stop):
            _own_source_download(target, root, asset)
            return target
        raise ValueError("Arquivo Nymeria existente diverge da origem; foi preservado.")
    partial = _path(root, *target.with_name(target.name + ".part").relative_to(root).parts)
    binding = _path(root, *target.with_name(target.name + ".part.binding.json").relative_to(root).parts)
    identity = {key: asset[key] for key in ("filename", "sha1sum", "file_size_bytes")}
    if partial.exists():
        if not binding.is_file() or json.loads(binding.read_text("utf8")) != identity:
            raise ValueError("Download parcial Nymeria pertence a outra origem; foi preservado.")
    else:
        _write_json(binding, identity)
    size = asset["file_size_bytes"]
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > size:
        raise ValueError("Download parcial Nymeria maior que o arquivo de origem.")
    if shutil.disk_usage(target.parent).free < size - offset + min_free_bytes:
        raise OSError("Espaço livre insuficiente para o arquivo Nymeria selecionado.")
    for attempt in range(3):
        try:
            if offset < size:
                with requests.Session() as session:
                    session.trust_env = False
                    headers = {"Accept-Encoding": "identity"}
                    if offset:
                        headers["Range"] = f"bytes={offset}-"
                    with session.get(asset["download_url"], stream=True, headers=headers,
                                     timeout=(20, 60), allow_redirects=False) as response:
                        expected_status = 206 if offset else 200
                        if response.status_code != expected_status:
                            raise RuntimeError(f"Origem Nymeria respondeu HTTP {response.status_code}.")
                        if offset and response.headers.get("Content-Range") != f"bytes {offset}-{size - 1}/{size}":
                            raise RuntimeError("Intervalo de retomada Nymeria divergente.")
                        if int(response.headers.get("Content-Length", -1)) != size - offset:
                            raise RuntimeError("Tamanho HTTP Nymeria diverge do manifesto.")
                        with partial.open("ab") as stream:
                            for block in response.iter_content(1024 * 1024):
                                _check_stop(should_stop)
                                if not block:
                                    continue
                                if offset + len(block) > size:
                                    raise RuntimeError("Origem Nymeria excedeu o tamanho esperado.")
                                stream.write(block)
                                offset += len(block)
                                if shutil.disk_usage(target.parent).free < min_free_bytes:
                                    raise OSError("A reserva de espaço livre Nymeria foi atingida; parcial preservado.")
                                if progress and offset % (32 * 1024 * 1024) < len(block):
                                    progress(f"Baixando {target.name}: {offset}/{size} bytes")
                            stream.flush()
                            os.fsync(stream.fileno())
            _check_stop(should_stop)
            if not _valid_asset(partial, asset, should_stop):
                raise ValueError("SHA-1 ou tamanho do arquivo Nymeria não confere; parcial preservado.")
            partial.replace(target)
            binding.unlink(missing_ok=True)
            _own_source_download(target, root, asset)
            return target
        except requests.RequestException as exc:
            offset = partial.stat().st_size if partial.exists() else 0
            if attempt == 2:
                raise RuntimeError("Download Nymeria interrompido; os bytes parciais foram preservados.") from exc
    raise RuntimeError("Download Nymeria incompleto.")


def _own_source_download(target, root, asset):
    # Catalogs and narration stay small and reusable. Only officially verified
    # bulky sources are temporary campaign media; supplied foreign files are
    # never adopted solely because they happen to have the expected filename.
    if target.suffix == '.vrs' or target.name == 'timesync_and_imu.zip':
        record_managed_media(target, root=root, provider='nymeria',
                             role='source_download', expected_digest=asset['sha1sum'], algorithm='sha1')


def _extract(archive, root, sid, prefix, should_stop=None, min_free_bytes=0):
    """Extract only source narration or head IMU; never follow archive paths."""
    with zipfile.ZipFile(archive) as bundle:
        selected = []
        for item in bundle.infolist():
            path = PurePosixPath(item.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in item.filename
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError("Caminho inválido no arquivo Nymeria; extração cancelada.")
            if item.is_dir() or not item.filename.startswith(prefix):
                continue
            if prefix == "narration/" and (len(path.parts) != 2 or path.name not in nymeria._ANNOTATIONS):
                continue
            if prefix != "narration/" and item.filename != "recording_head/data/motion.vrs":
                continue
            selected.append(item)
        for item in selected:
            _check_stop(should_stop)
            target = _path(root, sid, *PurePosixPath(item.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                with bundle.open(item) as source:
                    digest = hashlib.sha256()
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                if target.stat().st_size != item.file_size or _hash(target, "sha256") != digest.hexdigest():
                    raise ValueError("Fonte Nymeria extraída existente diverge; foi preservada.")
                if item.filename == 'recording_head/data/motion.vrs':
                    record_managed_media(target, root=root, provider='nymeria',
                                         role='source_imu', expected_digest=digest.hexdigest())
                continue
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            if shutil.disk_usage(target.parent).free < item.file_size + min_free_bytes:
                raise OSError("Espaço insuficiente para extrair a fonte Nymeria.")
            try:
                digest = hashlib.sha256()
                with bundle.open(item) as source, temporary.open("xb") as output:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        _check_stop(should_stop)
                        output.write(block)
                        digest.update(block)
                if temporary.stat().st_size != item.file_size:
                    raise ValueError("Tamanho do arquivo Nymeria extraído divergente.")
                temporary.replace(target)
                if item.filename == 'recording_head/data/motion.vrs':
                    record_managed_media(target, root=root, provider='nymeria',
                                         role='source_imu', expected_digest=digest.hexdigest())
            finally:
                temporary.unlink(missing_ok=True)
    return len(selected)


def _sync_sequence(root, sid, groups, should_stop=None):
    metadata = _download(groups["metadata_json"], _path(root, sid, "metadata.json"), root,
                         should_stop=should_stop)
    value = json.loads(metadata.read_text("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Metadata Nymeria inválido.")
    archive = _download(groups["narration"], _path(root, "_catalog", "archives", sid, "narration.zip"), root,
                        should_stop=should_stop)
    count = _extract(archive, root, sid, "narration/", should_stop)
    if count:
        try:
            rows, _hashes = nymeria._annotation_rows(_path(root, sid))
        except ValueError:
            return "invalid_annotations"
        if rows:
            return "cataloged"
    return "no_annotations"


def sync_catalog(root=None, progress=None, max_workers=12, should_stop=None):
    """Acquire metadata and atomic annotations for the entire imported catalog."""
    if type(max_workers) is not int or not 1 <= max_workers <= 32:
        raise ValueError("Paralelismo Nymeria inválido.")
    base = _root(root)
    with _LOCK:
        sequences = _load(base)["sequences"]
        if not sequences:
            raise ValueError("Importe o manifesto Nymeria antes de sincronizar.")
        report = {"schema": 1, "phase": "syncing_catalog", "total": len(sequences),
                  "completed": 0, "errors": {}, "states": {}}
        state = _path(base, "_catalog", "state.json")
        _write_json(state, report)
        with ThreadPoolExecutor(max_workers=max_workers) as workers:
            futures = {workers.submit(_sync_sequence, base, sid, groups, should_stop): sid
                       for sid, groups in sequences.items()}
            for future in as_completed(futures):
                sid = futures[future]
                try:
                    report["states"][sid] = future.result()
                except NymeriaCancelled:
                    for pending in futures:
                        pending.cancel()
                    report["phase"] = "paused"
                    _write_json(state, report)
                    raise
                except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
                    # URLs contain signed access parameters; do not publish exception strings.
                    report["states"][sid] = "download_failed"
                    report["errors"][sid] = "Não foi possível verificar os arquivos originais desta sequência."
                report["completed"] += 1
                _write_json(state, report)
                if progress:
                    progress(f"Catálogo Nymeria: {report['completed']}/{report['total']} sequências")
        report["phase"] = "catalog_synced" if not report["errors"] else "catalog_partial"
        _write_json(state, report)
        _PLAN_CACHE.clear()
        _INVENTORY_CACHE.clear()
        _EVENT_CACHE.clear()
        _CATALOG_INPUT_CACHE.clear()
        nymeria.clear_caches()
        result = summary(base)
        result.update(sync=report)
        return result


def _sequence_item(root, sid, groups):
    directory = _path(root, sid)
    metadata = _path(root, sid, "metadata.json")
    value = {}
    if metadata.is_file():
        try:
            value = json.loads(metadata.read_text("utf-8-sig"))
            if not isinstance(value, dict):
                value = {}
        except (OSError, ValueError):
            pass
    action_file = _path(root, sid, "narration", "atomic_action.csv")
    try:
        rows, _hashes = nymeria._annotation_rows(directory) if action_file.is_file() else ([], {})
    except (OSError, ValueError):
        rows = []
    video = _path(root, sid, "recording_head", "data", "data.vrs")
    motion = _path(root, sid, "recording_head", "data", "motion.vrs")
    has_video, has_motion = video.is_file(), motion.is_file()
    duration = value.get("head_duration_sec")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        duration = None
    if has_video and has_motion and rows:
        state = "downloaded"
    elif has_video or has_motion:
        state = "partial"
    elif rows:
        state = "cataloged"
    elif metadata.is_file():
        state = "no_annotations"
    else:
        state = "missing"
    measured = None
    marker = _path(root, "_catalog", "measured", sid + ".json")
    if state == "downloaded" and marker.is_file():
        try:
            measured = json.loads(marker.read_text("utf8"))
            current = [list(nymeria._stat(directory / name)) for name in
                       ("metadata.json", "recording_head/data/data.vrs", "recording_head/data/motion.vrs",
                        *("narration/" + name for name in nymeria._ANNOTATIONS))]
            if (measured.get("source_stats") != current
                    or measured.get("asset_identity") != _asset_identity(groups)
                    or measured.get("rules_sha256") != nymeria._rules_digest()
                    or measured.get("sdk_signature") != [[name, list(marker)]
                        for name, marker in nymeria._sdk_signature() if isinstance(marker, tuple)]):
                measured = None
        except (OSError, ValueError, AttributeError):
            measured = None
        if measured:
            state = "measured"
    download_bytes = sum(groups[name]["file_size_bytes"] for name in
                         ("recording_head_data_data_vrs", "timesync_and_imu"))
    if has_video and video.stat().st_size == groups["recording_head_data_data_vrs"]["file_size_bytes"]:
        download_bytes -= groups["recording_head_data_data_vrs"]["file_size_bytes"]
    archive = _path(root, "_catalog", "archives", sid, "timesync_and_imu.zip")
    if archive.is_file() and archive.stat().st_size == groups["timesync_and_imu"]["file_size_bytes"]:
        download_bytes -= groups["timesync_and_imu"]["file_size_bytes"]
    return {"seq_id": sid, "uid": str(value.get("uid") or sid), "path": str(directory),
            "script": str(value.get("script") or ""), "state": state,
            "declared_duration_s": duration, "atomic_action_count": len(rows),
            "has_data_vrs": has_video, "has_motion_vrs": has_motion,
            "download_bytes": download_bytes, "selection_ready": measured is not None,
            "measured_duration_s": measured.get("measured_duration_s") if measured else None,
            "requires_measured_validation": measured is None}


def _inventory_items(base, sequences):
    # Catalog browsing cannot open VRS indexes or repeatedly parse every CSV.
    # Bind the cached physical inventory to all source/marker stats and SDK.
    paths = [base / "_catalog/download_urls.json"]
    for sid in sequences:
        paths.extend(base / sid / name for name in
                     ("metadata.json", "recording_head/data/data.vrs", "recording_head/data/motion.vrs",
                      *("narration/" + name for name in nymeria._ANNOTATIONS)))
        paths.append(base / "_catalog/measured" / (sid + ".json"))
        paths.append(base / "_catalog/archives" / sid / "timesync_and_imu.zip")
        paths.append(base / "_catalog/archive_index" / (sid + ".json"))
    key = (str(base), tuple(nymeria._stat(path) for path in paths),
           nymeria._rules_digest(), nymeria._sdk_signature())
    cached = _INVENTORY_CACHE.get(key)
    if cached is not None:
        return cached
    items = [_sequence_item(base, sid, groups) for sid, groups in sequences.items()]
    if len(_INVENTORY_CACHE) >= 4:
        _INVENTORY_CACHE.clear()
    _INVENTORY_CACHE[key] = items
    return items


def summary(root=None):
    base = _root(root)
    sequences = _load(base)["sequences"]
    items = _inventory_items(base, sequences)
    counts = {name: sum(item["state"] == name for item in items)
              for name in ("missing", "cataloged", "downloaded", "measured", "partial", "no_annotations")}
    disk = base
    while not disk.exists() and disk != disk.parent:
        disk = disk.parent
    return {"root": str(base), "state": "indexed" if sequences else "missing",
            "sequence_count": len(sequences), "by_state": counts,
            "atomic_action_count": sum(item["atomic_action_count"] for item in items),
            "catalog_download_bytes": sum(groups[name]["file_size_bytes"]
                                          for groups in sequences.values() for name in ("metadata_json", "narration")),
            "source_download_bytes": sum(groups[name]["file_size_bytes"]
                                         for groups in sequences.values() for name in
                                         ("timesync_and_imu", "recording_head_data_data_vrs")),
            "declared_hours": sum(item["declared_duration_s"] or 0 for item in items) / 3600,
            "disk_free_bytes": shutil.disk_usage(disk).free,
            "capacity_kind": "source_catalog_requires_measured_rgb_imu"}


def inventory(root=None, query="", state="all", limit=50, offset=0):
    if (not isinstance(query, str) or len(query) > 200
            or state not in {"all", "missing", "cataloged", "downloaded", "measured", "partial", "no_annotations"}
            or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0):
        raise ValueError("Filtros Nymeria inválidos.")
    base = _root(root)
    sequences = _load(base)["sequences"]
    items = sorted(_inventory_items(base, sequences), key=lambda item: item["seq_id"])
    terms = query.casefold().split()
    items = [item for item in items if (state == "all" or item["state"] == state)
             and all(term in (item["seq_id"] + " " + item["script"]).casefold() for term in terms)]
    return {"total": len(items), "offset": offset, "limit": limit, "items": items[offset:offset + limit]}


def _catalog_label_rules(events, rules):
    """Skip rules whose unit evidence is absent; keep every possible label.

    Unit thresholds may be lower than whole-clip thresholds. Search the exact
    normalized wearer segments used by label_span_events, including their joins.
    A group absent from this union cannot occur in any individual event.
    """
    block = " ".join(event[2] for event in events if not event[3])
    present = {}
    possible = []
    for name, rule in rules:
        if not rule.evidence:
            continue
        required = rule.unit_min_evidence_groups
        if required is None:
            required = rule.min_evidence_groups
        if required is None:
            required = len(rule.evidence)
        count = 0
        for group in rule.evidence:
            if group not in present:
                present[group] = task_matching._evidence_group_present(block, group)
            count += present[group]
        if count >= required:
            possible.append((name, rule))
    return tuple(possible)


def _catalog_windows(rows, names, minimum, maximum, cache_key=None, *, rules=None, rivals=None):
    """Apply the same action rules, explicitly without claiming sensor coverage."""
    # No action can produce a take longer than its annotation component.
    # Check this before classifying every event against every task.
    origin = rows[0][0]
    components = []
    for start, end, _text in rows:
        a, b = start - origin, end - origin
        if components and a <= components[-1][1] + 1e-6:
            components[-1] = (components[-1][0], max(components[-1][1], b))
        else:
            components.append((a, b))
    if not any(upper - lower >= minimum for lower, upper in components):
        return []
    # Classification is independent of the requested duration, including when
    # no window passed the first range. Bind it to annotation/rule content and
    # the requested names, rather than reclassifying all rejected recordings.
    input_key = (cache_key, tuple(names)) if cache_key is not None else None
    previous = _CATALOG_INPUT_CACHE.get(input_key) if input_key is not None else None
    cached = _EVENT_CACHE.get(cache_key) if cache_key is not None else None
    lookup = nymeria.selection_rule_for if rules is None else rules.get
    if previous is not None:
        relative, names, events, selected_labels = previous
    else:
        relative = [(a - origin, b - origin, nymeria.selection_text(text)) for a, b, text in rows]
        block = task_matching.span_search_text((a, text) for a, _b, text in relative)
        names = [name for name in names if lookup(name) is not None
                 and task_matching.span_evidence_possible(lookup(name), block)]
        if not names:
            events, selected_labels = (), ()
        elif cached is not None:
            events, selected_labels = cached
        else:
            events = task_matching.prepare_span_events((a, text) for a, _b, text in relative)
            selected_labels = task_matching.label_span_events(events,
                _catalog_label_rules(events, ((name, lookup(name)) for name in names)))
        if input_key is not None:
            if len(_CATALOG_INPUT_CACHE) >= 2048:
                _CATALOG_INPUT_CACHE.clear()
            _CATALOG_INPUT_CACHE[input_key] = (relative, tuple(names), events, selected_labels)
    if not names:
        return []
    if cached is not None:
        events, selected_labels = cached
    # A five-minute core needs on-task observations at least five minutes
    # apart. This conservative prefilter avoids classifying all rival tasks
    # for recordings that cannot possibly contain a qualifying requested core.
    possible = []
    for name in names:
        times = [event[0] for event, labels in zip(events, selected_labels) if name in labels]
        if times and max(times) - min(times) >= max(minimum, lookup(name).min_span_s or 0):
            possible.append(name)
    if not possible:
        return []
    names = possible
    if cached is None:
        labels = task_matching.label_span_events(events, _catalog_label_rules(events,
            (nymeria.selection_rules() if rules is None else rules).items()))
        if cache_key is not None:
            if len(_EVENT_CACHE) >= 2048:
                _EVENT_CACHE.clear()
            _EVENT_CACHE[cache_key] = (events, labels)
    else:
        labels = selected_labels
    windows = []
    for name in names:
        rule = lookup(name)
        if rule is None:
            continue
        min_span = max(minimum, rule.min_span_s or 0)
        if min_span > maximum:
            continue
        competing = (task_matching.competing_span_names(name, task_matching.TASK_RULES.items())
                     if rivals is None else rivals[name])
        for lower, upper in components:
            if upper - lower < min_span:
                continue
            pairs = [(event, label) for event, label in zip(events, labels) if lower <= event[0] <= upper]
            for span in task_matching.extract_spans(rule, (), min_s=min_span, max_s=maximum,
                    prepared_events=tuple(event for event, _label in pairs),
                    event_task_names=tuple(label for _event, label in pairs), task_name=name,
                    competing_task_names=competing, video_duration_s=upper,
                    activity_mode=nymeria.selection_activity_mode(name)):
                start, end = max(lower, span["start"]), min(upper, span["end"])
                if (min_span <= end - start <= maximum
                        and nymeria.selection_window_complete(name, relative, start, end)):
                    windows.append({"task_name": name, "device_seconds": [start + origin, end + origin],
                                    "duration_s": end - start})
    return windows


def _union_seconds(windows):
    intervals = sorted(window["device_seconds"] for window in windows)
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return sum(end - start for start, end in merged)


def plan_expansion(task_names, min_dur_s=300, max_dur_s=1800, target_seconds=28800, root=None,
                   progress=None, should_stop=None, min_free_bytes=50 * 1024**3):
    if (not isinstance(task_names, (list, tuple)) or not task_names
            or any(not isinstance(name, str) for name in task_names)):
        raise ValueError("Categorias Nymeria inválidas.")
    minimum, maximum = nymeria._bounds(min_dur_s, max_dur_s)
    target = nymeria._finite(target_seconds)
    if target <= 0:
        raise ValueError("Meta Nymeria inválida.")
    if type(min_free_bytes) is not int or min_free_bytes < 0:
        raise ValueError("Reserva de espaço Nymeria inválida.")
    names = list(dict.fromkeys(task_matching.canonical_task_name(name) for name in task_names))
    base = _root(root)
    sequences = _load(base)["sequences"]
    candidates, per_task = [], {name: 0.0 for name in names}
    rules_digest = nymeria._rules_digest()
    for index, (sid, groups) in enumerate(sequences.items(), 1):
        _check_stop(should_stop)
        if progress:
            progress(f"Analisando ações Nymeria: {index}/{len(sequences)} sequências")
        try:
            rows, hashes = nymeria._annotation_rows(_path(base, sid))
        except (OSError, ValueError):
            continue
        evidence_key = (tuple(sorted(hashes.items())), rules_digest)
        key = (evidence_key, tuple(names), minimum, maximum)
        windows = _PLAN_CACHE.get(key)
        if windows is None:
            windows = _catalog_windows(rows, names, minimum, maximum, evidence_key)
            if len(_PLAN_CACHE) >= 4096:
                _PLAN_CACHE.clear()
            _PLAN_CACHE[key] = windows
        if not windows:
            continue
        for name in names:
            per_task[name] += _union_seconds([window for window in windows if window["task_name"] == name])
        item = _sequence_item(base, sid, groups)
        item.update(annotation_potential_seconds=_union_seconds(windows), windows=windows)
        candidates.append(item)
    candidates.sort(key=lambda item: (item["download_bytes"] / item["annotation_potential_seconds"], item["seq_id"]))
    selected, seconds, download_bytes = [], 0.0, 0
    for item in candidates:
        if seconds >= target:
            break
        selected.append(item["seq_id"])
        seconds += item["annotation_potential_seconds"]
        download_bytes += item["download_bytes"]
    disk = summary(base)["disk_free_bytes"]
    extraction = {sid: _extraction_bytes(base, sid, sequences[sid]) for sid in selected}
    unknown = [sid for sid, size in extraction.items() if size is None]
    working = sum(size or 0 for size in extraction.values())
    return {"capacity_kind": "atomic_annotation_potential_requires_measured_rgb_imu",
            "target_seconds": target, "potential_seconds": sum(item["annotation_potential_seconds"] for item in candidates),
            "per_task": [{"task_name": name, "annotation_potential_seconds": seconds} for name, seconds in per_task.items()],
            "sequence_count": len(candidates), "candidates": candidates, "selected_seq_ids": selected,
            "selected_potential_seconds": seconds, "download_bytes": download_bytes,
            "disk_free_bytes": disk, "min_free_bytes": min_free_bytes, "working_bytes": working,
            "extraction_bytes": working, "unknown_extraction_seq_ids": unknown,
            "space_check_complete": not unknown,
            "fits_source_downloads": download_bytes + min_free_bytes <= disk,
            "fits_available_disk": None if unknown else download_bytes + working + min_free_bytes <= disk,
            "target_found_in_annotations": seconds >= target, "campaign_ready": False}


def acquire_sequences(seq_ids, root=None, progress=None, should_stop=None, min_free_bytes=50 * 1024**3):
    """Download selected originals, then validate the real SDK clocks/annotations."""
    if (not isinstance(seq_ids, (list, tuple)) or not seq_ids
            or any(not isinstance(sid, str) or not _IDENTITY.fullmatch(sid) for sid in seq_ids)):
        raise ValueError("Seleção Nymeria inválida.")
    if type(min_free_bytes) is not int or min_free_bytes < 0:
        raise ValueError("Reserva de espaço Nymeria inválida.")
    base = _root(root)
    with _LOCK:
        sequences = _load(base)["sequences"]
        if any(sid not in sequences for sid in seq_ids):
            raise ValueError("Sequência Nymeria fora do manifesto importado.")
        ids = list(dict.fromkeys(seq_ids))
        required = sum(_sequence_item(base, sid, sequences[sid])["download_bytes"] for sid in ids)
        if summary(base)["disk_free_bytes"] < required + min_free_bytes:
            raise NymeriaLibraryError("Espaço insuficiente para as fontes Nymeria selecionadas e a reserva livre. "
                "Nenhum VRS grande foi baixado.", "source_space_insufficient")
        extraction = 0
        for sid in ids:
            _check_stop(should_stop)
            if progress:
                progress(f"Conferindo espaço de extração do IMU Nymeria: {sid}")
            extraction += _extraction_bytes(base, sid, sequences[sid], fetch=True, should_stop=should_stop)
        # Every retained ZIP and every retained extracted head IMU count in this
        # quote, rather than counting just the largest archive in the batch.
        free = summary(base)["disk_free_bytes"]
        if free < required + extraction + min_free_bytes:
            raise NymeriaLibraryError("Espaço insuficiente para todas as fontes, todos os IMUs extraídos "
                "e a reserva livre. Nenhum VRS grande foi baixado.", "extraction_space_insufficient")
        results = []
        for sid in ids:
            _check_stop(should_stop)
            groups = sequences[sid]
            if progress:
                progress(f"Preparando fontes Nymeria: {sid}")
            _sync_sequence(base, sid, groups, should_stop)
            motion = _download(groups["timesync_and_imu"],
                               _path(base, "_catalog", "archives", sid, "timesync_and_imu.zip"), base,
                               progress, should_stop, min_free_bytes)
            _extract(motion, base, sid, "recording_head/", should_stop, min_free_bytes)
            _download(groups["recording_head_data_data_vrs"],
                      _path(base, sid, "recording_head", "data", "data.vrs"), base,
                      progress, should_stop, min_free_bytes)
            _check_stop(should_stop)
            nymeria.clear_caches()
            if progress:
                progress({"message": f"Verificando vídeo e IMU medidos Nymeria: {sid}", "sequence_id": sid})
            try:
                snap = nymeria._snapshot(_path(base, sid), fresh=True)
            except (OSError, ValueError, RuntimeError, ImportError, UnicodeError) as exc:
                detail = str(exc)
                if "relógio de narração" in detail:
                    code, check = "narration_clock_unbound", "o relógio das narrações não coincide com os frames RGB originais"
                elif "dispositivos diferentes" in detail:
                    code, check = "source_devices_mismatch", "RGB e IMU pertencem a aparelhos diferentes"
                elif "timestamp" in detail or "DEVICE_TIME" in detail:
                    code, check = "source_timestamps_invalid", "os timestamps originais não passaram na verificação"
                elif "SDK" in detail or isinstance(exc, ImportError):
                    code, check = "source_sdk_unavailable", "o SDK não conseguiu ler os sensores originais"
                else:
                    code, check = "source_measurement_invalid", "a cobertura medida de RGB, IMU e narrações não passou na verificação"
                raise NymeriaLibraryError(f"Sequência {sid}: {check}. Fontes preservadas; nenhum envio iniciado.", code) from exc
            result = {"seq_id": sid, "state": "measured", "selection_ready": True,
                      "measured_duration_s": snap["duration_s"],
                      "imu_sample_count": snap["measured"]["imu_sample_count"]}
            sources = [list(nymeria._stat(_path(base, sid, *name.split("/")))) for name in
                       ("metadata.json", "recording_head/data/data.vrs", "recording_head/data/motion.vrs",
                        *("narration/" + name for name in nymeria._ANNOTATIONS))]
            _write_json(_path(base, "_catalog", "measured", sid + ".json"),
                        {**result, "source_stats": sources, "rules_sha256": nymeria._rules_digest(),
                         "sdk_signature": snap["proof"]["sdk_signature"],
                         "asset_identity": _asset_identity(groups)})
            results.append(result)
        _PLAN_CACHE.clear()
        _INVENTORY_CACHE.clear()
        return {"ok": True, "sequences": results, "summary": summary(base),
                "space_quote": {"source_download_bytes": required, "extraction_bytes": extraction,
                                "min_free_bytes": min_free_bytes, "disk_free_bytes": free}}


def ensure_sequence_for_campaign(clip, *, allow_download=True, progress=None,
                                 should_stop=None, min_free_bytes=50 * 1024**3):
    """Acquire exactly the current planned recording through the library path."""
    if not isinstance(clip, dict) or type(allow_download) is not bool:
        raise ValueError('Seleção Nymeria inválida.')
    sid = clip.get('seq_id')
    if not isinstance(sid, str) or not _IDENTITY.fullmatch(sid):
        raise ValueError('Identidade de sequência Nymeria inválida.')
    supplied = clip.get('path')
    if supplied is not None and (not isinstance(supplied, str) or not supplied):
        raise ValueError('Caminho da sequência Nymeria inválido.')
    base = _root(Path(supplied).parent if supplied else None)
    directory = _path(base, sid)
    if supplied and Path(supplied).absolute() != directory.absolute():
        raise ValueError('Caminho da sequência Nymeria diverge da identidade.')
    if not allow_download:
        if not all(_path(base, sid, 'recording_head', 'data', name).is_file()
                   for name in ('data.vrs', 'motion.vrs')):
            raise NymeriaLibraryError('Fontes locais Nymeria ausentes; download está desativado.',
                                      'source_download_disabled')
        return directory
    acquire_sequences([sid], root=base, progress=progress, should_stop=should_stop,
                      min_free_bytes=min_free_bytes)
    return directory


@cleanup_operation
def cleanup_sequence_sources(seq_id, root=None, *, protected_paths=(), protection=None):
    """Release a finished recording's owned bulk sources, retaining its catalog.

    The campaign calls this only after its whole account batch is confirmed.
    Journal hashes and explicit consumers independently keep each needed source.
    A later window reacquires the original recording through acquire_sequences.
    """
    if not isinstance(seq_id, str) or not _IDENTITY.fullmatch(seq_id):
        raise ValueError('Identidade de sequência Nymeria inválida.')
    base = _root(root)
    paths = [_path(base, seq_id, 'recording_head', 'data', name)
             for name in ('data.vrs', 'motion.vrs')]
    paths.append(_path(base, '_catalog', 'archives', seq_id, 'timesync_and_imu.zip'))
    result = cleanup_managed_media(paths, allowed_roots=(base,),
                                   protected_paths=protected_paths, protection=protection)
    if result['removed_paths']:
        measured = _path(base, '_catalog', 'measured', seq_id + '.json')
        # This is an app observation bound to the removed VRS, not a source
        # narration/index. It cannot keep advertising readiness after eviction.
        if measured.is_file():
            try:
                size = measured.stat().st_size
                measured.unlink()
                result['files'] += 1
                result['bytes'] += size
            except OSError as exc:
                result['errors'].append(f'{measured.name}: {type(exc).__name__}')
        _PLAN_CACHE.clear()
        _INVENTORY_CACHE.clear()
        nymeria.clear_caches()
    result['seq_id'] = seq_id
    return result
