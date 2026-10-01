from __future__ import annotations

import struct
from pathlib import Path

from hgalgame.models import ArchiveEntry, ArchiveManifest


class AdvHdFormatError(ValueError):
    """Raised when a file does not match the supported AdvHD ARC layout."""


class AdvHdArcReader:
    """Read AdvHD ARC metadata and individual entries without extracting them."""

    MAX_ENTRIES = 1_000_000
    MAX_NAME_BYTES = 32_768

    def read_manifest(self, path: Path) -> ArchiveManifest:
        path = path.resolve()
        file_size = path.stat().st_size

        with path.open("rb") as stream:
            header = stream.read(16)
            if len(header) != 16:
                raise AdvHdFormatError("archive header is shorter than 16 bytes")

            entry_count, table_size, first_size, first_relative_offset = struct.unpack(
                "<4I", header
            )
            # The table begins at offset 8. Its first record's size/offset pair
            # occupies header bytes 8..15, followed by its UTF-16LE name.
            data_offset = 8 + table_size
            if not 0 < entry_count <= self.MAX_ENTRIES:
                raise AdvHdFormatError(f"unreasonable entry count: {entry_count}")
            if data_offset < 16 or data_offset > file_size:
                raise AdvHdFormatError(f"invalid data offset: {data_offset}")

            entries: list[ArchiveEntry] = []
            size = first_size
            relative_offset = first_relative_offset
            for index in range(entry_count):
                name = self._read_utf16le_z(stream, data_offset)
                absolute_offset = data_offset + relative_offset
                if absolute_offset > file_size or size > file_size - absolute_offset:
                    raise AdvHdFormatError(
                        f"entry {index} points outside archive: {name!r}"
                    )
                entries.append(
                    ArchiveEntry(name, size, relative_offset, absolute_offset)
                )

                if index + 1 < entry_count:
                    raw = stream.read(8)
                    if len(raw) != 8:
                        raise AdvHdFormatError(
                            f"archive directory ended before entry {index + 1}"
                        )
                    size, relative_offset = struct.unpack("<2I", raw)

            if stream.tell() != data_offset:
                raise AdvHdFormatError(
                    f"directory ends at {stream.tell()}, expected {data_offset}"
                )

        return ArchiveManifest(path, entry_count, data_offset, entries)

    def read_entry(self, path: Path, entry_name: str) -> bytes:
        """Return one named entry in memory; no file is written to disk."""
        manifest = self.read_manifest(path)
        matches = [entry for entry in manifest.entries if entry.name == entry_name]
        if not matches:
            raise KeyError(f"archive entry not found: {entry_name}")
        if len(matches) > 1:
            raise AdvHdFormatError(f"duplicate archive entry: {entry_name}")
        entry = matches[0]
        with manifest.path.open("rb") as stream:
            stream.seek(entry.absolute_offset)
            data = stream.read(entry.size)
        if len(data) != entry.size:
            raise AdvHdFormatError(f"short read for archive entry: {entry_name}")
        return data

    def _read_utf16le_z(self, stream, directory_end: int) -> str:
        raw = bytearray()
        while stream.tell() < directory_end:
            pair = stream.read(2)
            if len(pair) != 2:
                raise AdvHdFormatError("unterminated UTF-16 filename")
            if pair == b"\x00\x00":
                try:
                    name = raw.decode("utf-16le")
                except UnicodeDecodeError as exc:
                    raise AdvHdFormatError("invalid UTF-16 filename") from exc
                if not name:
                    raise AdvHdFormatError("empty archive entry name")
                return name
            raw.extend(pair)
            if len(raw) > self.MAX_NAME_BYTES:
                raise AdvHdFormatError("archive entry name is too long")
        raise AdvHdFormatError("filename crosses archive directory boundary")
