from __future__ import annotations

from collections import deque
from typing import Any

from hgalgame.guides.analyzer import ChoiceBranchAnalyzer
from hgalgame.story.models import StoryEdge, StorySource


class StoryFlowBuilder:
    """Build the common reader/route-graph document from normalized engine data."""

    FORMAT_VERSION = "hgal-story-flow-v2"

    def build(self, source: StorySource) -> dict[str, Any]:
        self._validate(source)
        segments = sorted(source.segments, key=lambda item: item.order)
        segment_ids = {item.id for item in segments}
        edges = self._deduplicate_edges(source.edges, segment_ids)
        outgoing: dict[str, list[StoryEdge]] = {item.id: [] for item in segments}
        incoming: dict[str, list[StoryEdge]] = {item.id: [] for item in segments}
        for edge in edges:
            outgoing[edge.source].append(edge)
            incoming[edge.target].append(edge)

        choices = self._build_choices(segments, outgoing, source)
        entry = source.entry_segment or next(
            (item.id for item in segments if not incoming[item.id]), segments[0].id
        )
        scenes = [item.to_dict() for item in source.scenes]
        scenes_by_segment: dict[str, list[str]] = {}
        for scene in source.scenes:
            scenes_by_segment.setdefault(scene.segment_id, []).append(scene.id)

        nodes = []
        for segment in segments:
            counts: dict[str, int] = {}
            for event in segment.events:
                kind = str(event.get("kind", "unknown"))
                counts[kind] = counts.get(kind, 0) + 1
            nodes.append(
                {
                    "id": segment.id,
                    "label": segment.label,
                    "order": segment.order,
                    "event_counts": counts,
                    "scene_ids": scenes_by_segment.get(segment.id, []),
                }
            )

        document = {
            "format_version": self.FORMAT_VERSION,
            "game": {
                "title": source.title,
                "engine": source.engine,
                "game_dir": str(source.game_dir),
                "fingerprint": source.fingerprint,
            },
            "capabilities": {
                "actions": list(source.capabilities),
                "scene_replay": "scene_replay" in source.capabilities,
            },
            "summary": {
                "segment_count": len(segments),
                "edge_count": len(edges),
                "choice_count": len(choices),
                "scene_count": len(scenes),
                "dialogue_count": sum(
                    event.get("kind") == "dialogue"
                    for segment in segments
                    for event in segment.events
                ),
            },
            "entry_segment": entry,
            "segments": [item.to_dict() for item in segments],
            "graph": {
                "nodes": nodes,
                "edges": [item.to_dict() for item in edges],
                "choices": choices,
            },
            "scenes": scenes,
            "evidence": dict(source.evidence),
        }
        return ChoiceBranchAnalyzer().enrich(document)

    def _build_choices(
        self,
        segments,
        outgoing: dict[str, list[StoryEdge]],
        source: StorySource,
    ) -> list[dict[str, Any]]:
        labels = {item.id: item.label for item in segments}
        orders = {item.id: item.order for item in segments}
        scenes_by_segment: dict[str, list[str]] = {}
        for scene in source.scenes:
            scenes_by_segment.setdefault(scene.segment_id, []).append(scene.id)
        result: list[dict[str, Any]] = []
        for segment in segments:
            for event in segment.events:
                if event.get("kind") != "choice":
                    continue
                choice_id = str(event["id"])
                options = [
                    option
                    for option in event.get("options", [])
                    if option.get("target") in labels
                ]
                targets = [str(option["target"]) for option in options]
                merge = self._nearest_common_descendant(targets, outgoing)
                branches = []
                for option in options:
                    target = str(option["target"])
                    path = self._path_to(target, merge, outgoing)
                    branch_scenes = [
                        scene_id
                        for node in path
                        for scene_id in scenes_by_segment.get(node, [])
                    ]
                    branches.append(
                        {
                            "option_id": option.get("id"),
                            "text": option.get("text") or "（无选项文本）",
                            "target": target,
                            "path": path,
                            "nodes": [
                                {
                                    "id": node,
                                    "label": labels[node],
                                    "order": orders[node],
                                    "scene_ids": scenes_by_segment.get(node, []),
                                }
                                for node in path
                            ],
                            "scene_ids": branch_scenes,
                        }
                    )
                result.append(
                    {
                        "id": choice_id,
                        "source": segment.id,
                        "offset": event.get("offset", 0),
                        "merge": merge,
                        "merge_label": labels.get(merge) if merge else None,
                        "merge_order": orders.get(merge) if merge else None,
                        "branches": branches,
                    }
                )
        return result

    def _nearest_common_descendant(
        self, starts: list[str], outgoing: dict[str, list[StoryEdge]]
    ) -> str | None:
        if len(starts) < 2:
            return None
        distances = [self._distances(start, outgoing) for start in starts]
        common = set(distances[0])
        for distance in distances[1:]:
            common.intersection_update(distance)
        if not common:
            return None
        return min(
            common,
            key=lambda node: (
                max(distance[node] for distance in distances),
                sum(distance[node] for distance in distances),
                node,
            ),
        )

    def _distances(
        self, start: str, outgoing: dict[str, list[StoryEdge]]
    ) -> dict[str, int]:
        queue = deque([(start, 0)])
        distances: dict[str, int] = {}
        while queue:
            node, distance = queue.popleft()
            if node in distances:
                continue
            distances[node] = distance
            for edge in outgoing.get(node, []):
                queue.append((edge.target, distance + 1))
        return distances

    def _path_to(
        self,
        start: str,
        goal: str | None,
        outgoing: dict[str, list[StoryEdge]],
    ) -> list[str]:
        if goal == start:
            return []
        queue = deque([(start, [start])])
        visited: set[str] = set()
        best_terminal: list[str] | None = None
        while queue:
            node, path = queue.popleft()
            if node in visited or len(path) > 24:
                continue
            visited.add(node)
            if goal is not None and node == goal:
                return path[:-1]
            edges = self._ordered_edges(outgoing.get(node, []))
            if goal is None and (not edges or len(path) >= 8):
                best_terminal = best_terminal or path
                continue
            for edge in edges:
                queue.append((edge.target, [*path, edge.target]))
        return best_terminal or [start]

    def _ordered_edges(self, edges: list[StoryEdge]) -> list[StoryEdge]:
        choice_edges = [edge for edge in edges if edge.kind == "choice"]
        if choice_edges:
            return choice_edges
        return sorted(edges, key=lambda edge: (not edge.preferred, edge.target))

    def _deduplicate_edges(
        self, edges: tuple[StoryEdge, ...], segment_ids: set[str]
    ) -> list[StoryEdge]:
        selected: dict[tuple[str, str, str, int | str | None], StoryEdge] = {}
        for edge in edges:
            if edge.source not in segment_ids or edge.target not in segment_ids:
                continue
            key = (edge.source, edge.target, edge.kind, edge.option_id)
            previous = selected.get(key)
            if previous is None or edge.preferred:
                selected[key] = edge
        return list(selected.values())

    def _validate(self, source: StorySource) -> None:
        if not source.segments:
            raise ValueError("story source contains no segments")
        ids = [item.id for item in source.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("story source contains duplicate segment ids")
        known = set(ids)
        if source.entry_segment is not None and source.entry_segment not in known:
            raise ValueError("entry segment does not exist")
        for scene in source.scenes:
            if scene.segment_id not in known:
                raise ValueError(f"scene {scene.id} points to an unknown segment")
            has_resume_segment = scene.resume_segment_id is not None
            has_resume_offset = scene.resume_offset is not None
            if has_resume_segment != has_resume_offset:
                raise ValueError(
                    f"scene {scene.id} has an incomplete resume cursor"
                )
            if has_resume_segment and scene.resume_segment_id not in known:
                raise ValueError(
                    f"scene {scene.id} resumes in an unknown segment"
                )
        choice_ids = [
            str(event["id"])
            for segment in source.segments
            for event in segment.events
            if event.get("kind") == "choice"
        ]
        if len(choice_ids) != len(set(choice_ids)):
            raise ValueError("story source contains duplicate choice ids")
