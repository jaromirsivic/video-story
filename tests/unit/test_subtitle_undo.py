"""The undo / redo history of the editor's subtitle changes (pure logic)."""

from __future__ import annotations

from video_story.core.transcript import Segment
from video_story.editor.subtitle_undo import Step, SubtitleHistory

A = Segment(0, 1000, "a")
B = Segment(200, 1200, "a")


def test_undo_then_redo_moves_the_step_between_the_stacks():
    history = SubtitleHistory()
    assert not history.can_undo and history.peek_undo() is None
    history.record((A,), (B,), "subtitle moved")
    assert history.peek_undo() == Step((A,), (B,), "subtitle moved")
    assert history.peek_undo().inverse() == Step((B,), (A,), "subtitle moved")
    history.undone()
    assert not history.can_undo and history.peek_redo() == Step((A,), (B,), "subtitle moved")
    history.redone()
    assert history.can_undo and not history.can_redo


def test_a_new_step_clears_redo_and_unchanged_steps_are_ignored():
    history = SubtitleHistory()
    history.record((A,), (B,))
    history.undone()
    history.record((), (A,), "subtitle added")
    assert not history.can_redo and len(history) == 1
    history.record((A,), (A,))
    assert len(history) == 1


def test_limit_and_clear():
    history = SubtitleHistory(limit=3)
    for k in range(5):
        history.record((Segment(k, k + 10, "x"),), ())
    assert len(history) == 3
    assert history.peek_undo().old == (Segment(4, 14, "x"),)
    history.clear("session")
    assert not history.can_undo and not history.can_redo and history.owner == "session"
