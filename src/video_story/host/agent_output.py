"""Bounded output for agents: pages of lines, and files instead of huge JSON strings.

Agent hosts cut the output of a command (Claude Code at about 30 000 characters, Codex
and Cursor earlier) and the agent then acts on a part without knowing. So nothing a
command prints may grow with the length of the recording:

* the CLI writes such a text to a file the agent reads with its own file tool
  (:func:`deliver_lines`: ``<recording_dir>/agent/`` or ``--out``) and repeats it in the
  JSON only when it is short (``--inline-chars``);
* the MCP server, whose hosts cannot read files, returns it page by page
  (:func:`page_lines`: ``first_line`` / ``max_chars`` / ``next_line``).

Both use :func:`page_lines`, so "fits" means the same everywhere. Pages are numbered by
line, not by time: several events share one millisecond, and the text is rebuilt
identically by every call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_story.core.fileio import atomic_write_text

DEFAULT_INLINE_CHARS = 12_000  # CLI: a text up to this long is also returned inline
PAGE_DEFAULT_CHARS = 20_000  # MCP: characters of one page
PAGE_MIN_CHARS = 1_000
PAGE_MAX_CHARS = 100_000

TIMELINE_FILE_NAME = "timeline.txt"
MOUSE_DETAIL_FILE_NAME = "mouse.jsonl"


@dataclass(frozen=True)
class Page:
    """Consecutive lines of a text; ``next_line`` is ``None`` when it ends the text."""

    lines: tuple[str, ...]
    first_line: int  # 1-based number of the first line of the page
    next_line: int | None  # where the following page starts
    total_lines: int
    total_chars: int  # of the whole text, every line with its line break

    @property
    def text(self) -> str:
        return "".join(f"{line}\n" for line in self.lines)

    @property
    def chars(self) -> int:
        return sum(len(line) + 1 for line in self.lines)

    @property
    def last_line(self) -> int:
        """Number of the last line of the page (``first_line - 1`` for an empty page)."""
        return self.first_line + len(self.lines) - 1


def split_lines(text: str) -> list[str]:
    """Lines of ``text`` split at ``\\n`` only (``str.splitlines`` also splits at
    U+2028, form feeds, ... which a copied text may contain inside one event)."""
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def clamp_page_chars(max_chars: Any) -> int:
    """Keep the size of a page within sensible limits (a wrong value gives the default)."""
    try:
        value = int(max_chars)
    except (TypeError, ValueError):
        value = PAGE_DEFAULT_CHARS
    return max(PAGE_MIN_CHARS, min(PAGE_MAX_CHARS, value))


def page_lines(
    lines: Sequence[str], first_line: int = 1, max_chars: int = PAGE_DEFAULT_CHARS
) -> Page:
    """The lines from ``first_line`` (1-based) that fit into ``max_chars`` characters.

    Every line counts with its line break. The text is cut between lines only: a line
    longer than ``max_chars`` comes whole and alone, so a page is never empty while
    lines are left and following ``next_line`` always reaches the end. A ``first_line``
    past the end gives an empty page.
    """
    try:
        first = max(1, int(first_line))
    except (TypeError, ValueError):
        first = 1
    total = len(lines)
    taken: list[str] = []
    used = 0
    for line in lines[first - 1 :]:
        size = len(line) + 1
        if taken and used + size > max_chars:
            break
        taken.append(line)
        used += size
    end = first - 1 + len(taken)
    return Page(
        lines=tuple(taken),
        first_line=first,
        next_line=end + 1 if end < total else None,
        total_lines=total,
        total_chars=sum(len(line) + 1 for line in lines),
    )


def fits_inline(lines: Sequence[str], inline_chars: int) -> bool:
    """The whole text is at most ``inline_chars`` characters (``0``: never inline)."""
    if inline_chars <= 0:
        return False
    page = page_lines(lines, 1, inline_chars)
    return page.next_line is None and page.chars <= inline_chars


def output_target(out: str | None, default: Path) -> Path:
    """Where a derived file goes: ``default``, or the ``--out`` path (absolute; an
    existing directory gets the default file name)."""
    if not out:
        return default
    target = Path(out).expanduser()
    if target.is_dir():
        target = target / default.name
    return target.absolute()


@dataclass(frozen=True)
class Delivery:
    """What :func:`deliver_lines` did with a text."""

    file: Path | None  # the written file; None when it could not be written
    lines: int
    chars: int
    inline: bool  # the caller returns the text in the JSON as well
    note: str | None  # one sentence for the agent when there is something to know


def deliver_lines(
    lines: Sequence[str],
    target: Path,
    inline_chars: int,
    name: str,
    what: str,
) -> Delivery:
    """Write ``lines`` to ``target`` (UTF-8, LF, atomically) and decide about the inline copy.

    ``name`` is the field prefix (``timeline`` -> ``timeline_file``), ``what`` describes
    the file for the note (``plain text, one event per line``). The text is inline when it
    has at most ``inline_chars`` characters - and whatever its size when the file could
    not be written (a read-only folder), so the command never fails for that.
    """
    count = len(lines)
    chars = sum(len(line) + 1 for line in lines)
    size = f"{count} lines, {chars} characters"
    try:
        atomic_write_text(target, "".join(f"{line}\n" for line in lines), newline="\n")
    except OSError as exc:
        return Delivery(
            file=None,
            lines=count,
            chars=chars,
            inline=True,
            note=(
                f"`{name}_file` could not be written ({type(exc).__name__}: {exc}), so the "
                f"whole text ({size}) is inline whatever its size; pass --out <path> in a "
                "folder you can write to."
            ),
        )
    inline = fits_inline(lines, inline_chars)
    note = None
    if not inline:
        note = (
            f"Not inline ({size}): read `{name}_file` with your file reading tool - "
            f"{what}, so it can be read in parts and searched."
        )
    return Delivery(file=target, lines=count, chars=chars, inline=inline, note=note)


__all__ = [
    "DEFAULT_INLINE_CHARS",
    "MOUSE_DETAIL_FILE_NAME",
    "PAGE_DEFAULT_CHARS",
    "PAGE_MAX_CHARS",
    "PAGE_MIN_CHARS",
    "TIMELINE_FILE_NAME",
    "Delivery",
    "Page",
    "clamp_page_chars",
    "deliver_lines",
    "fits_inline",
    "output_target",
    "page_lines",
    "split_lines",
]
