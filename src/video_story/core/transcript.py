"""Transcript formats: ``transcript.txt`` (human readable) and ``transcript.json`` (machine).

A transcript may be *incomplete*: the editor transcribes in the background and stores
what whisper produced so far with ``complete`` false and ``transcribed_ms`` (how far
into the audio the stored segments reach). A transcript written in one go (the
``transcribe`` command, auto stop) is complete; files written before this flag existed
are read as complete.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .timeline import format_timestamp

UPPER_TRACK = 0  # the narration track every transcription fills
LOWER_TRACK = 1  # the second subtitle track the user moves subtitles to in the editor
LOWER_TRACK_MARK = "[lower track]"


@dataclass(frozen=True)
class Segment:
    """One subtitle: its time and words, and the editor track it lies in.

    ``track`` is :data:`UPPER_TRACK` for everything a transcription produces; the user
    may move a subtitle to :data:`LOWER_TRACK` in the editor (mostly to make room
    while rearranging subtitles). Subtitles of one track never overlap; subtitles of
    the two tracks may, and then both apply at that moment.
    """

    start_ms: int
    end_ms: int
    text: str
    track: int = UPPER_TRACK

    @property
    def lower(self) -> bool:
        return self.track == LOWER_TRACK

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
        }
        if self.track != UPPER_TRACK:
            data["track"] = self.track  # absent: the upper track (files before tracks)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Segment:
        lower = int(data.get("track") or UPPER_TRACK) == LOWER_TRACK
        track = LOWER_TRACK if lower else UPPER_TRACK
        return cls(int(data["start_ms"]), int(data["end_ms"]), str(data["text"]), track)


@dataclass
class Transcript:
    language: str | None
    segments: list[Segment]
    model: str | None = None
    backend: str | None = None
    complete: bool = True
    transcribed_ms: int | None = None  # how far the stored segments reach (incomplete)

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "model": self.model,
            "backend": self.backend,
            "time_unit": "ms since recording start",
            "complete": self.complete,
            "transcribed_ms": self.transcribed_ms,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        transcribed = data.get("transcribed_ms")
        return cls(
            language=data.get("language"),
            segments=[Segment.from_dict(s) for s in data.get("segments", [])],
            model=data.get("model"),
            backend=data.get("backend"),
            complete=bool(data.get("complete", True)),
            transcribed_ms=int(transcribed) if transcribed is not None else None,
        )

    def segments_at(self, time_ms: int) -> list[Segment]:
        return [s for s in self.segments if s.start_ms <= time_ms <= s.end_ms]

    def segments_in_range(self, start_ms: int, end_ms: int) -> list[Segment]:
        """Segments overlapping ``[start_ms, end_ms]`` (times are kept unshifted)."""
        return [s for s in self.segments if s.end_ms >= start_ms and s.start_ms <= end_ms]


def format_segment_line(segment: Segment) -> str:
    mark = f"{LOWER_TRACK_MARK} " if segment.lower else ""
    return (
        f"[{format_timestamp(segment.start_ms)} --> {format_timestamp(segment.end_ms)}] "
        f"{mark}{segment.text.strip()}"
    )


def ordered_segments(segments: list[Segment]) -> list[Segment]:
    """Both tracks in time order (the upper one first at the same start)."""
    return sorted(segments, key=lambda s: (s.start_ms, s.track, s.end_ms))


def overlaps(a: Segment, b: Segment) -> bool:
    """True when ``a`` and ``b`` share time; subtitles that only touch do not."""
    return a.start_ms < b.end_ms and b.start_ms < a.end_ms


def display_cues(segments: list[Segment]) -> list[Segment]:
    """What a viewer reads: the subtitles of both tracks as one sequence of cues.

    The time is cut at every start and end; each piece shows the upper subtitle, a
    space and the lower one (or the one that is there). Neighbouring pieces with the
    same text are joined, so a transcript without the lower track comes back
    unchanged. Used by the editor preview, the SRT and the rendered video - not by
    the agent, which reads the tracks apart (:mod:`video_story.host.events`).
    """
    texts = [s for s in segments if s.text.strip()]
    if not any(s.lower for s in texts):
        return sorted(
            (Segment(s.start_ms, s.end_ms, s.text) for s in texts),
            key=lambda s: (s.start_ms, s.end_ms),
        )
    cuts = sorted({t for s in texts for t in (s.start_ms, s.end_ms)})
    cues: list[Segment] = []
    for begin, finish in zip(cuts, cuts[1:], strict=False):
        active = [s for s in texts if s.start_ms <= begin and finish <= s.end_ms]
        if not active:
            continue
        upper = " ".join(s.text.strip() for s in active if not s.lower)
        lower = " ".join(s.text.strip() for s in active if s.lower)
        text = " ".join(t for t in (upper, lower) if t)
        if cues and cues[-1].end_ms == begin and cues[-1].text == text:
            cues[-1] = Segment(cues[-1].start_ms, finish, text)
        else:
            cues.append(Segment(begin, finish, text))
    return cues


def transcript_to_text(transcript: Transcript) -> str:
    lines = [format_segment_line(s) for s in ordered_segments(transcript.segments)]
    return "\n".join(lines) + ("\n" if lines else "")


def write_transcript(txt_path: Path, json_path: Path, transcript: Transcript) -> None:
    txt_path.write_text(transcript_to_text(transcript), encoding="utf-8")
    json_path.write_text(
        json.dumps(transcript.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def read_transcript(json_path: Path) -> Transcript:
    return Transcript.from_dict(json.loads(json_path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class TranscriptStatus:
    """What ``transcript.json`` says about a recording without loading its segments."""

    exists: bool
    complete: bool
    transcribed_ms: int

    @property
    def pending(self) -> bool:
        """True while the recording still needs (more) transcription."""
        return not self.complete


def transcript_status(json_path: Path) -> TranscriptStatus:
    """``exists``/``complete``/``transcribed_ms`` of a transcript file (absent = pending)."""
    if not json_path.exists():
        return TranscriptStatus(exists=False, complete=False, transcribed_ms=0)
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        complete = bool(data.get("complete", True))
        transcribed = data.get("transcribed_ms")
        if transcribed is None:
            transcribed = max((int(s["end_ms"]) for s in data.get("segments", [])), default=0)
        return TranscriptStatus(exists=True, complete=complete, transcribed_ms=int(transcribed))
    except (OSError, ValueError, KeyError, TypeError):
        # Unreadable: treat as pending so that the editor transcribes it again.
        return TranscriptStatus(exists=True, complete=False, transcribed_ms=0)


def normalize_segments(segments: list[Segment]) -> list[Segment]:
    """Drop empty segments, clamp negative times and keep chronological order."""
    cleaned = []
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        start = max(0, seg.start_ms)
        end = max(start, seg.end_ms)
        cleaned.append(Segment(start, end, text, seg.track))
    cleaned.sort(key=lambda s: (s.start_ms, s.end_ms))
    return cleaned


def merge_segments(
    transcript: Transcript | None,
    segments: list[Segment],
    transcribed_ms: int,
    language: str | None = None,
) -> tuple[Transcript, list[Segment]]:
    """``transcript`` (or an empty one) with ``segments`` added as an incomplete
    transcript reaching ``transcribed_ms``; returns it and the segments that were new.

    Used by the editor's background transcription and by the transcription while
    recording; segments already stored and hallucinated credits are left out.
    """
    base = transcript or Transcript(language=None, segments=[], complete=False, transcribed_ms=0)
    known = set(base.segments)
    new = [s for s in drop_credit_hallucinations(normalize_segments(segments)) if s not in known]
    reached = max(int(transcribed_ms), base.transcribed_ms or 0, *(s.end_ms for s in new))
    merged = sorted([*base.segments, *new], key=lambda s: (s.start_ms, s.end_ms))
    return (
        Transcript(
            language=language or base.language,
            segments=merged,
            model=base.model,
            backend=base.backend,
            complete=False,
            transcribed_ms=reached,
        ),
        new,
    )


# Hallucinated credits -----------------------------------------------------------

# Whisper was trained on subtitled video and sometimes ends (or fills a silence) with
# the credits line of a subtitle file: "Titulky vytvořil JohnyX", "Subtitles by the
# Amara.org community", "Překlad: ...". Nobody says these while narrating their
# screen, so segments matching one of these patterns are dropped from the transcript.
CREDIT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # Czech / Slovak
        r"^\W*titulky\s+(vytvo[řr]il[aiy]?|p[řr]elo[žz]il[aiy]?|p[řr]ipravil[aiy]?|"
        r"zpracoval[aiy]?|nad[aá]bov[aá]l[aiy]?)\b",
        r"^\W*titulky\s*:\s*\S",
        r"^\W*(p[řr]eklad|korektur[ay])(\s+a\s+titulky)?\s*:\s*\S",
        r"^\W*(p[řr]elo[žz]il[aiy]?|otitulkoval[aiy]?)\s*:?\s*\S+\W*$",
        # English
        r"^\W*subtitles?\s+(by|made\s+by|created\s+by|provided\s+by|brought\s+to\s+you\s+by)\b",
        r"^\W*(transcribed|translated|translation|subtitled|captions?|captioned)\s+by\b",
        r"\bamara\.org\b",
        r"^\W*thanks?\s+(you\s+)?(so\s+much\s+)?for\s+watching\b",
        # German, French, Spanish, Italian, Polish, Portuguese
        r"^\W*untertitel(ung)?\s+(von|im\s+auftrag|der|durch)\b",
        r"^\W*sous-?titr(es|age)\s*(par|:|r[ée]alis[ée]s)\b",
        r"^\W*subt[ií]tulos\s+(por|realizados|creados|hechos)\b",
        r"^\W*sottotitoli\s+(di|a\s+cura\s+di|creati\s+da)\b",
        r"^\W*napisy\s+(stworzone|wykonane|przez|zrobione)\b",
        r"^\W*legendas?\s+(por|feitas?\s+por|criadas?\s+por)\b",
    )
)


def is_credit_hallucination(text: str) -> bool:
    """True when ``text`` looks like the credits line of a subtitle file."""
    normalized = " ".join(text.split())
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in CREDIT_PATTERNS)


def drop_credit_hallucinations(segments: list[Segment]) -> list[Segment]:
    """Remove segments that are hallucinated subtitle credits (see :data:`CREDIT_PATTERNS`)."""
    return [s for s in segments if not is_credit_hallucination(s.text)]


__all__ = [
    "CREDIT_PATTERNS",
    "LOWER_TRACK",
    "LOWER_TRACK_MARK",
    "UPPER_TRACK",
    "Segment",
    "Transcript",
    "TranscriptStatus",
    "display_cues",
    "drop_credit_hallucinations",
    "format_segment_line",
    "is_credit_hallucination",
    "merge_segments",
    "normalize_segments",
    "ordered_segments",
    "overlaps",
    "read_transcript",
    "transcript_status",
    "transcript_to_text",
    "write_transcript",
]
