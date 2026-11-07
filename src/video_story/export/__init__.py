"""Export of a recording to an MP4 video (plus SRT subtitles) with FFmpeg.

Pure logic without Qt: used by the ``video-story export`` command, the MCP tool and
the editor's Render button. The public entry point is :func:`export_recording` in
:mod:`video_story.export.ffmpeg_export`; the types below are its contract.

Output is always constant 24 fps (``EXPORT_FPS``), H.264 (libx264) + AAC in an MP4
container, at the pixel size of the exported screen's image, cropped to
``[start_ms, end_ms]`` of the recording timeline; the SRT next to it holds the
transcript segments of that range with times shifted by ``start_ms``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from video_story.core.session import SessionPaths


class ExportError(RuntimeError):
    """Export failed; ``code`` is a short machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExportRequest:
    """What to export. ``end_ms`` of ``None`` means the end of the recording."""

    paths: SessionPaths
    output: Path
    screen_id: str = "screen1"
    start_ms: int = 0
    end_ms: int | None = None
    draw_cursor: bool = True
    write_srt: bool = True


@dataclass
class ExportResult:
    output: Path
    srt: Path | None
    screen_id: str
    start_ms: int
    end_ms: int
    duration_ms: int
    width: int
    height: int
    fps: int
    frames_used: int
    ffmpeg: str
    command: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": str(self.output),
            "srt": str(self.srt) if self.srt else None,
            "screen": self.screen_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "frames_used": self.frames_used,
            "ffmpeg": self.ffmpeg,
        }


# progress(fraction 0..1, text); cancel() -> True aborts the export
ProgressCallback = Callable[[float, str], None]
CancelCheck = Callable[[], bool]

__all__ = ["CancelCheck", "ExportError", "ExportRequest", "ExportResult", "ProgressCallback"]
