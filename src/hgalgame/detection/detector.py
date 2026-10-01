from pathlib import Path

from hgalgame.engines.registry import default_engine_adapters
from hgalgame.models import DetectionResult


class EngineDetector:
    """Run deterministic engine adapters and return the strongest match."""

    def __init__(self) -> None:
        self.adapters = default_engine_adapters()

    def detect(self, game_dir: Path) -> DetectionResult:
        results = [adapter.detect(game_dir) for adapter in self.adapters]
        return max(results, key=lambda result: result.confidence)
