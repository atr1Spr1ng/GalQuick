from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from hgalgame.models import Ws2Probe


def _rotate_right_2(data: bytes) -> bytes:
    return bytes((byte >> 2) | ((byte & 0x03) << 6) for byte in data)


class Ws2Reader:
    """Classify a WS2 payload without exporting its contents."""

    TRANSFORMS: tuple[tuple[str, Callable[[bytes], bytes]], ...] = (
        ("none", lambda data: data),
        ("rotate_right_2", _rotate_right_2),
    )
    MARKERS = (b"char\x00", b"%LC", b"%LF", b"LAYER_ORDER\x00")

    def decode(self, payload: bytes, transform: str) -> bytes:
        for name, apply_transform in self.TRANSFORMS:
            if name == transform:
                return apply_transform(payload)
        raise ValueError(f"unsupported WS2 transform: {transform}")

    def probe(self, entry_name: str, payload: bytes) -> Ws2Probe:
        candidates: list[tuple[int, str, bytes, dict[str, int]]] = []
        for transform, apply_transform in self.TRANSFORMS:
            decoded = apply_transform(payload)
            counts = {
                marker.rstrip(b"\x00").decode("ascii"): decoded.count(marker)
                for marker in self.MARKERS
            }
            ascii_cstrings = re.findall(rb"[\x20-\x7e]{4,}\x00", decoded)
            counts["ascii_cstring_count"] = len(ascii_cstrings)
            counts["ascii_cstring_bytes"] = sum(
                len(value) - 1 for value in ascii_cstrings
            )
            score = counts["char"] * 12 + counts["%LC"] * 8 + counts["%LF"] * 8
            score += counts["LAYER_ORDER"] * 4 + counts["ascii_cstring_bytes"]
            candidates.append((score, transform, decoded, counts))

        score, transform, decoded, counts = max(candidates, key=lambda item: item[0])
        status = "recognized" if counts["ascii_cstring_bytes"] >= 8 else "unknown"
        return Ws2Probe(
            entry_name=entry_name,
            byte_length=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            status=status,
            transform=transform,
            encrypted=transform != "none",
            encoding="cp932" if status == "recognized" else "unknown",
            marker_counts=counts,
            decoded_prefix_hex=decoded[:32].hex(" "),
        )
