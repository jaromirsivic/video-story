"""Master instructions: what the user wants done with the whole recording.

The **Instructions** button of the editor opens a text box; its content is stored next
to the recording as ``instructions.txt`` and handed to the agent with the crop (the
``process`` event, the final ``state.json`` and ``describe``). It frames everything the
user said in the recording ("create an implementation plan", "answer in Czech", ...):
the agent reads it first and applies it to the cropped transcript and frames. Empty by
default; an empty text removes the file.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .fileio import atomic_write_text

log = logging.getLogger(__name__)

MAX_INSTRUCTIONS_CHARS = 20_000


def normalize_instructions(text: str | None) -> str:
    """Strip surrounding blank lines and normalise line endings; ``None`` becomes ``""``."""
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()[:MAX_INSTRUCTIONS_CHARS]


def read_instructions(path: Path) -> str:
    """The stored instructions, ``""`` when there are none (or the file is unreadable)."""
    if not path.exists():
        return ""
    try:
        return normalize_instructions(path.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("cannot read %s: %s", path, exc)
        return ""


def write_instructions(path: Path, text: str | None) -> str:
    """Store ``text`` (atomically); an empty text deletes the file. Returns what was stored."""
    text = normalize_instructions(text)
    if not text:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("cannot remove %s: %s", path, exc)
            raise
        return ""
    atomic_write_text(path, text + "\n")
    return text


__all__ = [
    "MAX_INSTRUCTIONS_CHARS",
    "normalize_instructions",
    "read_instructions",
    "write_instructions",
]
