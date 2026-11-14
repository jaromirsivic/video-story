"""Bounded output for agents (host.agent_output): pages of lines and derived files."""

from __future__ import annotations

from pathlib import Path

import pytest

from video_story.core.fileio import atomic_write_text
from video_story.host import agent_output
from video_story.host.agent_output import (
    PAGE_DEFAULT_CHARS,
    PAGE_MAX_CHARS,
    PAGE_MIN_CHARS,
    clamp_page_chars,
    deliver_lines,
    fits_inline,
    output_target,
    page_lines,
    split_lines,
)

LINES = ["aaaa", "bb", "cccccc", "d"]  # 5 + 3 + 7 + 2 = 17 characters with line breaks


def test_page_takes_whole_lines_up_to_the_limit():
    page = page_lines(LINES, 1, 8)  # "aaaa\n" + "bb\n" is exactly 8
    assert page.lines == ("aaaa", "bb")
    assert page.text == "aaaa\nbb\n" and page.chars == 8
    assert (page.first_line, page.last_line, page.next_line) == (1, 2, 3)
    assert (page.total_lines, page.total_chars) == (4, 17)

    assert page_lines(LINES, 1, 7).lines == ("aaaa",)  # one character short of two lines
    assert page_lines(LINES, 1, 7).next_line == 2


def test_pages_follow_each_other_and_rebuild_the_text():
    seen: list[str] = []
    first: int | None = 1
    pages = 0
    while first is not None:
        page = page_lines(LINES, first, 8)
        assert page.lines, "a page is never empty while lines are left"
        seen.extend(page.lines)
        first = page.next_line
        pages += 1
    assert seen == LINES and pages == 3  # [aaaa, bb] [cccccc] [d]


def test_the_last_page_and_a_page_holding_everything_have_no_next_line():
    assert page_lines(LINES, 4, 8).lines == ("d",)
    assert page_lines(LINES, 4, 8).next_line is None
    whole = page_lines(LINES, 1, 17)
    assert whole.lines == tuple(LINES) and whole.next_line is None
    assert page_lines(LINES, 1, 16).next_line == 4


def test_a_line_longer_than_the_limit_comes_whole_and_alone():
    lines = ["short", "x" * 50, "tail"]
    page = page_lines(lines, 2, 10)
    assert page.lines == ("x" * 50,) and page.chars == 51
    assert page.next_line == 3
    first = page_lines(lines, 1, 10)  # the long line does not join a started page
    assert first.lines == ("short",) and first.next_line == 2
    assert page_lines(lines, 1, 0).lines == ("short",)  # even a zero limit moves on


def test_first_line_outside_the_text():
    past = page_lines(LINES, 5, 100)
    assert past.lines == () and past.next_line is None and past.text == ""
    assert (past.first_line, past.last_line, past.total_lines) == (5, 4, 4)
    assert page_lines(LINES, 0, 100).first_line == 1
    assert page_lines(LINES, -3, 100).lines == tuple(LINES)
    assert page_lines(LINES, "x", 100).first_line == 1  # type: ignore[arg-type]
    empty = page_lines([], 1, 100)
    assert empty.lines == () and empty.next_line is None and empty.total_chars == 0


def test_split_lines_splits_at_line_feeds_only():
    assert split_lines("") == []
    assert split_lines("a\nb\n") == ["a", "b"]
    assert split_lines("a\nb") == ["a", "b"]
    assert split_lines("a\n\nb\n") == ["a", "", "b"]
    odd = "copied   text \x0c here"
    assert split_lines(f"{odd}\nnext\n") == [odd, "next"]


def test_fits_inline_and_clamp():
    assert fits_inline(LINES, 17) and not fits_inline(LINES, 16)
    assert not fits_inline(LINES, 0) and not fits_inline([], 0)
    assert fits_inline([], 1)
    assert not fits_inline(["x" * 50], 10)  # one long line is still too long
    assert clamp_page_chars(5) == PAGE_MIN_CHARS
    assert clamp_page_chars(10**9) == PAGE_MAX_CHARS
    assert clamp_page_chars(2500) == 2500
    assert clamp_page_chars(None) == PAGE_DEFAULT_CHARS
    assert clamp_page_chars("many") == PAGE_DEFAULT_CHARS


def test_deliver_writes_lf_utf8_and_decides_about_the_inline_copy(tmp_path: Path):
    lines = ["100 SAY ř ￼", "200 CLICK"]
    target = tmp_path / "rec" / "agent" / "timeline.txt"
    small = deliver_lines(lines, target, 1000, "timeline", "plain text, one event per line")
    assert small.file == target and small.inline and small.note is None
    assert (small.lines, small.chars) == (2, 22)
    assert target.read_bytes() == "100 SAY ř ￼\n200 CLICK\n".encode()  # no CR
    assert not list(target.parent.glob("*.tmp"))
    assert deliver_lines(lines, target, 22, "timeline", "x").inline  # exactly the limit

    big = deliver_lines(lines, target, 21, "timeline", "plain text, one event per line")
    assert big.file == target and not big.inline
    assert "timeline_file" in big.note and "2 lines, 22 characters" in big.note
    assert not deliver_lines(lines, target, 0, "timeline", "x").inline

    empty = deliver_lines([], target, 0, "timeline", "x")
    assert target.read_bytes() == b"" and empty.lines == 0 and not empty.inline


def test_deliver_falls_back_to_inline_when_the_file_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder", encoding="utf-8")
    done = deliver_lines(["a" * 100], blocker / "agent" / "timeline.txt", 10, "timeline", "x")
    assert done.file is None and done.inline  # inline although longer than the limit
    assert "could not be written" in done.note and "--out" in done.note

    def read_only(*_args, **_kwargs):
        raise PermissionError(13, "read-only folder")

    monkeypatch.setattr(agent_output, "atomic_write_text", read_only)
    done = deliver_lines(["a"], tmp_path / "t.txt", 0, "mouse", "x")
    assert done.file is None and done.inline and "mouse_file" in done.note
    assert not (tmp_path / "t.txt").exists()


def test_output_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    default = tmp_path / "rec" / "agent" / "timeline.txt"
    assert output_target(None, default) == default
    assert output_target("", default) == default
    assert output_target(str(tmp_path / "x.txt"), default) == tmp_path / "x.txt"
    assert output_target(str(tmp_path), default) == tmp_path / "timeline.txt"  # a directory
    monkeypatch.chdir(tmp_path)
    assert output_target("work/tl.txt", default) == tmp_path / "work" / "tl.txt"


def test_atomic_write_text_keeps_lf_when_asked(tmp_path: Path):
    path = tmp_path / "lf.txt"
    atomic_write_text(path, "a\nb\n", newline="\n")
    assert path.read_bytes() == b"a\nb\n"
