"""Elements under the mouse (M3) with a fake inspector: when the recorder asks, what it
stores, and that a slow or broken inspector never costs the events."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from mouse_fakes import FakeElement, FakeElementInspector, FakeMouseSource

from video_story.capture import input_recorder as ir
from video_story.capture.input_recorder import InputRecorder
from video_story.core.input_events import (
    ContentRequest,
    ElementInfo,
    MouseButton,
    RawMouseEvent,
    WindowInfo,
    read_mouse_log,
)
from video_story.core.screens import Screen
from video_story.core.timeline import RecordingClock
from video_story.platform.base import PlatformError

SCREENS = [Screen("screen1", 0, 0, 0, 2000, 1200, True, 1.0)]
OWN = 4242
APP = 999
WIN = 0x1234


def ev(kind, t, x, y, button=MouseButton.LEFT, pid=APP, window=WIN, **kw) -> RawMouseEvent:
    return RawMouseEvent(kind=kind, t_ms=t, gx=x, gy=y, button=button, pid=pid, window=window, **kw)


def inspector() -> FakeElementInspector:
    return FakeElementInspector(
        elements=[
            FakeElement(
                (100, 100, 300, 30),
                ElementInfo(role="edit", role_raw="Edit", name="Search", pid=APP),
                value="video story",
                selection="story",
            ),
            FakeElement(
                (100, 300, 800, 400),
                ElementInfo(role="document", role_raw="Document", name="Notes", pid=APP),
                text="The paragraph under the mouse.",
                selection="paragraph under",
            ),
            FakeElement((100, 800, 200, 30), ElementInfo(role="button", name="Save", pid=APP)),
        ],
        windows={WIN: WindowInfo("Settings - Notepad", "notepad.exe", APP)},
    )


def _wait(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


@pytest.fixture
def setup(tmp_path: Path):
    clock = RecordingClock()
    clock.start()
    source = FakeMouseSource()
    insp = inspector()
    rec = InputRecorder(
        tmp_path / "mouse.jsonl", SCREENS, source, clock, own_pid=OWN, inspector=insp
    )
    rec.start()
    _wait(lambda: rec._elements_ok)
    yield rec, source, insp
    if rec.writer.is_open:
        rec.stop()


def _elements(rec) -> dict:
    return read_mouse_log(rec.path).elements


def test_a_press_stores_the_element_its_value_and_the_window(setup):
    rec, source, insp = setup
    source.emit(ev("down", 10, 150, 110))
    source.emit(ev("up", 60, 150, 110))
    _wait(lambda: len(insp.calls) == 1)
    info = rec.stop()
    log = read_mouse_log(rec.path)
    down = log.buttons[0]
    element = log.element_for(down.seq)
    assert element is not None and element.name == "Search" and element.role == "edit"
    assert log.content_of(element) == "video story" and element.content["kind"] == "value"
    assert element.win == down.win and log.windows[down.win].title == "Settings - Notepad"
    assert log.windows[down.win].process == "notepad.exe"
    assert insp.calls == [(150, 110, ContentRequest.VALUE_AND_TEXT)]
    assert info.elements == 1 and info.contents == 1 and info.windows == 1
    assert info.capabilities.elements and info.capabilities.element_content


def test_a_double_click_and_a_drag_read_the_selection_where_they_ended(setup):
    rec, source, insp = setup
    for t in (10, 200):  # double click into the document
        source.emit(ev("down", t, 300, 400))
        source.emit(ev("up", t + 50, 300, 400))
    source.emit(ev("down", 1000, 200, 350))  # a drag selecting text
    for i in range(1, 6):
        source.emit(ev("move", 1000 + i * 20, 200 + i * 40, 350, button=None))
    source.emit(ev("up", 1200, 400, 350))
    _wait(lambda: len(insp.calls) == 5)
    rec.stop()
    wants = [want for _, _, want in insp.calls]
    assert wants == [
        ContentRequest.VALUE_AND_TEXT,  # first press
        ContentRequest.VALUE_AND_TEXT,  # second press
        ContentRequest.SELECTION,  # double click done
        ContentRequest.VALUE_AND_TEXT,  # drag start
        ContentRequest.SELECTION,  # drag end
    ]
    log = read_mouse_log(rec.path)
    ups = [b for b in log.buttons if b.kind == "up"]
    assert log.content_of(log.element_for(ups[1].seq)) == "paragraph under"
    assert log.element_for(ups[2].seq).content["kind"] == "selection"
    assert log.element_for(ups[0].seq) is None  # a single click reads no selection
    first_down = log.buttons[0]
    assert log.content_of(log.element_for(first_down.seq)) == "The paragraph under the mouse."


def test_a_scroll_asks_once_at_its_start_without_content(setup):
    rec, source, insp = setup
    t = rec.clock.elapsed_ms()
    for i in range(5):
        source.emit(ev("wheel", t + i * 30, 300, 400, button=None, axis="v", delta=-120))
    source.emit(ev("wheel", t + 2000, 300, 400, button=None, axis="v", delta=-120))
    _wait(lambda: len(insp.calls) == 2)
    rec.stop()
    log = read_mouse_log(rec.path)
    assert [want for *_, want in insp.calls] == [ContentRequest.NONE] * 2
    first = min(log.wheels, key=lambda w: w.t)
    element = log.element_for(first.seq)
    assert element is not None and element.name == "Notes" and element.content is None


def test_a_rest_point_is_found_and_asked_about_once(setup):
    rec, source, insp = setup
    now = rec.clock.elapsed_ms()
    source.emit(ev("move", now, 120, 810, button=None))
    _wait(lambda: len(read_mouse_log(rec.path).hovers) == 1 or _flush(rec))
    time.sleep(1.0)  # still resting: no second rest point
    source.emit(ev("move", rec.clock.elapsed_ms(), 121, 811, button=None))  # within drag_px
    time.sleep(0.9)
    rec.stop()
    log = read_mouse_log(rec.path)
    assert len(log.hovers) == 1
    element = log.element_for(log.hovers[0].seq)
    assert element is not None and element.name == "Save" and element.role == "button"


def _flush(rec) -> bool:
    rec.writer.flush()
    return False


def test_video_story_windows_and_the_drawing_layer_are_never_asked_about(tmp_path: Path):
    clock = RecordingClock()
    clock.start()
    source, insp = FakeMouseSource(), inspector()
    rec = InputRecorder(
        tmp_path / "mouse.jsonl",
        SCREENS,
        source,
        clock,
        own_pid=OWN,
        drawing_windows=lambda: frozenset({77}),
        inspector=insp,
    )
    rec.start()
    _wait(lambda: rec._elements_ok)
    source.emit(ev("down", 10, 150, 110, pid=OWN, window=5))
    source.emit(ev("up", 50, 150, 110, pid=OWN, window=5))
    source.emit(ev("down", 100, 150, 110, pid=OWN, window=77))
    source.emit(ev("up", 150, 150, 110, pid=OWN, window=77))
    source.emit(ev("move", clock.elapsed_ms(), 150, 110, pid=OWN, window=5, button=None))
    time.sleep(0.9)
    rec.stop()
    assert insp.calls == []
    log = read_mouse_log(rec.path)
    assert all(b.win is None for b in log.buttons) and log.windows == {} and log.hovers == []


def test_timeouts_and_failures_are_counted_and_the_events_kept(tmp_path: Path):
    class Slow(FakeElementInspector):
        def element_at(self, gx, gy, want, timeout_ms):
            self.calls.append((gx, gy, want))
            if len(self.calls) == 1:
                raise TimeoutError("no answer")
            raise RuntimeError("provider crashed")

    clock = RecordingClock()
    clock.start()
    source, insp = FakeMouseSource(), Slow()
    rec = InputRecorder(
        tmp_path / "mouse.jsonl", SCREENS, source, clock, own_pid=OWN, inspector=insp
    )
    rec.start()
    source.emit(ev("down", 10, 150, 110))
    source.emit(ev("up", 30, 150, 110))
    source.emit(ev("down", 900, 600, 600))
    source.emit(ev("up", 930, 600, 600))
    _wait(lambda: len(insp.calls) == 2)
    info = rec.stop()
    log = read_mouse_log(rec.path)
    assert len(log.buttons) == 4 and info.element_timeouts == 1
    errors = [items[0].error for items in log.elements.values()]
    assert errors[0] == "timeout" and errors[1].startswith("failed: provider crashed")


def test_an_inspector_that_cannot_start_leaves_the_events(tmp_path: Path):
    class Broken(FakeElementInspector):
        def prepare(self):
            raise PlatformError("UI Automation unavailable")

    clock = RecordingClock()
    clock.start()
    source = FakeMouseSource()
    rec = InputRecorder(
        tmp_path / "mouse.jsonl", SCREENS, source, clock, own_pid=OWN, inspector=Broken()
    )
    rec.start()
    _wait(lambda: "elements_unavailable" in rec.path.read_text(encoding="utf-8") or _flush(rec))
    source.emit(ev("down", 10, 150, 110))
    source.emit(ev("up", 30, 150, 110))
    info = rec.stop()
    assert info.events == 2 and info.elements == 0
    assert not info.capabilities.elements


def test_a_full_queue_drops_rest_points_first(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ir, "MAX_PENDING_QUERIES", 3)
    rec = InputRecorder(
        tmp_path / "m.jsonl", SCREENS, FakeMouseSource(), RecordingClock(), inspector=inspector()
    )
    queries = [
        ir._Query(1, 0, 0, ContentRequest.VALUE_AND_TEXT, None),
        ir._Query(2, 0, 0, ContentRequest.VALUE_AND_TEXT, None, hover=True),
        ir._Query(3, 0, 0, ContentRequest.VALUE_AND_TEXT, None),
        ir._Query(4, 0, 0, ContentRequest.VALUE_AND_TEXT, None),
        ir._Query(5, 0, 0, ContentRequest.VALUE_AND_TEXT, None),
    ]
    for q in queries:
        rec._enqueue(q)
    assert [q.ref for q in rec._queries] == [3, 4, 5]
    assert rec._dropped_queries == 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows helper protocol")
def test_uia_helper_json_becomes_an_element_with_core_limits():
    from video_story.platform.windows_uia import element_from_json

    info = element_from_json(
        {
            "role_raw": "Hyperlink",
            "name": "Docs",
            "bounds": [1, 2, 3, 4],
            "ancestors": [["Pane", "Main"], ["Window", "Browser"], ["bad"]],
            "pid": 12,
            "process": "msedge.exe",
            "content": {"kind": "text", "text": "x" * 4097, "chars": 9000},
        }
    )
    assert info.role == "link" and info.bounds == (1, 2, 3, 4)
    assert [(a.role, a.name) for a in info.ancestors] == [("pane", "Main"), ("window", "Browser")]
    assert info.content.text == "x" * 4096 + "..." and info.content.chars == 9000
    assert info.content.truncated
