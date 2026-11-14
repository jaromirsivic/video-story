"""Atomic file writes that survive Windows sharing rules.

Every JSON file the plugin shares between processes (``state.json``, ``crop.json``,
the editor session files, the configuration) is written to a ``.tmp`` sibling and
renamed over the target, so a reader never sees a half-written file. On Windows that
rename fails with ``PermissionError`` while *another* process has the target open:
Python's ``open()`` never grants ``FILE_SHARE_DELETE``, so the agent reading
``state.json`` (``video-story wait``/``status``, or the agent itself), the search
indexer or an antivirus scanner can block it for a few milliseconds. The remedy is to
retry for a while before giving up. The reader has the mirror problem: opening the
target while it is being replaced fails the same way, so reads retry as well.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

REPLACE_ATTEMPTS = 60
REPLACE_DELAY_S = 0.05  # 60 x 50 ms = 3 s before an open target counts as an error
READ_ATTEMPTS = 40
READ_DELAY_S = 0.025  # 40 x 25 ms = 1 s of replace storms before a read fails


def replace_with_retry(
    src: Path, dst: Path, attempts: int = REPLACE_ATTEMPTS, delay: float = REPLACE_DELAY_S
) -> None:
    """``os.replace`` that retries ``PermissionError`` (Windows sharing violations)."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def atomic_write_text(
    path: Path, text: str, encoding: str = "utf-8", newline: str | None = None
) -> None:
    """Write ``text`` to ``path`` through a ``.tmp`` sibling and an atomic rename.

    ``newline`` is passed to ``open``: ``"\\n"`` keeps LF line ends on Windows too (the
    default translates them to the platform's).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding, newline=newline)
    try:
        replace_with_retry(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_json_with_retry(path: Path, attempts: int | None = None) -> Any:
    """``json.loads`` of ``path``, retrying sharing violations and half-visible content.

    ``FileNotFoundError`` is raised at once (the file is really absent); other
    ``OSError`` and malformed JSON are retried, the last error is raised.
    """
    attempts = READ_ATTEMPTS if attempts is None else attempts
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(READ_DELAY_S)
    assert last is not None
    raise last


__all__ = [
    "READ_ATTEMPTS",
    "READ_DELAY_S",
    "REPLACE_ATTEMPTS",
    "REPLACE_DELAY_S",
    "atomic_write_text",
    "read_json_with_retry",
    "replace_with_retry",
]
