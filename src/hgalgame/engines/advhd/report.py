from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from hgalgame.engines.advhd.adapter import AdvHdAdapter
from hgalgame.engines.advhd.archive import AdvHdArcReader
from hgalgame.engines.advhd.ws2_reader import Ws2Reader


class AdvHdReportBuilder:
    """Build a read-only, game-level AdvHD inspection report."""

    def build(self, game_dir: Path) -> dict[str, Any]:
        game_dir = game_dir.resolve()
        detector = AdvHdAdapter().detect(game_dir)
        archive_reader = AdvHdArcReader()
        ws2_reader = Ws2Reader()
        archives: list[dict[str, Any]] = []
        statuses: Counter[str] = Counter()
        transforms: Counter[str] = Counter()
        categories: Counter[str] = Counter()
        undetermined: list[str] = []

        for archive_path in sorted(game_dir.glob("Rio*.arc")):
            manifest = archive_reader.read_manifest(archive_path)
            ws2_entries = [
                entry
                for entry in manifest.entries
                if entry.name.casefold().endswith(".ws2")
            ]
            archives.append(
                {
                    "name": archive_path.name,
                    "entry_count": manifest.entry_count,
                    "ws2_entry_count": len(ws2_entries),
                    "data_offset": manifest.data_offset,
                }
            )
            for entry in ws2_entries:
                payload = archive_reader.read_entry(archive_path, entry.name)
                probe = ws2_reader.probe(entry.name, payload)
                statuses[probe.status] += 1
                transforms[probe.transform] += 1
                if probe.status != "recognized":
                    category = "undetermined"
                    undetermined.append(f"{archive_path.name}:{entry.name}")
                elif probe.marker_counts["char"]:
                    category = "dialogue_candidate"
                else:
                    category = "system_script"
                categories[category] += 1

        return {
            "game_dir": str(game_dir),
            "read_only": True,
            "detection": detector.to_dict(),
            "archives": archives,
            "ws2_summary": {
                "total": sum(statuses.values()),
                "status_counts": dict(statuses),
                "transform_counts": dict(transforms),
                "category_counts": dict(categories),
                "undetermined_entries": undetermined,
            },
        }

