"""Recording timeline: a single monotonic clock shared by frames, audio and transcript.

All timestamps in a recording are milliseconds since the recording started. The
recording never pauses, so the timeline is continuous.
"""

from __future__ import annotations

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
    """Monotonic clock measuring time since :meth:`start`."""

    _start_ns: int | None = field(default=None, repr=False)
    _stop_ns: int | None = field(default=None, repr=False)

    def start(self) -> None:
        self._start_ns = time.perf_counter_ns()
        self._stop_ns = None

    def stop(self) -> int:
        """Freeze the clock and return the total duration in milliseconds."""
        if self._start_ns is None:
            raise RuntimeError("clock not started")
        if self._stop_ns is None:
            self._stop_ns = time.perf_counter_ns()
        return self.elapsed_ms()

    @property
    def running(self) -> bool:
        return self._start_ns is not None and self._stop_ns is None

    def elapsed_ns(self) -> int:
        if self._start_ns is None:
            return 0
        end = self._stop_ns if self._stop_ns is not None else time.perf_counter_ns()
        return end - self._start_ns

    def elapsed_ms(self) -> int:
        return self.elapsed_ns() // 1_000_000

    def perf_counter_at(self, elapsed_ns: int) -> float:
        """Absolute ``perf_counter`` value for a point on the timeline (for sleeping)."""
        if self._start_ns is None:
            raise RuntimeError("clock not started")
        return (self._start_ns + elapsed_ns) / 1e9


__all__ = ["RecordingClock", "format_timestamp", "parse_timestamp"]
