from __future__ import annotations

from collections import Counter
from typing import Any


class ChoiceBranchAnalyzer:
    """Add engine-neutral route summaries to normalized choice branches."""

    SHORT_MERGE_MAX_NODES = 2
    WALK_LIMIT = 64

    def enrich(self, document: dict[str, Any]) -> dict[str, Any]:
        segments = {
            str(segment["id"]): segment
            for segment in document.get("segments", [])
            if isinstance(segment, dict) and segment.get("id") is not None
        }
        orders = {
            segment_id: int(segment.get("order", 0))
            for segment_id, segment in segments.items()
        }
        outgoing: dict[str, list[dict[str, Any]]] = {}
        for edge in document.get("graph", {}).get("edges", []):
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source", ""))
            target = str(edge.get("target", ""))
            if source in segments and target in segments:
                outgoing.setdefault(source, []).append(edge)

        scenes = {
            str(scene["id"]): scene
            for scene in document.get("scenes", [])
            if isinstance(scene, dict) and scene.get("id") is not None
        }
        scenes_by_segment: dict[str, list[dict[str, Any]]] = {}
        for scene in scenes.values():
            segment_id = str(scene.get("segment_id", ""))
            if segment_id in segments:
                scenes_by_segment.setdefault(segment_id, []).append(scene)

        split_count = 0
        choices = document.get("graph", {}).get("choices", [])
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            merge = str(choice["merge"]) if choice.get("merge") is not None else None
            branches = [
                branch
                for branch in choice.get("branches", [])
                if isinstance(branch, dict)
            ]
            kind = self._choice_kind(merge, branches)
            if kind == "route_split":
                split_count += 1

            for branch in branches:
                path = [
                    str(node)
                    for node in branch.get("path", [])
                    if str(node) in segments
                ]
                speaker_counts: Counter[str] = Counter()
                dialogue_count = 0
                for segment_id in path:
                    for event in segments[segment_id].get("events", []):
                        if not isinstance(event, dict) or event.get("kind") != "dialogue":
                            continue
                        dialogue_count += 1
                        speaker = str(event.get("speaker") or "").strip()
                        if speaker:
                            speaker_counts[speaker] += 1

                scene_ids = self._unique(
                    str(scene_id)
                    for scene_id in branch.get("scene_ids", [])
                    if str(scene_id) in scenes
                )
                next_gate, walked_path = self._walk_to_gate(
                    str(branch.get("target", "")),
                    merge,
                    segments,
                    orders,
                    outgoing,
                    scenes,
                    scenes_by_segment,
                )
                branch["analysis"] = {
                    "node_count": len(path),
                    "dialogue_count": dialogue_count,
                    "scene_count": len(scene_ids),
                    "scene_ids": scene_ids,
                    "top_speakers": [
                        {"name": name, "dialogue_count": count}
                        for name, count in speaker_counts.most_common(3)
                    ],
                    "next_gate": next_gate,
                    "walked_path": walked_path,
                    "path_truncated": self._path_is_truncated(
                        path,
                        merge,
                        outgoing,
                    ),
                }

            choice["analysis"] = {
                "kind": kind,
                "merge": merge,
                "merge_order": choice.get("merge_order"),
                "max_branch_nodes": max(
                    (len(branch.get("path", [])) for branch in branches),
                    default=0,
                ),
                "option_count": len(branches),
            }

        summary = document.setdefault("summary", {})
        summary["route_split_choice_count"] = split_count
        summary["merged_choice_count"] = max(0, len(choices) - split_count)
        return document

    def _choice_kind(
        self,
        merge: str | None,
        branches: list[dict[str, Any]],
    ) -> str:
        if len(branches) < 2:
            return "single_path"
        if merge is None:
            return "route_split"
        longest = max((len(branch.get("path", [])) for branch in branches), default=0)
        if longest <= self.SHORT_MERGE_MAX_NODES:
            return "short_merge"
        return "merged_branch"

    def _walk_to_gate(
        self,
        start: str,
        merge: str | None,
        segments: dict[str, dict[str, Any]],
        orders: dict[str, int],
        outgoing: dict[str, list[dict[str, Any]]],
        scenes: dict[str, dict[str, Any]],
        scenes_by_segment: dict[str, list[dict[str, Any]]],
    ) -> tuple[dict[str, Any], list[str]]:
        node = start
        visited: set[str] = set()
        path: list[str] = []
        for _step in range(self.WALK_LIMIT):
            if node == merge:
                return self._merge_gate(node, segments), path
            if node not in segments:
                return {"kind": "unknown", "label": "未知剧情位置"}, path
            if node in visited:
                return {"kind": "cycle", "label": "循环剧情"}, path
            visited.add(node)
            path.append(node)

            gate = self._first_gate(
                node,
                segments[node],
                scenes,
                scenes_by_segment,
            )
            if gate:
                return gate, path

            edges = [
                edge
                for edge in outgoing.get(node, [])
                if str(edge.get("target", "")) in segments
            ]
            if not edges:
                return {
                    "kind": "ending",
                    "label": "路线结尾",
                    "segment_id": node,
                }, path
            node = str(self._preferred_edge(edges, node, orders).get("target"))
        return {"kind": "limit", "label": "后续剧情过长"}, path

    def _first_gate(
        self,
        segment_id: str,
        segment: dict[str, Any],
        scenes: dict[str, dict[str, Any]],
        scenes_by_segment: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any] | None:
        candidates: list[tuple[float, int, dict[str, Any]]] = []
        seen_scenes: set[str] = set()
        for event in segment.get("events", []):
            if not isinstance(event, dict):
                continue
            kind = str(event.get("kind", ""))
            offset = self._number(event.get("offset"), 0)
            if kind == "choice":
                candidates.append(
                    (
                        offset,
                        1,
                        {
                            "kind": "choice",
                            "id": str(event.get("id", "")),
                            "label": "下一次路线选择",
                            "segment_id": segment_id,
                        },
                    )
                )
            elif kind == "scene":
                scene_id = str(event.get("id", ""))
                scene = scenes.get(scene_id, {})
                seen_scenes.add(scene_id)
                candidates.append(
                    (
                        offset,
                        0,
                        {
                            "kind": "scene",
                            "id": scene_id,
                            "label": str(scene.get("label") or "Scene"),
                            "segment_id": segment_id,
                        },
                    )
                )
        for scene in scenes_by_segment.get(segment_id, []):
            scene_id = str(scene.get("id", ""))
            if scene_id in seen_scenes:
                continue
            candidates.append(
                (
                    self._number(scene.get("offset"), 0),
                    0,
                    {
                        "kind": "scene",
                        "id": scene_id,
                        "label": str(scene.get("label") or "Scene"),
                        "segment_id": segment_id,
                    },
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1], str(item[2].get("id", ""))))
        return candidates[0][2]

    def _preferred_edge(
        self,
        edges: list[dict[str, Any]],
        source: str,
        orders: dict[str, int],
    ) -> dict[str, Any]:
        non_choice = [edge for edge in edges if edge.get("kind") != "choice"]
        candidates = non_choice or edges
        source_order = orders.get(source, 0)
        return min(
            candidates,
            key=lambda edge: (
                0 if edge.get("preferred") else 1,
                0 if orders.get(str(edge.get("target")), 0) > source_order else 1,
                abs(orders.get(str(edge.get("target")), 0) - source_order),
                str(edge.get("target", "")),
            ),
        )

    def _merge_gate(
        self,
        segment_id: str,
        segments: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        segment = segments.get(segment_id, {})
        return {
            "kind": "merge",
            "id": segment_id,
            "label": str(segment.get("label") or "共同剧情"),
            "segment_id": segment_id,
            "order": segment.get("order"),
        }

    def _path_is_truncated(
        self,
        path: list[str],
        merge: str | None,
        outgoing: dict[str, list[dict[str, Any]]],
    ) -> bool:
        if merge is not None or not path:
            return False
        return len(path) >= 8 and bool(outgoing.get(path[-1]))

    @staticmethod
    def _number(value: Any, fallback: float) -> float:
        if isinstance(value, bool):
            return fallback
        if isinstance(value, (int, float)):
            return float(value)
        return fallback

    @staticmethod
    def _unique(values) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result
