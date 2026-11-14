"""Search in the spoken text (the transcripts) of the recordings.

The search box under the recordings list looks for a word or a phrase exactly as typed,
but without regard to upper / lower case and to diacritics (``Prilis`` finds
``příliš``); runs of white space count as one space. A subtitle (transcript segment)
matches when its text contains the phrase. The recording shown in the editor is searched
first (its matching boxes on the narration track turn yellow at once); the others are
searched afterwards in a background thread, and every recording with a match gets a
yellow frame in the list. In the subtitle under the preview the found phrase is drawn
bold and red (:func:`match_spans` finds it in the original text).

Everything here is free of Qt except :class:`SearchJob`, the worker that reads the
transcripts of the other recordings.
"""

from __future__ import annotations

import logging
import threading
import unicodedata
from collections.abc import Iterable
from functools import lru_cache

from PySide6.QtCore import QObject, QRunnable, Signal

from video_story.core.session import SessionPaths
from video_story.core.transcript import read_transcript

from .workers import automatic_gc_paused

log = logging.getLogger(__name__)


@lru_cache(maxsize=8192)
def normalize(text: str) -> str:
    """``text`` folded for searching: no diacritics, case folded, single spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


def normalize_query(query: str | None) -> str:
    """The folded search phrase; ``""`` means "no search"."""
    return normalize(query or "")


def text_matches(text: str, folded_query: str) -> bool:
    """True when ``text`` contains the phrase (``folded_query`` from :func:`normalize_query`)."""
    return bool(folded_query) and folded_query in normalize(text)


def match_spans(text: str, folded_query: str) -> list[tuple[int, int]]:
    """Where the phrase occurs in ``text``: ``(start, end)`` character positions of the
    original text (end exclusive), found the same way as :func:`text_matches` - case,
    diacritics and runs of white space do not matter. Used to highlight the searched
    phrase in the subtitle under the preview."""
    if not folded_query or not text:
        return []
    folded: list[str] = []
    origin: list[int] = []  # index in ``text`` of each folded character
    for index, char in enumerate(text):
        if char.isspace():
            if folded and folded[-1] != " ":
                folded.append(" ")
                origin.append(index)
            continue
        decomposed = unicodedata.normalize("NFKD", char)
        stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
        for ch in stripped.casefold():
            folded.append(ch)
            origin.append(index)
    haystack = "".join(folded)
    spans: list[tuple[int, int]] = []
    start = haystack.find(folded_query)
    while start >= 0:
        end = start + len(folded_query)
        spans.append((origin[start], origin[end - 1] + 1))
        start = haystack.find(folded_query, end)
    return spans


def any_segment_matches(texts: Iterable[str], folded_query: str) -> bool:
    return bool(folded_query) and any(text_matches(text, folded_query) for text in texts)


def recording_matches(paths: SessionPaths, folded_query: str) -> bool:
    """True when the transcript of the recording at ``paths`` contains the phrase."""
    json_path = paths.transcript_json_path(paths.final_dir)
    if not folded_query or not json_path.exists():
        return False
    try:
        transcript = read_transcript(json_path)
    except (OSError, ValueError, KeyError) as exc:
        log.debug("search skips %s: %s", json_path, exc)
        return False
    return any_segment_matches((s.text for s in transcript.segments), folded_query)


class SearchSignals(QObject):
    # generation, session id of a recording whose transcript contains the phrase
    match = Signal(int, str)
    finished = Signal(int, int)  # generation, number of recordings searched


class SearchJob(QRunnable):
    """Searches the transcripts of ``recordings`` one after another (background)."""

    def __init__(
        self,
        generation: int,
        folded_query: str,
        recordings: list[SessionPaths],
        signals: SearchSignals,
        cancel: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._query = folded_query
        self._recordings = recordings
        self._signals = signals
        self._cancel = cancel or threading.Event()  # set: stop after the current one

    def run(self) -> None:
        searched = 0
        with automatic_gc_paused():
            for paths in self._recordings:
                if self._cancel.is_set():
                    break
                searched += 1
                try:
                    if recording_matches(paths, self._query):
                        self._signals.match.emit(self._generation, paths.session_id)
                except Exception:  # noqa: BLE001 - one broken recording must not stop the rest
                    log.exception("search in %s failed", paths.session_id)
        self._signals.finished.emit(self._generation, searched)


__all__ = [
    "SearchJob",
    "SearchSignals",
    "any_segment_matches",
    "match_spans",
    "normalize",
    "normalize_query",
    "recording_matches",
    "text_matches",
]
