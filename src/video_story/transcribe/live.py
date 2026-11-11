"""Transcription while recording: the audio is transcribed in windows as it grows.

Whisper works on windows of up to 30 s. :class:`LiveCore` takes the audio from the
point up to which the transcript is done (``done_ms``), plans the next window, has
whisper transcribe it and decides which segments are final:

* A window ends in a **pause** of the narration (:func:`plan_window`): first a pause
  in the mix, then (under loud computer sound) a pause of the microphone alone, and
  only when there is none a hard cut at 30 s. The longest pause after 20 s wins (else
  the longest after 12 s): whisper's encoder always works on 30 s, so a short window
  costs as much as a long one, and a long pause is most likely the end of a sentence.
* After a **hard cut** the last segment, which may be cut in half, is dropped and the
  next window starts at its beginning (:func:`commit_window`).
* A window without speech (:func:`has_speech`) is not given to whisper at all: no CPU
  and no hallucinated subtitle credits in silence.
* Once the first window with speech told it, every window gets the language of the
  recording, which then stays fixed. The text before is not passed as
  ``initial_prompt`` (:attr:`LiveParams.use_prompt`): measured in L0, it made whisper
  return a whole window as one long segment without improving the words.

``done_ms`` is at the same time the ``transcribed_ms`` of the stored transcript: the
editor resumes exactly there. Nothing here knows about processes or files of the
recording; :mod:`video_story.transcribe.live_worker` runs this in the worker process
and the benchmark (``scripts/live_benchmark.py``) on finished recordings.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

import numpy as np

from video_story.core.transcript import Segment, is_credit_hallucination

from .growing_wav import GrowingMix

log = logging.getLogger(__name__)

LiveMode = Literal["off", "light", "faster"]
LIVE_MODES: tuple[str, ...] = ("off", "light", "faster")

FRAME_MS = 20  # energy frames for pause and speech detection
SILENCE_DB = -90.0  # energy of digital silence
PROMPT_CHARS = 200
LANGUAGE_MIN_CHARS = 20  # text a window needs before its language is trusted


@dataclass(frozen=True)
class LiveParams:
    """Tunables of the windowing (measured in L0, see the live-transcription plan)."""

    min_window_ms: int = 12_000
    max_window_ms: int = 30_000
    prefer_from_ms: int = 20_000  # the cut prefers a pause after this (long windows)
    pause_ms: int = 400  # shortest pause a window may end in
    early_cut: bool = False  # end a window before the maximum is there (in a long pause)
    early_pause_ms: int = 800  # ... a pause at least this long
    guard_ms: int = 1_500  # after a hard cut, segments ending this close are redone
    restart_back_ms: int = 200
    noise_percentile: float = 10.0
    speech_margin_db: float = 12.0
    min_threshold_db: float = -60.0
    min_speech_ms: int = 250
    safety_ms: int = 300  # the newest audio is left alone (the tracks align within 100 ms)
    poll_ms: int = 2_000
    # The end of the text before as whisper's initial_prompt. Off: in L0 (three Czech
    # recordings) it changed the wording by no more than leaving it out, but whisper then
    # put a window into one or two long segments (12-13 s on average instead of 3-10 s),
    # which spoils the subtitles and the timeline.
    use_prompt: bool = False
    lock_language: bool = True


def live_threads(mode: str, cpus: int | None) -> int:
    """Whisper threads of a live mode: a quarter (light) or half (faster) of the CPUs."""
    count = max(1, cpus or 4)
    if mode == "faster":
        return max(2, min(16, count // 2))
    return max(2, min(8, count // 4))


# Energy ---------------------------------------------------------------------------


def frame_db(samples: np.ndarray, rate: int, frame_ms: int = FRAME_MS) -> np.ndarray:
    """RMS energy in dBFS of consecutive ``frame_ms`` frames (an incomplete last frame
    is left out)."""
    size = max(1, rate * frame_ms // 1000)
    frames = len(samples) // size
    if frames <= 0:
        return np.zeros(0, dtype=np.float64)
    block = samples[: frames * size].astype(np.float64).reshape(frames, size)
    rms = np.sqrt(np.mean(block * block, axis=1))
    return 20.0 * np.log10(np.maximum(rms, 1.0) / 32768.0)


class NoiseFloor:
    """Rolling estimate of the background level: a low percentile of the frame
    energies of the last few minutes. Speech is what rises clearly above it."""

    def __init__(self, params: LiveParams, history_frames: int = 15_000) -> None:
        self.params = params
        self._history: deque[np.ndarray] = deque()
        self._frames = 0
        self._limit = history_frames

    def add(self, db: np.ndarray) -> None:
        if len(db) == 0:
            return
        self._history.append(db)
        self._frames += len(db)
        while self._frames - len(self._history[0]) >= self._limit:
            self._frames -= len(self._history.popleft())

    def threshold(self, extra: np.ndarray | None = None) -> float:
        """Energy above which a frame counts as sound (speech)."""
        parts = list(self._history)
        if extra is not None and len(extra):
            parts.append(extra)
        if not parts:
            return self.params.min_threshold_db
        floor = float(np.percentile(np.concatenate(parts), self.params.noise_percentile))
        return max(floor + self.params.speech_margin_db, self.params.min_threshold_db)


def find_pauses(db: np.ndarray, threshold: float, min_frames: int) -> list[tuple[int, int]]:
    """Runs of at least ``min_frames`` frames below ``threshold`` as ``(first, end)``."""
    quiet = db < threshold
    runs: list[tuple[int, int]] = []
    start = None
    for index, value in enumerate(quiet):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= min_frames:
                runs.append((start, index))
            start = None
    if start is not None and len(quiet) - start >= min_frames:
        runs.append((start, len(quiet)))
    return runs


def has_speech(db: np.ndarray, threshold: float, params: LiveParams) -> bool:
    """True when the frames above ``threshold`` add up to ``min_speech_ms``."""
    return int(np.count_nonzero(db >= threshold)) * FRAME_MS >= params.min_speech_ms


# Windows --------------------------------------------------------------------------


class Cut(StrEnum):
    PAUSE = "pause"  # in a pause of the mix
    MIC_PAUSE = "mic_pause"  # in a pause of the microphone (the mix had none)
    HARD = "hard"  # no pause: cut at the maximum length
    END = "end"  # the rest of a finished recording


@dataclass(frozen=True)
class Window:
    start_ms: int
    end_ms: int
    cut: Cut

    @property
    def length_ms(self) -> int:
        return self.end_ms - self.start_ms


def _best_pause(
    db: np.ndarray,
    threshold: float,
    first: int,
    last: int,
    min_frames: int,
    prefer_from: int,
) -> int | None:
    """Frame in the middle of the pause to cut in: the longest pause (``min_frames``
    long within ``first .. last``) that starts at ``prefer_from`` or later, else the
    longest one at all (later wins a tie); ``None`` when there is none."""
    first, last = max(0, first), min(len(db), last)
    if last - first < min_frames:
        return None
    runs = [(first + a, first + b) for a, b in find_pauses(db[first:last], threshold, min_frames)]
    if not runs:
        return None
    preferred = [run for run in runs if run[0] >= prefer_from] or runs
    a, b = max(preferred, key=lambda run: (run[1] - run[0], run[0]))
    return (a + b) // 2


def plan_window(
    start_ms: int,
    available_ms: int,
    mix_db: np.ndarray,
    mic_db: np.ndarray,
    mix_threshold: float,
    mic_threshold: float,
    params: LiveParams,
    final: bool = False,
) -> Window | None:
    """The next window from ``start_ms`` or ``None`` while it should wait for audio.

    ``mix_db``/``mic_db`` are the frame energies from ``start_ms`` on (as far as the
    audio reaches, at most the maximum window). ``final``: the recording has ended and
    the rest is transcribed whatever its length.
    """
    length = available_ms - start_ms
    if length <= 0:
        return None
    if final and length <= params.max_window_ms:
        return Window(start_ms, available_ms, Cut.END)
    lo = params.min_window_ms // FRAME_MS
    prefer = params.prefer_from_ms // FRAME_MS
    if length >= params.max_window_ms:
        hi = params.max_window_ms // FRAME_MS
        pause_frames = max(1, params.pause_ms // FRAME_MS)
        middle = _best_pause(mix_db, mix_threshold, lo, hi, pause_frames, prefer)
        if middle is not None:
            return Window(start_ms, start_ms + middle * FRAME_MS, Cut.PAUSE)
        middle = _best_pause(mic_db, mic_threshold, lo, hi, pause_frames, prefer)
        if middle is not None:
            return Window(start_ms, start_ms + middle * FRAME_MS, Cut.MIC_PAUSE)
        return Window(start_ms, start_ms + params.max_window_ms, Cut.HARD)
    if not params.early_cut or length < params.min_window_ms:
        return None
    hi = length // FRAME_MS
    pause_frames = max(1, params.early_pause_ms // FRAME_MS)
    middle = _best_pause(mix_db, mix_threshold, lo, hi, pause_frames, prefer)
    if middle is not None:
        return Window(start_ms, start_ms + middle * FRAME_MS, Cut.PAUSE)
    return None


@dataclass
class Commit:
    segments: list[Segment]  # final segments of the window (absolute times)
    next_ms: int  # where the next window starts = transcribed_ms


def commit_window(
    window: Window, segments: list[Segment], done_ms: int, params: LiveParams
) -> Commit:
    """Which segments of a transcribed window are final and where the next one starts.

    Segment times are absolute and clamped to the window. A segment whose middle lies
    before ``done_ms`` was stored already and is dropped. After a hard cut the
    segments that end within ``guard_ms`` of the cut are redone by the next window,
    which starts at the first of them (never before the last kept segment ends); when
    that would leave nothing, everything is kept.
    """
    clamped: list[Segment] = []
    for seg in sorted(segments, key=lambda s: (s.start_ms, s.end_ms)):
        text = seg.text.strip()
        if not text:
            continue
        start = min(max(seg.start_ms, window.start_ms), window.end_ms)
        end = min(max(seg.end_ms, start), window.end_ms)
        if (start + end) // 2 < done_ms:
            continue
        clamped.append(Segment(start, end, text))
    if window.cut is not Cut.HARD or not clamped:
        return Commit(clamped, window.end_ms)
    limit = window.end_ms - params.guard_ms
    tail = next((i for i, seg in enumerate(clamped) if seg.end_ms > limit), None)
    if tail is None or tail == 0:
        return Commit(clamped, window.end_ms)
    keep = clamped[:tail]
    restart = max(clamped[tail].start_ms - params.restart_back_ms, keep[-1].end_ms)
    restart = min(max(restart, window.start_ms + 1), window.end_ms)
    return Commit(keep, restart)


def build_prompt(texts: list[str], max_chars: int = PROMPT_CHARS) -> str | None:
    """The end of the text so far as whisper's ``initial_prompt`` (whole sentences when
    possible); ``None`` when there is no text or it ends in a repetition loop."""
    recent = [" ".join(t.split()) for t in texts[-12:] if t.strip()]
    if not recent:
        return None
    if len(recent) >= 3 and len({t.casefold() for t in recent[-3:]}) == 1:
        return None
    joined = " ".join(recent)
    if len(joined) <= max_chars:
        return joined
    cut = joined[-max_chars:]
    for mark in (". ", "? ", "! "):
        index = cut.find(mark)
        if 0 <= index < len(cut) - 40:
            return cut[index + 2 :]
    space = cut.find(" ")
    return cut[space + 1 :] if 0 <= space < len(cut) - 40 else cut


# Running the windows ----------------------------------------------------------------


class SampleTranscriber:
    """What :class:`LiveCore` needs from a whisper backend."""

    def transcribe_samples(
        self,
        samples: np.ndarray,
        *,
        language: str | None,
        threads: int,
        initial_prompt: str | None = None,
    ) -> tuple[list[Segment], str | None]:  # pragma: no cover - protocol
        raise NotImplementedError


Resampler = Callable[[np.ndarray, int], np.ndarray]  # int16 at a rate -> int16 at 16 kHz


@dataclass
class WindowResult:
    window: Window
    segments: list[Segment]  # committed (final) segments
    done_ms: int  # = transcribed_ms after this window
    language: str | None
    speech: bool  # False: silence, whisper was not run
    seconds: float  # wall time of the window
    dropped: int = 0  # hallucinated credits removed


@dataclass
class LiveCore:
    """Plans and transcribes the windows of one recording (single threaded)."""

    mix: GrowingMix
    backend: SampleTranscriber
    resample: Resampler
    threads: int
    params: LiveParams = field(default_factory=LiveParams)
    done_ms: int = 0
    language: str | None = None
    texts: list[str] = field(default_factory=list)  # committed text (for the prompt)

    def __post_init__(self) -> None:
        self._mix_floor = NoiseFloor(self.params)
        self._mic_floor = NoiseFloor(self.params)

    def _frames(self, ms: int) -> int:
        return ms * self.mix.sample_rate // 1000

    def _energies(self, start_ms: int, end_ms: int) -> tuple[np.ndarray, np.ndarray]:
        first = self._frames(start_ms)
        count = self._frames(end_ms) - first
        mix, mic = self.mix.read(first, count)
        rate = self.mix.sample_rate
        return frame_db(mix, rate), frame_db(mic, rate)

    def next_window(self, available_ms: int, final: bool = False) -> Window | None:
        """The window to transcribe now, ``None`` while it should wait for audio."""
        if self.mix.sample_rate <= 0 or available_ms <= self.done_ms:
            return None
        end = min(available_ms, self.done_ms + self.params.max_window_ms)
        mix_db, mic_db = self._energies(self.done_ms, end)
        return plan_window(
            self.done_ms,
            available_ms,
            mix_db,
            mic_db,
            self._mix_floor.threshold(mix_db),
            self._mic_floor.threshold(mic_db),
            self.params,
            final=final,
        )

    def transcribe(self, window: Window) -> WindowResult:
        """Transcribe ``window`` and advance ``done_ms``."""
        started = time.monotonic()
        rate = self.mix.sample_rate
        first = self._frames(window.start_ms)
        mix, mic = self.mix.read(first, self._frames(window.end_ms) - first)
        mix_db, mic_db = frame_db(mix, rate), frame_db(mic, rate)
        threshold = self._mix_floor.threshold(mix_db)
        self._mix_floor.add(mix_db)
        self._mic_floor.add(mic_db)
        if not has_speech(mix_db, threshold, self.params):
            self.done_ms = window.end_ms
            return WindowResult(
                window, [], self.done_ms, self.language, False, time.monotonic() - started
            )
        samples = self.resample(mix, rate).astype(np.float32) / 32768.0
        prompt = build_prompt(self.texts) if self.params.use_prompt else None
        language = self.language if self.params.lock_language else None
        raw, detected = self.backend.transcribe_samples(
            samples, language=language or "auto", threads=self.threads, initial_prompt=prompt
        )
        absolute = [
            Segment(window.start_ms + s.start_ms, window.start_ms + s.end_ms, s.text) for s in raw
        ]
        kept = [s for s in absolute if not is_credit_hallucination(s.text)]
        commit = commit_window(window, kept, self.done_ms, self.params)
        if (
            self.params.lock_language
            and self.language is None
            and detected
            and sum(len(s.text) for s in commit.segments) >= LANGUAGE_MIN_CHARS
        ):
            self.language = detected
            log.info("live transcription: language %s from %d ms on", detected, window.start_ms)
        self.texts.extend(s.text for s in commit.segments)
        self.done_ms = commit.next_ms
        return WindowResult(
            window,
            commit.segments,
            self.done_ms,
            self.language or detected,
            True,
            time.monotonic() - started,
            dropped=len(absolute) - len(kept),
        )


__all__ = [
    "FRAME_MS",
    "LIVE_MODES",
    "Commit",
    "Cut",
    "LiveCore",
    "LiveMode",
    "LiveParams",
    "NoiseFloor",
    "SampleTranscriber",
    "Window",
    "WindowResult",
    "build_prompt",
    "commit_window",
    "find_pauses",
    "frame_db",
    "has_speech",
    "live_threads",
    "plan_window",
]
