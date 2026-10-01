from __future__ import annotations

from pathlib import Path
from typing import Any

from hgalgame.engines.advhd.archive import AdvHdArcReader, AdvHdFormatError
from hgalgame.models import DetectionResult, Evidence
from hgalgame.story.models import StoryEdge, StoryScene, StorySegment, StorySource


class AdvHdAdapter:
    name = "advhd"

    def detect(self, game_dir: Path) -> DetectionResult:
        game_dir = game_dir.resolve()
        evidence: list[Evidence] = []
        score = 0.0

        if (game_dir / "AdvHD.exe").is_file():
            evidence.append(Evidence("executable", "AdvHD.exe is present"))
            score += 0.45

        arc_paths = sorted(game_dir.glob("*.arc"))
        arc_names = {path.name.casefold() for path in arc_paths}
        found = {"rio.arc", "script.arc"} & arc_names
        if found:
            evidence.append(
                Evidence("archive_names", f"found known archives: {', '.join(sorted(found))}")
            )
            score += 0.15 * len(found)

        ws2_count = 0
        reader = AdvHdArcReader()
        for arc_path in arc_paths:
            if not arc_path.name.casefold().startswith("rio"):
                continue
            try:
                manifest = reader.read_manifest(arc_path)
            except (OSError, AdvHdFormatError):
                continue
            ws2_count += sum(
                entry.name.casefold().endswith(".ws2") for entry in manifest.entries
            )
        if ws2_count:
            evidence.append(
                Evidence("script_entries", f"found {ws2_count} .ws2 entries in Rio archives")
            )
            score += 0.25

        return DetectionResult(
            self.name if score >= 0.5 else "unknown",
            min(score, 1.0),
            tuple(evidence),
        )

    def build_story_source(self, game_dir: Path) -> StorySource:
        """Translate AdvHD-specific ARC/WS2 data into the common story contract."""

        from hgalgame.graph.replay_scene_indexer import AdvHdReplaySceneIndexer
        from hgalgame.graph.scene_indexer import AdvHdSceneIndexer

        game_dir = game_dir.resolve()
        story_index = AdvHdSceneIndexer().build(game_dir, include_events=True)
        replay_index = AdvHdReplaySceneIndexer().build(game_dir, include_events=True)
        segment_names = {
            Path(script["script_name"]).stem.casefold(): script["script_name"]
            for script in story_index["scripts"]
        }

        scene_markers: dict[str, list[dict[str, Any]]] = {}
        scenes: list[StoryScene] = []
        for scene in replay_index["scenes"]:
            start = scene["boundary"]["start"]
            end = scene["boundary"]["end"]
            segment_id = Path(start["script"]).stem.casefold()
            resume_segment_id = Path(end["script"]).stem.casefold()
            replay_id = int(scene["replay_id"])
            marker = {
                "kind": "scene",
                "id": scene["scene_id"],
                "replay_key": replay_id,
                "offset": int(start["offset"]),
                "label": f"Scene {replay_id:02d}",
                "speakers": list(scene["speakers"]),
                "cg_count": len(scene["event_cg_assets"]),
            }
            scene_markers.setdefault(segment_id, []).append(marker)
            scenes.append(
                StoryScene(
                    id=scene["scene_id"],
                    label=marker["label"],
                    segment_id=segment_id,
                    offset=marker["offset"],
                    replay_key=replay_id,
                    speakers=tuple(scene["speakers"]),
                    cg_assets=tuple(
                        asset["asset"] for asset in scene["event_cg_assets"]
                    ),
                    resume_segment_id=resume_segment_id,
                    resume_offset=int(end["resume_offset"]),
                    metadata={
                        "boundary_confidence": scene["boundary"]["confidence"],
                        "dialogue_count": scene["event_counts"].get("dialogue", 0),
                        "script_span_count": len(scene["script_path"]),
                    },
                )
            )

        segments: list[StorySegment] = []
        for order, script in enumerate(story_index["scripts"]):
            segment_id = Path(script["script_name"]).stem.casefold()
            events: list[dict[str, Any]] = []
            for event in script.get("events", []):
                normalized = self._normalize_story_event(event, segment_names)
                if normalized is not None:
                    if normalized["kind"] == "choice":
                        normalized["id"] = f"{segment_id}@{normalized['id']}"
                    events.append(normalized)
            events.extend(scene_markers.get(segment_id, []))
            events.sort(key=lambda event: (int(event.get("offset", 0)), event["kind"] != "scene"))
            segments.append(
                StorySegment(
                    id=segment_id,
                    label=script["script_name"],
                    order=order,
                    events=tuple(events),
                    source={
                        "engine": self.name,
                        "archive": script["archive"],
                        "entry": script["script_name"],
                    },
                )
            )

        edges: list[StoryEdge] = []
        for edge in story_index["edges"]:
            source = edge["source"].casefold()
            target = edge["target"].casefold()
            if source not in segment_names or target not in segment_names:
                continue
            edges.append(
                StoryEdge(
                    source=source,
                    target=target,
                    kind=edge["type"],
                    label=edge.get("option_text"),
                    option_id=edge.get("option_id"),
                    evidence={
                        "engine": self.name,
                        "offset": edge["offset"],
                        "archive": edge.get("target_archive"),
                        "entry": edge.get("target_entry"),
                    },
                )
            )

        profile = replay_index["replay_profile"]
        return StorySource(
            engine=self.name,
            game_dir=game_dir,
            title=game_dir.name,
            fingerprint=str(profile["profile_id"]),
            segments=tuple(segments),
            edges=tuple(edges),
            scenes=tuple(scenes),
            entry_segment=segments[0].id,
            capabilities=("scene_replay",),
            evidence={
                "story_archive": story_index["story_archive"],
                "profile_id": profile["profile_id"],
                "scene_boundary_method": replay_index["evidence"]["boundary_method"],
            },
        )

    def perform_story_action(
        self,
        action: str,
        parameters: dict[str, Any],
        game_dir: Path,
        output_dir: Path,
    ) -> dict[str, Any]:
        from hgalgame.runtime.advhd_inplace_runner import AdvHdInPlaceReplayRunner
        from hgalgame.runtime.advhd_replay_builder import AdvHdContinuousReplayBuilder

        if action != "scene_replay":
            raise ValueError(f"AdvHD does not support story action: {action}")
        try:
            replay_id = int(parameters["replay_key"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("scene_replay requires an integer replay_key") from exc
        package = output_dir.resolve() / "in_place_replay"
        report = AdvHdContinuousReplayBuilder().build(
            game_dir.resolve(), package, playlist=[replay_id]
        )
        result = AdvHdInPlaceReplayRunner().run(package / "manifest.json")
        return {
            "action": action,
            "replay_key": replay_id,
            "playlist": report["playlist"],
            "runtime": result,
        }

    def create_story_session(
        self,
        game_dir: Path,
        output_dir: Path,
    ):
        """Create the optional persistent runtime without leaking it into the UI."""

        from hgalgame.runtime.advhd_scene_session import AdvHdSceneSession

        return AdvHdSceneSession(
            game_dir.resolve(),
            output_dir.resolve() / "scene_session",
        )

    def _normalize_story_event(
        self, event: dict[str, Any], segment_names: dict[str, str]
    ) -> dict[str, Any] | None:
        event_type = event["type"]
        if event_type == "dialogue":
            text = str(event["text"])
            if not text.strip():
                return None
            result: dict[str, Any] = {
                "kind": "dialogue",
                "offset": event["offset"],
                "speaker": event.get("speaker"),
                "text": text,
            }
            voice = event.get("voice")
            if voice:
                result["voice"] = {"asset": voice["asset"]}
            return result
        if event_type == "choice":
            options = []
            for option in event["options"]:
                target = option["target"]
                if target.get("type") != "script":
                    continue
                segment_id = str(target["script"]).casefold()
                if segment_id not in segment_names:
                    continue
                options.append(
                    {
                        "id": option["id"],
                        "text": option["text"],
                        "target": segment_id,
                    }
                )
            return {
                "kind": "choice",
                "id": f"{event['offset']}",
                "offset": event["offset"],
                "options": options,
            }
        return None
