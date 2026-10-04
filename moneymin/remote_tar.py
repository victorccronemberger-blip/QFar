"""Leitura seletiva de arquivos TAR remotos com suporte a HTTP Range.

O HoloAssist publica modalidades em TARs enormes. Baixar 150 GB para obter uma
única sessão é desnecessário: o TAR não é comprimido, então seus cabeçalhos e o
conteúdo de um membro podem ser buscados pelos offsets exatos.
"""
from __future__ import annotations

import http.client
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from . import tls
from .atomic_io import load_json, save_json

# A leitura salta o conteúdo dos membros grandes usando HTTP Range. Um bloco
# pequeno evita transferir 256 KiB só para ler cada cabeçalho de 512 bytes.
_BLOCK = 16 * 1024
_COPY_BLOCK = 4 * 1024 * 1024


@dataclass(frozen=True)
class TarMember:
    name: str
    offset: int
    size: int
    typeflag: str


def _octal(raw: bytes) -> int:
    value = raw.rstrip(b"\0 ").lstrip(b" ")
    if not value:
        return 0
    if value[0] & 0x80:  # extensão base-256 do formato TAR
        return int.from_bytes(value, "big", signed=True)
    return int(value, 8)


def parse_header(header: bytes, position: int) -> TarMember | None:
    """Interpreta um cabeçalho TAR de 512 bytes."""
    if len(header) < 512:
        raise EOFError("cabeçalho TAR incompleto")
    if not header.strip(b"\0"):
        return None
    name = header[:100].rstrip(b"\0").decode("utf-8", "replace")
    prefix = header[345:500].rstrip(b"\0").decode("utf-8", "replace")
    if prefix:
        name = f"{prefix}/{name}"
    return TarMember(
        name=name,
        offset=position + 512,
        size=_octal(header[124:136]),
        typeflag=(header[156:157] or b"0").decode("ascii", "replace") or "0",
    )


