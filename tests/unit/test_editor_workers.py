"""The editor's worker jobs keep cyclic garbage collection on the GUI thread."""

from __future__ import annotations

import gc

from video_story.editor import workers


def test_automatic_gc_paused_nests_and_restores(monkeypatch):
    gc.enable()
    with workers.automatic_gc_paused():
        assert not gc.isenabled()
        with workers.automatic_gc_paused():
            assert not gc.isenabled()
        assert not gc.isenabled(), "the outer job is still running"
    assert gc.isenabled()

    gc.disable()
    try:
        with workers.automatic_gc_paused():
            assert not gc.isenabled()
        assert not gc.isenabled(), "a collector that was off stays off"
    finally:
        gc.enable()


def test_collect_garbage_runs_a_collection():
    class Node:
        def __init__(self) -> None:
            self.me = self

    Node()
    assert workers.collect_garbage() >= 1
