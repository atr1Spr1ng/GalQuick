from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from hgalgame.engines.advhd.archive import AdvHdArcReader
from hgalgame.engines.advhd.ws2_parser import Ws2Parser


class AdvHdSceneIndexer:
    """Build a deterministic script/event index from an AdvHD game directory."""

    def __init__(self) -> None:
        self.archive_reader = AdvHdArcReader()
        self.parser = Ws2Parser()

    def build(self, game_dir: Path, include_events: bool = False) -> dict[str, Any]:
        game_dir = game_dir.resolve()
        archive_paths = sorted(game_dir.glob("Rio*.arc"))
        manifests = {
            archive_path.name: self.archive_reader.read_manifest(archive_path)
            for archive_path in archive_paths
        }
        all_ws2 = {
            Path(entry.name).stem.casefold(): {
                "archive": archive_name,
                "entry": entry.name,
            }
            for archive_name, manifest in manifests.items()
            for entry in manifest.entries
            if entry.name.casefold().endswith(".ws2")
        }

        story_archive_path = self._select_story_archive(archive_paths, manifests)
        story_manifest = manifests[story_archive_path.name]
        scripts: list[dict[str, Any]] = []
        event_totals: Counter[str] = Counter()
        edges: list[dict[str, Any]] = []
        unresolved_targets: Counter[str] = Counter()

        for entry in story_manifest.entries:
            if not entry.name.casefold().endswith(".ws2"):
                continue
            result = self.parser.parse(
                entry.name,
                self.archive_reader.read_entry(story_archive_path, entry.name),
            )
            event_totals.update(result.event_counts)
            script_record = result.to_dict(include_events=include_events)
            script_record["archive"] = story_archive_path.name
            script_record["event_cg_assets"] = sorted(
                {
                    event.data["asset"]
                    for event in result.events
                    if event.type == "image"
                    and event.data.get("role") == "event_cg_candidate"
                }
            )
            scripts.append(script_record)
            source = Path(entry.name).stem

            for event in result.events:
                if event.type == "script_jump":
                    target = event.data["target"]
                    edges.append(
                        self._edge(source, target, "script_jump", event.offset, all_ws2)
                    )
                    if target.casefold() not in all_ws2:
                        unresolved_targets[target] += 1
                elif event.type == "choice":
                    for option in event.data["options"]:
                        target = option["target"]
                        if target["type"] != "script":
                            continue
                        target_name = target["script"]
                        edge = self._edge(
                            source, target_name, "choice", event.offset, all_ws2
                        )
                        edge["option_id"] = option["id"]
                        edge["option_text"] = option["text"]
                        edges.append(edge)
                        if target_name.casefold() not in all_ws2:
                            unresolved_targets[target_name] += 1

        return {
            "game_dir": str(game_dir),
            "read_only": True,
            "engine": "advhd",
            "format_version": self.parser.FORMAT_VERSION,
            "story_archive": story_archive_path.name,
            "archives": [
                {
                    "name": name,
                    "entry_count": manifest.entry_count,
                    "ws2_entry_count": sum(
                        entry.name.casefold().endswith(".ws2")
                        for entry in manifest.entries
                    ),
                }
                for name, manifest in manifests.items()
            ],
            "summary": {
                "script_count": len(scripts),
                "instruction_count": sum(
                    script["instruction_count"] for script in scripts
                ),
                "event_count": sum(event_totals.values()),
                "event_counts": dict(event_totals),
                "edge_count": len(edges),
                "resolved_edge_count": sum(edge["resolved"] for edge in edges),
                "unresolved_targets": dict(unresolved_targets),
                "event_cg_script_count": sum(
                    bool(script["event_cg_assets"]) for script in scripts
                ),
            },
            "scripts": scripts,
            "edges": edges,
        }

    def _select_story_archive(self, archive_paths, manifests) -> Path:
        candidates = []
        for path in archive_paths:
            count = sum(
                entry.name.casefold().startswith("sce_")
                and entry.name.casefold().endswith(".ws2")
                for entry in manifests[path.name].entries
            )
            candidates.append((count, path.name.casefold(), path))
        if not candidates or max(candidates)[0] == 0:
            raise ValueError("no Rio archive contains sce_*.ws2 story entries")
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    def _edge(
        self,
        source: str,
        target: str,
        edge_type: str,
        offset: int,
        all_ws2: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        resolved = all_ws2.get(target.casefold())
        return {
            "type": edge_type,
            "source": source,
            "target": target,
            "offset": offset,
            "resolved": resolved is not None,
            "target_archive": resolved["archive"] if resolved else None,
            "target_entry": resolved["entry"] if resolved else None,
        }
