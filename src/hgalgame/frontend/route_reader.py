from __future__ import annotations

from dataclasses import dataclass
from math import inf
from typing import Any


@dataclass(frozen=True)
class NovelParagraph:
    kind: str
    text: str
    speaker: str | None = None
    segment_id: str | None = None
    offset: int | None = None


@dataclass(frozen=True)
class NovelGate:
    kind: str
    id: str
    trace_index: int
    event: dict[str, Any]
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class NovelTrace:
    paragraphs: tuple[NovelParagraph, ...]
    current_paragraphs: tuple[NovelParagraph, ...]
    gate: NovelGate | None
    action_index: int
    dialogue_count: int
    current_dialogue_count: int
    choice_count: int
    scene_count: int
    ending: str | None = None
    error: str | None = None


class RouteNovelReader:
    """Engine-neutral route state machine shared by native frontends."""

    SESSION_VERSION = 1

    def __init__(
        self,
        document: dict[str, Any],
        actions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.document = document
        self.segments = {
            str(segment["id"]): segment for segment in document["segments"]
        }
        self.choices = {
            str(choice["id"]): choice for choice in document["graph"]["choices"]
        }
        self.scenes = {str(scene["id"]): scene for scene in document["scenes"]}
        self.outgoing: dict[str, list[dict[str, Any]]] = {}
        for edge in document["graph"]["edges"]:
            self.outgoing.setdefault(str(edge["source"]), []).append(edge)
        self.actions = self._sanitize_actions(actions or [])

    def session(self) -> dict[str, Any]:
        return {"version": self.SESSION_VERSION, "actions": list(self.actions)}

    def choose(self, gate: NovelGate, option_id: int | str) -> NovelTrace:
        if gate.kind != "choice":
            raise ValueError("current gate is not a route choice")
        option = next(
            (
                value
                for value in gate.event.get("options", [])
                if str(value.get("id")) == str(option_id)
            ),
            None,
        )
        if option is None:
            raise ValueError("unknown route option")
        self._commit(
            gate.trace_index,
            {"type": "choice", "id": gate.id, "option_id": option["id"]},
        )
        return self.render()

    def finish_scene(self, gate: NovelGate, status: str) -> NovelTrace:
        if gate.kind != "scene":
            raise ValueError("current gate is not a Scene")
        if status not in {"played", "skipped"}:
            raise ValueError("Scene status must be played or skipped")
        self._commit(
            gate.trace_index,
            {"type": "scene", "id": gate.id, "status": status},
        )
        return self.render()

    def rewind(self, trace_index: int) -> NovelTrace:
        if isinstance(trace_index, bool) or not 0 <= trace_index <= len(self.actions):
            raise ValueError("invalid route history position")
        self.actions = self.actions[:trace_index]
        return self.render()

    def restart(self) -> NovelTrace:
        self.actions = []
        return self.render()

    def render(self) -> NovelTrace:
        paragraphs: list[NovelParagraph] = []
        action_index = 0
        dialogue_count = 0
        segment_id = str(self.document["entry_segment"])
        start_offset = -inf
        visits: dict[str, int] = {}
        cursors: set[tuple[str, float, int]] = set()
        ending: str | None = None
        error: str | None = None
        gate: NovelGate | None = None
        current_start = 0

        for _step in range(4096):
            cursor = (segment_id, start_offset, action_index)
            if cursor in cursors:
                error = "剧情图在当前位置形成循环，已停止继续展开。"
                break
            cursors.add(cursor)
            segment = self.segments.get(segment_id)
            if segment is None:
                error = f"找不到剧情段：{segment_id}"
                break

            visit_count = visits.get(segment_id, 0)
            visits[segment_id] = visit_count + 1
            paragraphs.append(
                NovelParagraph(
                    kind="chapter",
                    text=self._segment_label(segment, visit_count),
                    segment_id=segment_id,
                )
            )
            events = sorted(
                (
                    event
                    for event in segment.get("events", [])
                    if self._number(event.get("offset"), 0) >= start_offset
                ),
                key=lambda event: self._number(event.get("offset"), 0),
            )

            jumped = False
            for event in events:
                kind = str(event.get("kind", ""))
                if kind == "dialogue":
                    paragraphs.append(
                        NovelParagraph(
                            kind="dialogue",
                            text=str(event.get("text", "")),
                            speaker=(
                                str(event["speaker"])
                                if event.get("speaker")
                                else None
                            ),
                            segment_id=segment_id,
                            offset=int(self._number(event.get("offset"), 0)),
                        )
                    )
                    dialogue_count += 1
                    continue

                if kind == "choice":
                    event_id = str(event["id"])
                    action = self._recorded_action(
                        action_index, "choice", event_id
                    )
                    selected = None
                    if action:
                        selected = next(
                            (
                                option
                                for option in event.get("options", [])
                                if str(option.get("id"))
                                == str(action.get("option_id"))
                            ),
                            None,
                        )
                    if action and selected is None:
                        self.actions = self.actions[:action_index]
                        action = None
                    if not action:
                        gate = NovelGate(
                            kind="choice",
                            id=event_id,
                            trace_index=action_index,
                            event=event,
                            detail=self.choices.get(event_id),
                        )
                        break
                    action_index += 1
                    current_start = len(paragraphs)
                    segment_id = str(selected["target"])
                    start_offset = -inf
                    jumped = True
                    break

                if kind == "scene":
                    event_id = str(event["id"])
                    scene = self.scenes.get(event_id)
                    if scene is None:
                        continue
                    action = self._recorded_action(
                        action_index, "scene", event_id
                    )
                    if action and action.get("status") not in {"played", "skipped"}:
                        self.actions = self.actions[:action_index]
                        action = None
                    if not action:
                        gate = NovelGate(
                            kind="scene",
                            id=event_id,
                            trace_index=action_index,
                            event=event,
                            detail=scene,
                        )
                        break
                    action_index += 1
                    current_start = len(paragraphs)
                    resume = scene.get("resume_cursor")
                    if not resume:
                        ending = "这个 Scene 之后没有可继续的剧情位置。"
                        break
                    segment_id = str(resume["segment_id"])
                    start_offset = self._number(resume.get("offset"), 0)
                    jumped = True
                    break

            if gate or ending or error:
                break
            if jumped:
                continue
            target = self._next_segment(segment, start_offset)
            if target is None:
                ending = "没有更多可达的剧情段。"
                break
            segment_id = target
            start_offset = -inf
        else:
            error = "剧情路径超过安全上限，已停止继续展开。"

        if len(self.actions) > action_index:
            self.actions = self.actions[:action_index]
        choice_count = sum(action.get("type") == "choice" for action in self.actions)
        scene_count = sum(action.get("type") == "scene" for action in self.actions)
        current_paragraphs = tuple(paragraphs[current_start:])
        current_dialogue_count = sum(
            paragraph.kind == "dialogue" for paragraph in current_paragraphs
        )
        return NovelTrace(
            paragraphs=tuple(paragraphs),
            current_paragraphs=current_paragraphs,
            gate=gate,
            action_index=action_index,
            dialogue_count=dialogue_count,
            current_dialogue_count=current_dialogue_count,
            choice_count=choice_count,
            scene_count=scene_count,
            ending=ending,
            error=error,
        )

    def _recorded_action(
        self,
        index: int,
        action_type: str,
        event_id: str,
    ) -> dict[str, Any] | None:
        if index >= len(self.actions):
            return None
        action = self.actions[index]
        if action.get("type") == action_type and str(action.get("id")) == event_id:
            return action
        self.actions = self.actions[:index]
        return None

    def _next_segment(
        self,
        segment: dict[str, Any],
        start_offset: float,
    ) -> str | None:
        candidates = [
            edge
            for edge in self.outgoing.get(str(segment["id"]), [])
            if edge.get("kind") != "choice"
            and str(edge.get("target")) in self.segments
        ]
        if not candidates:
            return None
        after_cursor = [
            edge
            for edge in candidates
            if start_offset == -inf
            or edge.get("evidence", {}).get("offset") is None
            or self._number(edge.get("evidence", {}).get("offset"), 0)
            >= start_offset
        ]
        candidates = after_cursor or candidates
        source_order = int(segment.get("order", 0))

        def order(edge: dict[str, Any]) -> tuple[Any, ...]:
            target = self.segments[str(edge["target"])]
            target_order = int(target.get("order", 0))
            return (
                0 if edge.get("preferred") else 1,
                0 if target_order > source_order else 1,
                abs(target_order - source_order),
                str(edge["target"]),
            )

        candidates.sort(key=order)
        return str(candidates[0]["target"])

    def _commit(self, index: int, action: dict[str, Any]) -> None:
        if isinstance(index, bool) or not 0 <= index <= len(self.actions):
            raise ValueError("invalid route history position")
        self.actions = self.actions[:index]
        self.actions.append(action)

    def _segment_label(self, segment: dict[str, Any], visit_count: int) -> str:
        label = str(segment.get("label") or "")
        if label.casefold().endswith(".ws2"):
            label = f"剧情 {int(segment.get('order', 0)) + 1:03d}"
        if visit_count:
            label += f" · 路线重访 {visit_count + 1}"
        return label

    def _sanitize_actions(
        self, actions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not isinstance(actions, list):
            return []
        return [dict(action) for action in actions[:512] if isinstance(action, dict)]

    def _number(self, value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
