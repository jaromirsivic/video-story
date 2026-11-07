"""SubRip (``.srt``) subtitles from the transcript, cropped to the exported range."""

from __future__ import annotations

from pathlib import Path

from video_story.core.transcript import Transcript


def srt_timestamp(ms: int) -> str:
    """``HH:MM:SS,mmm`` as SubRip wants it."""
    ms = max(0, int(ms))
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def srt_text(transcript: Transcript, start_ms: int, end_ms: int) -> str:
    """Segments overlapping ``[start_ms, end_ms]``, clipped to it and shifted by ``start_ms``.

    Segments that end up empty (no text, or no duration after clipping) are dropped;
    entries are numbered from 1 and separated by a blank line, LF line endings.
    """
    entries: list[str] = []
    for segment in transcript.segments:
        text = segment.text.strip()
        if not text or segment.end_ms <= start_ms or segment.start_ms >= end_ms:
            continue
        begin = max(segment.start_ms, start_ms) - start_ms
        finish = min(segment.end_ms, end_ms) - start_ms
        if finish <= begin:
            continue
        entries.append(
            f"{len(entries) + 1}\n{srt_timestamp(begin)} --> {srt_timestamp(finish)}\n{text}\n"
        )
    return "\n".join(entries)


def write_srt(path: Path, transcript: Transcript, start_ms: int, end_ms: int) -> Path:
    """Write the subtitles as UTF-8 without BOM and return ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(srt_text(transcript, start_ms, end_ms))
    return path


__all__ = ["srt_text", "srt_timestamp", "write_srt"]
