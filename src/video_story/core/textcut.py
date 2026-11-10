"""Cutting long texts the same way everywhere: at most ``limit`` characters, then ``...``.

Used by the clipboard (sidecars) and by the content of elements under the mouse
(``input/mouse.jsonl``), on every operating system.
"""

from __future__ import annotations

ELLIPSIS = "..."


def truncate_text(text: str, limit: int) -> str:
    """``text`` unchanged when it is at most ``limit`` characters, else cut plus ``...``."""
    if len(text) <= limit:
        return text
    return text[:limit] + ELLIPSIS


def is_truncated(text: str, limit: int) -> bool:
    """Whether :func:`truncate_text` with ``limit`` would cut ``text``."""
    return len(text) > limit


__all__ = ["ELLIPSIS", "is_truncated", "truncate_text"]
