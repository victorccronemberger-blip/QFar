"""Inspect explicitly supplied original capture files without modifying them.

Only the standard library is used. The result describes observed bytes and a
bounded structural policy; it does not establish physical acquisition,
ownership, sensor accuracy, Android attestation or provider acceptance.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import stat
import struct
import sys
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_MEMBER_NAME = re.compile(r"[A-Za-z0-9_.-]{1,240}\Z")
_DIGITS = re.compile(r"[0-9]{1,19}\Z")
_INTEGER = re.compile(r"-?[0-9]{1,19}\Z")
_MAX_LONG = (1 << 63) - 1
_READ_SIZE = 1024 * 1024
_MESSAGES = {
    "invalid_arguments": "Informe dois arquivos locais e limites válidos.",
    "unsafe_source": "A origem deve ser um arquivo local regular, sem links.",
    "source_read_failed": "Não foi possível ler os arquivos originais.",
    "source_changed": "A origem mudou durante a leitura; repita com arquivos estáveis.",
    "limit_exceeded": "A captura excede os limites de inspeção.",
    "invalid_archive": "O sidecar ZIP está inválido ou usa um formato não suportado.",
    "unsafe_archive": "O sidecar contém membros ambíguos ou inseguros.",
    "invalid_metadata": "Os metadados não atendem à estrutura de inspeção.",
    "inconsistent_identity": "IDs, sessão, chunk e nomes de artefatos não são consistentes.",
    "invalid_csv": "Os CSVs não atendem à estrutura de inspeção.",
}


class CaptureImportError(ValueError):
    """Fixed public diagnostics never contain source metadata or paths."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_MESSAGES[code])


@dataclass(frozen=True)
class CaptureLimits:
    media_bytes: int = 20 * 1024**3
    archive_bytes: int = 64 * 1024**2
    expanded_bytes: int = 512 * 1024**2
    member_bytes: int = 256 * 1024**2
    metadata_bytes: int = 1024**2
    members: int = 32
    csv_rows: int = 2_000_000
    csv_line_bytes: int = 64 * 1024
    compression_ratio: int = 200


@dataclass(frozen=True)
class CaptureFile:
    path: Path
    bytes: int
    sha256: str


@dataclass(frozen=True)
class CaptureMember:
    name: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class CaptureDescriptor:
    media: CaptureFile
    sidecar: CaptureFile
    members: tuple[CaptureMember, ...]
    metadata: dict[str, Any]
    imu_rows: int
    frames_rows: int

    def public_summary(self) -> dict[str, Any]:
        """No original paths, IDs, member names, metadata or credentials."""
        return {
            "inspection": "original_capture_read_only_v1",
            "validation_policy": "safety_container_identity_csv_types",
            "valid_structure": True,
            "media": {"bytes": self.media.bytes, "sha256": self.media.sha256},
            "sidecar": {"bytes": self.sidecar.bytes, "sha256": self.sidecar.sha256,
                        "members": len(self.members)},
            "metadata_fields": len(self.metadata),
            "imu_rows": self.imu_rows, "frames_rows": self.frames_rows,
            "physical_provenance_verified": False,
            "provider_acceptance_verified": False,
            "media_decoding_verified": False,
            "csv_temporal_consistency_verified": False,
        }


def _fail(code: str) -> None:
    raise CaptureImportError(code) from None


def _fingerprint(value: os.stat_result) -> tuple[int, ...]:
    # CPython 3.12 on Windows can report different st_ctime semantics for
    # stat(path) and fstat(handle). Compare identity, size and last-write time;
    # this is a stability check, not proof against adversarial restamping.
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns)


def _open_source(value: str | Path, limit: int) -> tuple[Path, BinaryIO, os.stat_result]:
    if not isinstance(value, (str, Path)) or not str(value):
        _fail("invalid_arguments")
    if str(value).startswith(("\\\\", "//")):
        _fail("unsafe_source")
    path = Path(value).expanduser().absolute()
    if any(":" in part for part in path.parts[1:]):
        _fail("unsafe_source")  # Windows alternate data streams are not files.
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            _fail("unsafe_source")
    initial = path.stat()
    if not stat.S_ISREG(initial.st_mode):
        _fail("unsafe_source")
    if initial.st_size <= 0 or initial.st_size > limit:
        _fail("limit_exceeded")
    stream = path.open("rb")
    try:
        opened = os.fstat(stream.fileno())
    except OSError:
        stream.close()
        raise
    if _fingerprint(opened) != _fingerprint(initial):
        stream.close()
        _fail("source_changed")
    return path, stream, opened


