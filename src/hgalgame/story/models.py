from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StoryEdge:
    """Engine-neutral edge between two story segments."""

    source: str
    target: str
    kind: str = "flow"
    label: str | None = None
    option_id: int | str | None = None
    preferred: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StorySegment:
    """A readable chunk of story, independent of the script file format."""

    id: str
    label: str
    order: int
    events: tuple[dict[str, Any], ...]
    source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "order": self.order,
            "events": list(self.events),
            "source": dict(self.source),
        }


@dataclass(frozen=True)
class StoryScene:
    """A playable scene attached to a point in the story graph."""

    id: str
    label: str
    segment_id: str
    offset: int
    replay_key: int | str
    speakers: tuple[str, ...] = ()
    cg_assets: tuple[str, ...] = ()
    resume_segment_id: str | None = None
    resume_offset: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "segment_id": self.segment_id,
            "offset": self.offset,
            "replay_key": self.replay_key,
            "speakers": list(self.speakers),
            "cg_assets": list(self.cg_assets),
            "resume_cursor": (
                {
                    "segment_id": self.resume_segment_id,
                    "offset": self.resume_offset,
                }
                if self.resume_segment_id is not None
                and self.resume_offset is not None
                else None
            ),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class StorySource:
    """Normalized output contract implemented by every engine adapter."""

    engine: str
    game_dir: Path
    title: str
    fingerprint: str
    segments: tuple[StorySegment, ...]
    edges: tuple[StoryEdge, ...]
    scenes: tuple[StoryScene, ...] = ()
    entry_segment: str | None = None
    capabilities: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
