"""Screen capture (mss) and image encoding (Pillow)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from PIL import Image

from video_story.core.screens import Screen

PNG_COMPRESS_LEVEL = 1  # speed over size: frames are produced several times per second


@dataclass
class RawFrame:
    screen_id: str
    width: int
    height: int
    bgra: bytes


class ScreenGrabber:
    """Grabs individual screens. Not thread-safe: use one instance per thread."""

    def __init__(self) -> None:
        self._sct: Any = None

    def open(self) -> None:
        import mss

        factory = getattr(mss, "MSS", None) or mss.mss
        self._sct = factory()

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None

    def __enter__(self) -> ScreenGrabber:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def grab(self, screen: Screen) -> RawFrame:
        if self._sct is None:
            self.open()
        region = {"left": screen.x, "top": screen.y, "width": screen.width, "height": screen.height}
        shot = self._sct.grab(region)
        return RawFrame(screen.id, shot.width, shot.height, bytes(shot.bgra))


def frame_to_image(frame: RawFrame) -> Image.Image:
    return Image.frombuffer("RGB", (frame.width, frame.height), frame.bgra, "raw", "BGRX", 0, 1)


def encode_frame(frame: RawFrame, image_format: str, jpeg_quality: int = 85) -> bytes:
    """Encode a raw frame to PNG or JPEG bytes."""
    image = frame_to_image(frame)
    buffer = io.BytesIO()
    if image_format == "png":
        image.save(buffer, format="PNG", compress_level=PNG_COMPRESS_LEVEL)
    elif image_format == "jpeg":
        image.save(buffer, format="JPEG", quality=int(jpeg_quality), optimize=False)
    else:
        raise ValueError(f"unknown image format {image_format!r}")
    return buffer.getvalue()


__all__ = ["PNG_COMPRESS_LEVEL", "RawFrame", "ScreenGrabber", "encode_frame", "frame_to_image"]