def _stable(path: Path, stream: BinaryIO, before: os.stat_result) -> None:
    if (_fingerprint(os.fstat(stream.fileno())) != _fingerprint(before)
            or _fingerprint(path.stat()) != _fingerprint(before)):
        _fail("source_changed")


def _file_hash(stream: BinaryIO, limit: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    while data := stream.read(_READ_SIZE):
        total += len(data)
        if total > limit:
            _fail("limit_exceeded")
        digest.update(data)
    return total, digest.hexdigest()


def _preflight_directory(stream: BinaryIO, size: int, limits: CaptureLimits) -> None:
    # Bound the entry count before ZipFile allocates objects for the directory.
    tail_size = min(size, 22 + 65535)
    stream.seek(size - tail_size)
    tail = stream.read(tail_size)
    position = tail.rfind(b"PK\x05\x06")
    while position >= 0:
        if position + 22 <= len(tail):
            values = struct.unpack_from("<4s4H2LH", tail, position)
            if position + 22 + values[-1] == len(tail):
                break
        position = tail.rfind(b"PK\x05\x06", 0, position)
    if position < 0:
        _fail("invalid_archive")
    _, disk, central_disk, disk_count, count, central_size, offset, _ = values
    if disk or central_disk or disk_count != count or count == 0xffff:
        _fail("invalid_archive")  # No split archives or ZIP64 are required here.
    if count < 3 or count > limits.members:
        _fail("limit_exceeded")
    end = size - tail_size + position
    if offset == 0xffffffff or central_size == 0xffffffff or offset + central_size != end:
        _fail("invalid_archive")
    stream.seek(offset)
    observed_count = 0
    while stream.tell() < end:
        header = stream.read(46)
        if len(header) != 46 or not header.startswith(b"PK\x01\x02"):
            _fail("invalid_archive")
        fields = struct.unpack("<4s6H3L5H2L", header)
        observed_count += 1
        if observed_count > limits.members:
            _fail("limit_exceeded")
        if fields[13] or stream.tell() + sum(fields[10:13]) > end:
            _fail("invalid_archive")
        stream.seek(sum(fields[10:13]), 1)
    if observed_count != count:
        _fail("invalid_archive")
    stream.seek(0)


def _archive_members(archive: zipfile.ZipFile, limits: CaptureLimits) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > limits.members:
        _fail("limit_exceeded")
    names: set[str] = set()
    offsets: set[int] = set()
    expanded = 0
    for member in members:
        name = member.filename
        folded = name.casefold()
        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        stem = name.split(".", 1)[0].upper()
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))}
        if (member.orig_filename != name or not _MEMBER_NAME.fullmatch(name) or name in {".", ".."}
                or name.endswith((".", " ")) or stem in reserved
                or folded in names or member.header_offset in offsets
                or member.is_dir() or file_type not in (0, stat.S_IFREG)
                or member.external_attr & 0x400 or member.flag_bits & 0x2041):
            _fail("unsafe_archive")
        if (member.extract_version >= 45
                or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            _fail("invalid_archive")
        names.add(folded)
        offsets.add(member.header_offset)
        expanded += member.file_size
        if (member.file_size > limits.member_bytes or expanded > limits.expanded_bytes
                or member.file_size > max(member.compress_size, 1) * limits.compression_ratio):
            _fail("limit_exceeded")
    return members


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_metadata")
        result[key] = value
    return result


def _nonfinite(_value: str) -> None:
    _fail("invalid_metadata")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _fail("invalid_metadata")
    return number


def _long(value: Any) -> bool:
    return type(value) is int and -_MAX_LONG - 1 <= value <= _MAX_LONG


def _metadata_identity(value: Any, prefix: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("invalid_metadata")
    session, chunk = value.get("session"), value.get("chunk")
    if (not isinstance(session, dict) or not isinstance(chunk, dict)
            or not isinstance(session.get("id"), str)
            or not _IDENTIFIER.fullmatch(session["id"])
            or type(chunk.get("index")) is not int or not 0 <= chunk["index"] < 2**31):
        _fail("invalid_metadata")
    log_id = f"{session['id']}_{chunk['index']}"
    if value.get("id") != log_id or value.get("logId") != log_id or prefix != log_id:
        _fail("inconsistent_identity")
    for key in ("sessionId", "session_id"):
        if key in value and value[key] != session["id"]:
            _fail("inconsistent_identity")
    for key in ("chunkIndex", "chunk_index"):
        if key in value and (type(value[key]) is not int or value[key] != chunk["index"]):
            _fail("inconsistent_identity")
    platform, timebase = value.get("platform"), value.get("timebase")
    if not isinstance(platform, dict) or not isinstance(timebase, dict):
        _fail("invalid_metadata")
    platforms = [platform[key] for key in ("os", "type") if key in platform]
    if (not platforms or any(not isinstance(item, str) or not item.strip() for item in platforms)
            or len(set(platforms)) != 1 or type(platform.get("version")) is not int
            or platform["version"] <= 0
            or not isinstance(timebase.get("clockDomain"), str)
            or not timebase["clockDomain"].strip()):
        _fail("invalid_metadata")
    for key in ("startNs", "endNs"):
        number = timebase.get(key)
        if not isinstance(number, str) or not _DIGITS.fullmatch(number) or int(number) > _MAX_LONG:
            _fail("invalid_metadata")
    for key in ("startTimeMs", "endTimeMs"):
        if not _long(chunk.get(key)):
            _fail("invalid_metadata")
    # Unknown metadata is retained verbatim as parsed JSON; no inferred owner,
    # device model, clock conversion, timestamp or platform is inserted.
    if "artifacts" in value:
        artifacts = value["artifacts"]
        if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
            _fail("invalid_metadata")
        for name in ("imu", "frames"):
            matches = [item for item in artifacts if item.get("name") == name]
            if len(matches) != 1 or matches[0].get("remoteFilename") != f"{log_id}.{name}.csv":
                _fail("inconsistent_identity")
    return value


def _read_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo, limit: int) -> bytes:
    if member.file_size > limit:
        _fail("limit_exceeded")
    with archive.open(member) as stream:
        data = stream.read(limit + 1)
        if len(data) > limit:
            _fail("limit_exceeded")
        if len(data) != member.file_size:
            _fail("invalid_archive")
        return data


def _csv_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo, kind: str,
                limits: CaptureLimits) -> tuple[CaptureMember, int]:
    digest = hashlib.sha256()
    total = 0
    with archive.open(member) as stream:
        def lines():
            nonlocal total
            while raw := stream.readline(limits.csv_line_bytes + 1):
                if len(raw) > limits.csv_line_bytes:
                    _fail("limit_exceeded")
                total += len(raw)
                if total > limits.member_bytes:
                    _fail("limit_exceeded")
                digest.update(raw)
                try:
                    yield raw.decode("utf-8")
                except UnicodeError:
                    _fail("invalid_csv")

        reader = csv.reader(lines(), strict=True)
        expected = (["t", "ax", "ay", "az", "wx", "wy", "wz"] if kind == "imu"
                    else ["i", "ptsNs", "dtNs", "tNs", "key"])
        if next(reader, None) != expected:
            _fail("invalid_csv")
        count = 0
        for row in reader:
            count += 1
            if count > limits.csv_rows:
                _fail("limit_exceeded")
            if len(row) != len(expected):
                _fail("invalid_csv")
            if kind == "imu":
                if not _DIGITS.fullmatch(row[0]) or int(row[0]) > _MAX_LONG:
                    _fail("invalid_csv")
                try:
                    finite = all(math.isfinite(float(item)) for item in row[1:])
                except ValueError:
                    _fail("invalid_csv")
                if not finite:
                    _fail("invalid_csv")
            else:
                if not all(_INTEGER.fullmatch(item) and _long(int(item)) for item in row):
                    _fail("invalid_csv")
                if int(row[0]) < 0 or int(row[3]) < 0 or row[4] not in ("0", "1"):
                    _fail("invalid_csv")
        if not count or total != member.file_size:
            _fail("invalid_csv")
    return CaptureMember(member.filename, total, digest.hexdigest()), count


