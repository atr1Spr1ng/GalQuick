from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Evidence:
    kind: str
    detail: str


@dataclass(frozen=True)
class DetectionResult:
    engine: str
    confidence: float
    evidence: tuple[Evidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "confidence": self.confidence,
            "evidence": [asdict(item) for item in self.evidence],
        }


@dataclass(frozen=True)
class ArchiveEntry:
    name: str
    size: int
    relative_offset: int
    absolute_offset: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArchiveManifest:
    path: Path
    entry_count: int
    data_offset: int
    entries: list[ArchiveEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "entry_count": self.entry_count,
            "data_offset": self.data_offset,
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class Ws2Probe:
    entry_name: str
    byte_length: int
    sha256: str
    status: str
    transform: str
    encrypted: bool
    encoding: str
    marker_counts: dict[str, int]
    decoded_prefix_hex: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ScriptEvent:
    type: str
    offset: int
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "offset": self.offset, **self.data}


@dataclass
class ScriptParseResult:
    script_name: str
    format_version: str
    transform: str
    encoding: str
    byte_length: int
    instruction_count: int
    trailer_hex: str
    events: list[ScriptEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def event_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.events:
            counts[event.type] = counts.get(event.type, 0) + 1
        return counts

    def to_dict(self, include_events: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "script_name": self.script_name,
            "format_version": self.format_version,
            "transform": self.transform,
            "encoding": self.encoding,
            "byte_length": self.byte_length,
            "instruction_count": self.instruction_count,
            "trailer_hex": self.trailer_hex,
            "event_counts": self.event_counts,
            "event_count": len(self.events),
            "warnings": list(self.warnings),
        }
        if include_events:
            result["events"] = [event.to_dict() for event in self.events]
        return result
