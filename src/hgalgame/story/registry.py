from __future__ import annotations

from pathlib import Path
from typing import Iterable

from hgalgame.story.adapter import StoryEngineAdapter


class EngineAdapterRegistry:
    """Select an engine adapter without exposing it to the generic UI."""

    def __init__(self, adapters: Iterable[StoryEngineAdapter]) -> None:
        self.adapters = tuple(adapters)
        if not self.adapters:
            raise ValueError("at least one engine adapter is required")

    def select(self, game_dir: Path) -> StoryEngineAdapter:
        results = [(adapter.detect(game_dir), adapter) for adapter in self.adapters]
        result, adapter = max(results, key=lambda item: item[0].confidence)
        if result.engine == "unknown":
            raise ValueError("no registered engine adapter recognized the game directory")
        return adapter

