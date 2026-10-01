from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from hgalgame.models import DetectionResult
from hgalgame.story.models import StorySource


class StoryEngineAdapter(Protocol):
    """The only interface shared by the frontend and engine implementations."""

    name: str

    def detect(self, game_dir: Path) -> DetectionResult: ...

    def build_story_source(self, game_dir: Path) -> StorySource: ...

    def perform_story_action(
        self,
        action: str,
        parameters: dict[str, Any],
        game_dir: Path,
        output_dir: Path,
    ) -> dict[str, Any]: ...


class StoryPlaybackSession(Protocol):
    """Optional long-lived original-engine runtime used by overlay frontends."""

    @property
    def pid(self) -> int | None: ...

    def start(self) -> dict[str, Any]: ...

    def perform(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]: ...

    def reader_resumed(self) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...

    def stop(self) -> dict[str, Any]: ...
