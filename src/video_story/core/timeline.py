"""Recording timeline: a single monotonic clock shared by frames, audio and transcript.

All timestamps in a recording are milliseconds since the recording started. The time
a recording was paused (Pause) is cut out, so the timeline is continuous: it counts
only the recorded time.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


def format_timestamp(ms: int) -> str:
    """Format milliseconds as ``HH:MM:SS.mmm`` (hours grow beyond 99 if needed)."""
    if ms < 0:
        raise ValueError("timestamp must not be negative")
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def parse_timestamp(text: str) -> int:
    """Parse ``HH:MM:SS.mmm`` back into milliseconds."""
    try:
        hms, millis = text.strip().split(".")
        hours, minutes, seconds = (int(part) for part in hms.split(":"))
        return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(millis.ljust(3, "0")[:3])
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid timestamp {text!r}") from exc


@dataclass
class RecordingClock:
    """Monotonic clock measuring time since :meth:`start`, without the paused time.

    :meth:`pause` freezes the clock and :meth:`resume` lets it run on from where it
    stopped: the paused time is cut out of the timeline, which stays continuous.
    """

    _start_ns: int | None = field(default=None, repr=False)
    _stop_ns: int | None = field(default=None, repr=False)
    _paused_at_ns: int | None = field(default=None, repr=False)
    # Finished pauses as (from, to) perf_counter_ns pairs, oldest first.
    _pauses: list[tuple[int, int]] = field(default_factory=list, repr=False)
    _paused_ns: int = field(default=0, repr=False)  # total length of the finished pauses
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start(self) -> None:
        with self._lock:
            self._start_ns = time.perf_counter_ns()
            self._stop_ns = None
            self._paused_at_ns = None
            self._pauses = []
            self._paused_ns = 0

    def stop(self) -> int:
        """Freeze the clock and return the total duration in milliseconds (a pause still
        going on ends here)."""
        if self._start_ns is None:
            raise RuntimeError("clock not started")
        self.resume()
        with self._lock:
            if self._stop_ns is None:
                self._stop_ns = time.perf_counter_ns()
        return self.elapsed_ms()

    @property
    def running(self) -> bool:
        return self._start_ns is not None and self._stop_ns is None

    @property
    def paused(self) -> bool:
        return self._paused_at_ns is not None

    def pause(self) -> bool:
        """Freeze the clock; ``False`` when it is not running or already paused."""
        with self._lock:
            if self._start_ns is None or self._stop_ns is not None:
                return False
            if self._paused_at_ns is not None:
                return False
            self._paused_at_ns = time.perf_counter_ns()
            return True

    def resume(self) -> int | None:
        """Let a paused clock run on; returns how long the pause took (ns), ``None`` when
        it was not paused."""
        with self._lock:
            if self._paused_at_ns is None:
                return None
            now = time.perf_counter_ns()
            self._pauses.append((self._paused_at_ns, now))
            self._paused_ns += now - self._paused_at_ns
            self._paused_at_ns = None
            return now - self._pauses[-1][0]

    def elapsed_ns(self) -> int:
        with self._lock:
            if self._start_ns is None:
                return 0
            if self._paused_at_ns is not None:
                end = self._paused_at_ns
            elif self._stop_ns is not None:
                end = self._stop_ns
            else:
                end = time.perf_counter_ns()
            return end - self._start_ns - self._paused_ns

    def elapsed_ms(self) -> int:
        return self.elapsed_ns() // 1_000_000

    def ms_at(self, perf_counter_ns: int) -> int | None:
        """Recording time of a ``time.perf_counter_ns()`` stamp, also one taken in another
        process: the counter is system wide (QueryPerformanceCounter on Windows,
        CLOCK_MONOTONIC elsewhere). ``None`` before :meth:`start` and for a stamp taken
        while the clock was paused (nothing is recorded then)."""
        with self._lock:
            if self._start_ns is None:
                return None
            if self._paused_at_ns is not None and perf_counter_ns >= self._paused_at_ns:
                return None
            cut = 0
            for begin, end in self._pauses:
                if perf_counter_ns >= end:
                    cut += end - begin
                elif perf_counter_ns >= begin:
                    return None  # inside a pause
                else:
                    break
            return (perf_counter_ns - self._start_ns - cut) // 1_000_000

    def perf_counter_at(self, elapsed_ns: int) -> float:
        """Absolute ``perf_counter`` value for a point on the timeline (for sleeping);
        valid while the clock runs (the pauses so far are added)."""
        if self._start_ns is None:
            raise RuntimeError("clock not started")
        return (self._start_ns + self._paused_ns + elapsed_ns) / 1e9


__all__ = ["RecordingClock", "format_timestamp", "parse_timestamp"]
