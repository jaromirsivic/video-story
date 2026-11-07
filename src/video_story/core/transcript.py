"""Transcript formats: ``transcript.txt`` (human readable) and ``transcript.json`` (machine)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .timeline import format_timestamp


@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms, "text": self.text}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Segment:
        return cls(int(data["start_ms"]), int(data["end_ms"]), str(data["text"]))


@dataclass
class Transcript:
    language: str | None
    segments: list[Segment]
    model: str | None = None
    backend: str | None = None

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "model": self.model,
            "backend": self.backend,
            "time_unit": "ms since recording start",
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        return cls(
            language=data.get("language"),
            segments=[Segment.from_dict(s) for s in data.get("segments", [])],
            model=data.get("model"),
            backend=data.get("backend"),
        )

    def segments_at(self, time_ms: int) -> list[Segment]:
        return [s for s in self.segments if s.start_ms <= time_ms <= s.end_ms]


def format_segment_line(segment: Segment) -> str:
    return (
        f"[{format_timestamp(segment.start_ms)} --> {format_timestamp(segment.end_ms)}] "
        f"{segment.text.strip()}"
    )


def transcript_to_text(transcript: Transcript) -> str:
    lines = [format_segment_line(s) for s in transcript.segments]
    return "\n".join(lines) + ("\n" if lines else "")


def write_transcript(txt_path: Path, json_path: Path, transcript: Transcript) -> None:
    txt_path.write_text(transcript_to_text(transcript), encoding="utf-8")
    json_path.write_text(
        json.dumps(transcript.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def read_transcript(json_path: Path) -> Transcript:
    return Transcript.from_dict(json.loads(json_path.read_text(encoding="utf-8")))


def normalize_segments(segments: list[Segment]) -> list[Segment]:
    """Drop empty segments, clamp negative times and keep chronological order."""
    cleaned = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        start = max(0, seg.start_ms)
        end = max(start, seg.end_ms)
        cleaned.append(Segment(start, end, text))
    cleaned.sort(key=lambda s: (s.start_ms, s.end_ms))
    return cleaned


__all__ = [
    "Segment",
    "Transcript",
    "format_segment_line",
    "normalize_segments",
    "read_transcript",
    "transcript_to_text",
    "write_transcript",
]
