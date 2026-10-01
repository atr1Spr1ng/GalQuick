"""AdvHD / WillPlus engine support."""

from hgalgame.engines.advhd.adapter import AdvHdAdapter
from hgalgame.engines.advhd.archive import AdvHdArcReader, AdvHdFormatError
from hgalgame.engines.advhd.archive_writer import AdvHdArcWriter
from hgalgame.engines.advhd.report import AdvHdReportBuilder
from hgalgame.engines.advhd.replay_catalog import (
    GalleryAsset,
    ReplayDispatch,
    extract_gallery_assets,
    extract_replay_dispatches,
)
from hgalgame.engines.advhd.ws2_reader import Ws2Reader
from hgalgame.engines.advhd.ws2_encoder import Ws2Encoder
from hgalgame.engines.advhd.ws2_parser import Ws2ParseError, Ws2Parser
from hgalgame.engines.advhd.profile import (
    AdvHdParsedScript,
    AdvHdReplayProfile,
    AdvHdReplayProfiler,
    infer_replay_control,
)

__all__ = [
    "AdvHdAdapter",
    "AdvHdArcReader",
    "AdvHdArcWriter",
    "AdvHdFormatError",
    "AdvHdReportBuilder",
    "AdvHdParsedScript",
    "AdvHdReplayProfile",
    "AdvHdReplayProfiler",
    "GalleryAsset",
    "ReplayDispatch",
    "Ws2Reader",
    "Ws2Encoder",
    "Ws2ParseError",
    "Ws2Parser",
    "extract_gallery_assets",
    "extract_replay_dispatches",
    "infer_replay_control",
]
