"""Shared timeout contract. Tool-host budgets remain the caller's responsibility."""

import math

from video_story.host.errors import HostError

DEFAULT_WAIT_S = 45.0
MCP_MAX_WAIT_S = 240.0


def timeout_seconds(value, *, allow_zero: bool = True) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = math.nan
    if not math.isfinite(seconds) or seconds < 0 or (seconds == 0 and not allow_zero):
        raise HostError(
            "INVALID_TIMEOUT",
            "Timeout must be finite and " + ("non-negative." if allow_zero else "positive."),
            2,
        )
    return seconds
