"""File naming for frames and their sidecar JSON files.

A frame is named ``<seconds>_<millis>_<screenId>.<ext>`` where ``millis`` has three
digits, for example ``0_250_screen1.png``; its sidecar is ``0_250_screen1.json``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EXTENSION_BY_FORMAT = {"png": "png", "jpeg": "jpg"}
FORMAT_BY_EXTENSION = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg"}
SIDECAR_EXTENSION = "json"
SCREEN_ID_PREFIX = "screen"

_FRAME_RE = re.compile(
    rf"^(?P<seconds>\d+)_(?P<millis>\d{{3}})_(?P<screen>{SCREEN_ID_PREFIX}\d+)\.(?P<ext>png|jpg|jpeg|json)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class FrameName:
    time_ms: int
    screen_id: str
    extension: str

    @property
    def is_sidecar(self) -> bool:
        return self.extension.lower() == SIDECAR_EXTENSION

    @property
    def base(self) -> str:
        return frame_base(self.time_ms, self.screen_id)

    @property
    def filename(self) -> str:
        return f"{self.base}.{self.extension}"


def screen_id(index: int) -> str:
    """``screen1`` for index 1 (indexes start at one)."""
    if index < 1:
        raise ValueError("screen index starts at 1")
    return f"{SCREEN_ID_PREFIX}{index}"


def frame_base(time_ms: int, screen: str) -> str:
    if time_ms < 0:
        raise ValueError("time_ms must not be negative")
    seconds, millis = divmod(time_ms, 1000)
    return f"{seconds}_{millis:03d}_{screen}"


def frame_filename(time_ms: int, screen: str, image_format: str) -> str:
    try:
        ext = EXTENSION_BY_FORMAT[image_format.lower()]
    except KeyError as exc:
        raise ValueError(f"unknown image format {image_format!r}") from exc
    return f"{frame_base(time_ms, screen)}.{ext}"


def sidecar_filename(time_ms: int, screen: str) -> str:
    return f"{frame_base(time_ms, screen)}.{SIDECAR_EXTENSION}"


def parse_frame_filename(name: str) -> FrameName | None:
    """Parse a frame or sidecar filename; returns ``None`` for other files."""
    match = _FRAME_RE.match(name)
    if not match:
        return None
    time_ms = int(match["seconds"]) * 1000 + int(match["millis"])
    return FrameName(
        time_ms=time_ms, screen_id=match["screen"].lower(), extension=match["ext"].lower()
    )


__all__ = [
    "EXTENSION_BY_FORMAT",
    "FORMAT_BY_EXTENSION",
    "FrameName",
    "frame_base",
    "frame_filename",
    "parse_frame_filename",
    "screen_id",
    "sidecar_filename",
]
