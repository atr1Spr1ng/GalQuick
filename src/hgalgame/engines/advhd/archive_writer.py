from __future__ import annotations

import os
import struct
import tempfile
from collections.abc import Iterable
from pathlib import Path

from hgalgame.engines.advhd.archive import AdvHdArcReader, AdvHdFormatError


class AdvHdArcWriter:
    """Rebuild small AdvHD ARC files while preserving entry order and names."""

    def build(self, entries: Iterable[tuple[str, bytes]]) -> bytes:
        items = list(entries)
        if not items:
            raise AdvHdFormatError("cannot build an empty AdvHD archive")
        names = [name for name, _ in items]
        if len(set(names)) != len(names):
            raise AdvHdFormatError("cannot build an archive with duplicate entry names")

        encoded_names: list[bytes] = []
        for name in names:
            if not name or "\x00" in name:
                raise AdvHdFormatError(f"invalid archive entry name: {name!r}")
            raw = name.encode("utf-16le") + b"\x00\x00"
            if len(raw) > AdvHdArcReader.MAX_NAME_BYTES:
                raise AdvHdFormatError(f"archive entry name is too long: {name!r}")
            encoded_names.append(raw)

        offsets: list[int] = []
        cursor = 0
        for _, data in items:
            if len(data) > 0xFFFFFFFF or cursor > 0xFFFFFFFF:
                raise AdvHdFormatError("archive data exceeds the 32-bit ARC layout")
            offsets.append(cursor)
            cursor += len(data)
        if cursor > 0xFFFFFFFF:
            raise AdvHdFormatError("archive data exceeds the 32-bit ARC layout")

        table_size = 8 * len(items) + sum(len(name) for name in encoded_names)
        if table_size > 0xFFFFFFFF:
            raise AdvHdFormatError("archive directory exceeds the 32-bit ARC layout")
        output = bytearray(
            struct.pack(
                "<4I", len(items), table_size, len(items[0][1]), offsets[0]
            )
        )
        for index, ((_, data), encoded_name) in enumerate(
            zip(items, encoded_names, strict=True)
        ):
            output.extend(encoded_name)
            if index + 1 < len(items):
                output.extend(
                    struct.pack("<2I", len(items[index + 1][1]), offsets[index + 1])
                )
        for _, data in items:
            output.extend(data)
        return bytes(output)

    def rebuild(
        self,
        archive_path: Path,
        replacements: dict[str, bytes],
        additions: Iterable[tuple[str, bytes]] = (),
    ) -> bytes:
        """Rebuild an archive with audited replacements and optional new entries.

        Existing entries retain their exact order.  Generated helper scripts are
        appended, which keeps engine-owned names and offsets stable while allowing
        a runtime adapter to add a small controller without sacrificing the
        original archive backup.
        """

        reader = AdvHdArcReader()
        manifest = reader.read_manifest(archive_path)
        known = {entry.name for entry in manifest.entries}
        unknown = set(replacements) - known
        if unknown:
            raise KeyError(f"replacement entries not present in archive: {sorted(unknown)}")
        added = list(additions)
        known_casefold = {name.casefold() for name in known}
        added_names = [name for name, _ in added]
        conflicts = sorted(
            name for name in added_names if name.casefold() in known_casefold
        )
        if conflicts:
            raise KeyError(f"additional entries already present in archive: {conflicts}")
        if len({name.casefold() for name in added_names}) != len(added_names):
            raise AdvHdFormatError("cannot add duplicate archive entry names")
        entries = [
            (
                entry.name,
                replacements.get(
                    entry.name, reader.read_entry(archive_path, entry.name)
                ),
            )
            for entry in manifest.entries
        ]
        entries.extend(added)
        return self.build(entries)

    def write_atomic(self, path: Path, payload: bytes) -> Path:
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return path