def inspect_original_capture(media_path: str | Path, sidecar_path: str | Path, *,
                             limits: CaptureLimits | None = None) -> CaptureDescriptor:
    """Read explicit originals; never copy, extract, relabel, upload or save.

    Limits reject ZIP64/split archives and unsupported compression. Structural
    success covers safe container layout, identity links and CSV value types;
    it does not prove media decoding, physical sensor or clock correctness.
    """
    limits = CaptureLimits() if limits is None else limits
    if not isinstance(limits, CaptureLimits) or any(
            type(value) is not int or value <= 0 for value in vars(limits).values()):
        _fail("invalid_arguments")
    opened: list[BinaryIO] = []
    try:
        media, media_stream, media_stat = _open_source(media_path, limits.media_bytes)
        opened.append(media_stream)
        sidecar, sidecar_stream, sidecar_stat = _open_source(sidecar_path, limits.archive_bytes)
        opened.append(sidecar_stream)
        if (media_stat.st_dev, media_stat.st_ino) == (sidecar_stat.st_dev, sidecar_stat.st_ino):
            _fail("invalid_arguments")
        media_size, media_hash = _file_hash(media_stream, limits.media_bytes)
        zip_size, zip_hash = _file_hash(sidecar_stream, limits.archive_bytes)
        _preflight_directory(sidecar_stream, zip_size, limits)
        with zipfile.ZipFile(sidecar_stream) as archive:
            members = _archive_members(archive, limits)
            metadata_members = [item for item in members if item.filename.endswith(".metadata.json")]
            if len(metadata_members) != 1:
                _fail("invalid_metadata")
            metadata_member = metadata_members[0]
            prefix = metadata_member.filename.removesuffix(".metadata.json")
            raw_metadata = _read_member(archive, metadata_member, limits.metadata_bytes)
            metadata = _metadata_identity(json.loads(raw_metadata.decode("utf-8"),
                                                    object_pairs_hook=_unique_pairs,
                                                    parse_float=_finite_float,
                                                    parse_constant=_nonfinite), prefix)
            by_name = {item.filename: item for item in members}
            if any(f"{prefix}.{kind}.csv" not in by_name for kind in ("imu", "frames")):
                _fail("inconsistent_identity")
            imu, imu_rows = _csv_member(archive, by_name[f"{prefix}.imu.csv"], "imu", limits)
            frames, frames_rows = _csv_member(archive, by_name[f"{prefix}.frames.csv"], "frames", limits)
            evidence = [CaptureMember(metadata_member.filename, len(raw_metadata),
                                      hashlib.sha256(raw_metadata).hexdigest()), imu, frames]
            required = {item.name for item in evidence}
            for member in members:
                if member.filename not in required:
                    with archive.open(member) as stream:
                        size, digest = _file_hash(stream, limits.member_bytes)
                    if size != member.file_size:
                        _fail("invalid_archive")
                    evidence.append(CaptureMember(member.filename, size, digest))
        _stable(media, media_stream, media_stat)
        _stable(sidecar, sidecar_stream, sidecar_stat)
        if media_size != media_stat.st_size or zip_size != sidecar_stat.st_size:
            _fail("source_changed")
        return CaptureDescriptor(CaptureFile(media, media_size, media_hash),
                                 CaptureFile(sidecar, zip_size, zip_hash), tuple(evidence),
                                 metadata, imu_rows, frames_rows)
    except CaptureImportError as error:
        raise CaptureImportError(error.code) from None
    except OSError:
        raise CaptureImportError("source_read_failed") from None
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, struct.error, zlib.error):
        raise CaptureImportError("invalid_archive") from None
    except (ValueError, TypeError, UnicodeError, RecursionError, csv.Error, OverflowError):
        raise CaptureImportError("invalid_metadata") from None
    finally:
        pending_failure = sys.exc_info()[0] is not None
        close_failed = False
        for stream in opened:
            try:
                stream.close()
            except Exception:
                close_failed = True
        if close_failed and not pending_failure:
            raise CaptureImportError("source_read_failed") from None


__all__ = ["CaptureImportError", "CaptureLimits", "CaptureFile", "CaptureMember",
           "CaptureDescriptor", "inspect_original_capture"]