class HttpRangeReader:
    """Conexão HTTPS persistente para leituras posicionais."""

    def __init__(self, url: str, timeout: int = 120) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("somente URLs HTTPS são aceitas")
        self.url = url
        self._host = parsed.hostname
        self._path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self._timeout = timeout
        self._connection: http.client.HTTPSConnection | None = None
        self.size, self.etag = self._head()

    def _connect(self) -> http.client.HTTPSConnection:
        if self._connection is None:
            self._connection = http.client.HTTPSConnection(
                self._host, timeout=self._timeout, context=tls.context())
        return self._connection

    def _head(self) -> tuple[int, str]:
        connection = self._connect()
        connection.request("HEAD", self._path)
        response = connection.getresponse()
        response.read()
        if response.status != 200:
            raise OSError(f"HEAD do TAR falhou: HTTP {response.status}")
        if "bytes" not in str(response.getheader("Accept-Ranges") or "").lower():
            raise OSError("servidor não oferece HTTP Range")
        return int(response.getheader("Content-Length") or 0), str(response.getheader("ETag") or "")

    def read(self, start: int, length: int) -> bytes:
        if start < 0 or length < 0:
            raise ValueError("range negativo")
        if length == 0 or start >= self.size:
            return b""
        end = min(self.size - 1, start + length - 1)
        for attempt in range(2):
            try:
                connection = self._connect()
                connection.request("GET", self._path, headers={"Range": f"bytes={start}-{end}"})
                response = connection.getresponse()
                body = response.read()
                if response.status != 206:
                    raise OSError(f"Range do TAR falhou: HTTP {response.status}")
                if len(body) != end - start + 1:
                    raise OSError("Range do TAR retornou tamanho incorreto")
                return body
            except (OSError, http.client.HTTPException):
                self.close()
                if attempt:
                    raise
        raise AssertionError("inalcançável")

    def copy(
        self,
        member: TarMember,
        output: BinaryIO,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        remaining = member.size
        position = member.offset
        copied = 0
        while remaining:
            block = self.read(position, min(_COPY_BLOCK, remaining))
            if not block:
                raise EOFError(f"membro TAR truncado: {member.name}")
            output.write(block)
            position += len(block)
            remaining -= len(block)
            copied += len(block)
            if progress:
                progress(copied, member.size)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> HttpRangeReader:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


class RemoteTar:
    """Índice incremental e extração seletiva de um TAR remoto."""

    def __init__(self, url: str, index_path: Path) -> None:
        self.url = url
        self.index_path = index_path

    @staticmethod
    def _next_position(member: TarMember) -> int:
        return member.offset + math.ceil(member.size / 512) * 512

    def _load_index(self, etag: str, archive_size: int) -> tuple[int, dict[str, TarMember]]:
        raw = load_json(self.index_path, {})
        if not isinstance(raw, dict) or raw.get("url") != self.url or raw.get("etag") != etag:
            return 0, {}
        cursor, records = raw.get("cursor"), raw.get("members")
        if (type(cursor) is not int or not 0 <= cursor <= archive_size or cursor % 512
                or not isinstance(records, dict) or (cursor > 0 and not records)):
            return 0, {}
        # Legacy public seeds do not have a stored archive size. Their bounds
        # still use the current reader size and selected header verification.
        if "size" in raw and (type(raw["size"]) is not int or raw["size"] != archive_size):
            return 0, {}
        members = {}
        for name, value in records.items():
            if (not isinstance(name, str) or not name or not isinstance(value, dict)
                    or set(value) != {"name", "offset", "size", "typeflag"}
                    or value["name"] != name or not isinstance(value["name"], str)
                    or type(value["offset"]) is not int or value["offset"] < 512
                    or value["offset"] % 512 or type(value["size"]) is not int
                    or value["size"] < 0 or value["offset"] + value["size"] > archive_size
                    or not isinstance(value["typeflag"], str) or len(value["typeflag"]) != 1):
                return 0, {}
            members[name] = TarMember(**value)
        # Zero deliberately restarts scanning. A positive resume cursor cannot
        # point inside the header/payload/padding of a member already known.
        if cursor and any(cursor < member.offset + ((member.size + 511) // 512) * 512
                          for member in members.values()):
            return 0, {}
        return cursor, members

    @staticmethod
    def _selected_header_matches(reader: HttpRangeReader, member: TarMember) -> bool:
        position = member.offset - 512
        header = reader.read(position, 512)
        try:
            actual = parse_header(header, position)
        except ValueError:
            # A cached offset may point into payload rather than a TAR header.
            return False
        if actual is None:
            return False
        regular = {"0", "\0"}
        return (actual.name == member.name and actual.offset == member.offset
                and actual.size == member.size and (actual.typeflag == member.typeflag
                    or actual.typeflag in regular and member.typeflag in regular))

    def _save_index(self, etag: str, cursor: int, members: dict[str, TarMember], archive_size: int) -> None:
        save_json(
            self.index_path,
            {
                "url": self.url,
                "etag": etag,
                "size": archive_size,
                "cursor": cursor,
                "members": {name: asdict(member) for name, member in members.items()},
            },
        )

    def build_index(
        self,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Completa uma única vez o índice de offsets, sem baixar os membros.

        O cursor e os membros são persistidos no JSON; chamadas posteriores
        retomam do último checkpoint e terminam imediatamente quando completo.
        """
        try:
            self.find(lambda _member: False, progress=progress)
        except FileNotFoundError:
            pass
        raw = load_json(self.index_path, {})
        members = raw.get("members") if isinstance(raw, dict) else None
        return len(members) if isinstance(members, dict) else 0

    def find(
        self,
        predicate: Callable[[TarMember], bool],
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> TarMember:
        """Localiza um membro, retomando do último cabeçalho já indexado."""
        with HttpRangeReader(self.url) as reader:
            cursor, members = self._load_index(reader.etag, reader.size)
            for member in members.values():
                if predicate(member):
                    if self._selected_header_matches(reader, member):
                        return member
                    cursor, members = 0, {}
                    break

            block_start = -1
            block = b""
            scanned = 0
            while cursor < reader.size:
                if not (block_start <= cursor and cursor + 512 <= block_start + len(block)):
                    block_start = cursor
                    block = reader.read(cursor, _BLOCK)
                local = cursor - block_start
                member = parse_header(block[local : local + 512], cursor)
                if member is None:
                    self._save_index(reader.etag, cursor, members, reader.size)
                    break
                cursor = self._next_position(member)
                # Cabeçalhos PAX têm nome sintético e não são dados do usuário.
                if member.typeflag not in ("x", "g"):
                    members[member.name] = member
                    if predicate(member):
                        self._save_index(reader.etag, cursor, members, reader.size)
                        return member
                scanned += 1
                if progress and scanned % 100 == 0:
                    progress(cursor, reader.size)
                if scanned % 500 == 0:
                    self._save_index(reader.etag, cursor, members, reader.size)
        raise FileNotFoundError("membro não encontrado no TAR remoto")

    def extract(
        self,
        member: TarMember,
        destination: Path,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        with HttpRangeReader(self.url) as reader, temporary.open("wb") as output:
            reader.copy(member, output, progress=progress)
        if temporary.stat().st_size != member.size:
            raise OSError("extração remota terminou com tamanho incorreto")
        temporary.replace(destination)
        return destination


__all__ = ["HttpRangeReader", "RemoteTar", "TarMember", "parse_header"]
