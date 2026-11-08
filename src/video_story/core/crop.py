"""The crop of a recording: the range ``[start_ms, end_ms]`` the user selected.

Stored next to the recording as ``crop.json`` whenever the user changes the In/Out
handles in the editor, so the next time the recording is opened the same range comes
back, and so that agents (``describe``) know which part of the recording to act on.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fileio import atomic_write_text, read_json_with_retry
from .session import now_iso

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Crop:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def to_dict(self) -> dict[str, int]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms}

    def clamped(self, duration_ms: int) -> Crop:
        """Fit into ``[0, duration_ms]``; an empty or inverted range becomes the whole."""
        if duration_ms <= 0:  # unknown length: keep a sane range, whole from 0 if inverted
            start, end = max(0, self.start_ms), max(0, self.end_ms)
            return Crop(start, end) if end > start else Crop(0, end)
        start = min(max(0, self.start_ms), duration_ms)
        end = min(max(0, self.end_ms), duration_ms)
        if end <= start:
            return Crop(0, duration_ms)
        return Crop(start, end)

    @classmethod
    def whole(cls, duration_ms: int) -> Crop:
        return cls(0, max(0, duration_ms))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Crop:
        return cls(int(data["start_ms"]), int(data["end_ms"]))


def read_crop(path: Path, duration_ms: int) -> Crop:
    """The stored crop clamped to the recording, or the whole recording when absent."""
    if not path.exists():
        return Crop.whole(duration_ms)
    try:
        crop = Crop.from_dict(read_json_with_retry(path))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("ignoring unreadable %s: %s", path, exc)
        return Crop.whole(duration_ms)
    return crop.clamped(duration_ms)


def write_crop(path: Path, crop: Crop) -> None:
    """Atomically store the crop (``start_ms``, ``end_ms``, ``updated_at``)."""
    payload = {**crop.to_dict(), "updated_at": now_iso()}
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


__all__ = ["Crop", "read_crop", "write_crop"]
