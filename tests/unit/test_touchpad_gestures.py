"""Touchpad gestures end to end without hardware (M5): hybrid reports -> frames ->
protocol lines -> gesture records in input/mouse.jsonl -> timeline lines."""

from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pytest
from mouse_fakes import FakeElement, FakeElementInspector, FakeMouseSource, FakeTouchpadSource

from video_story.capture.input_recorder import InputRecorder
from video_story.core.gestures import Contact, ContactFrame
from video_story.core.input_events import (
    ContentRequest,
    ElementInfo,
    RawMouseEvent,
    WindowInfo,
    read_mouse_log,
)
from video_story.core.mouse_actions import derive_actions
from video_story.core.screens import Screen
from video_story.core.timeline import RecordingClock
from video_story.platform.windows_touchpad import (
    FrameAssembler,
    PadLine,
    TouchLine,
    format_pad,
    format_touch,
    parse_touch_line,
)

SCREENS = [Screen("screen1", 0, 0, 0, 2000, 1200, True, 1.0)]
OWN, APP, WIN = 4242, 999, 0x55


# Hybrid reports ---------------------------------------------------------------------


def test_hybrid_reports_are_put_back_together():
    asm = FrameAssembler()
    # scan 1: the count (2) comes with the first finger, the second finger in report two
    assert asm.add(2, [(0, 100, 200, True)]) is None
    assert asm.add(0, [(1, 900, 210, True)]) == [(0, 100, 200), (1, 900, 210)]
    # scan 2: both fingers in one report (parallel mode)
    assert asm.add(2, [(0, 110, 200, True), (1, 890, 210, True)]) == [(0, 110, 200), (1, 890, 210)]
    # the lift: both reported once more without the tip switch
    assert asm.add(2, [(0, 110, 200, False), (1, 890, 210, False)]) == []
    # a report without a started scan is ignored
    assert asm.add(0, [(5, 1, 1, True)]) is None


def test_touch_protocol_lines_roundtrip():
    assert parse_touch_line(format_pad(131271, 3679, 2261)) == PadLine(131271, 3679, 2261)
    line = parse_touch_line(format_touch(123, 7, [(0, 10, 20), (1, 30, 40)]))
    assert line == TouchLine(123, 7, (Contact(0, 10, 20), Contact(1, 30, 40)))
    assert parse_touch_line(format_touch(5, 7, [])) == TouchLine(5, 7, ())
    assert parse_touch_line("T 1 2 2 0 1 1") is None  # two contacts announced, one given
    assert parse_touch_line("PAD 1 2") is None
    assert parse_touch_line("123 513 0 0 0 0 0") is None


# Recorder ------------------------------------------------------------------------------


def pinch(t0: int, device: int = 0) -> list[ContactFrame]:
    frames = []
    for i in range(20):
        d = 400 + i * 90
        frames.append(
            ContactFrame(
                t0 + i * 8, (Contact(3, 1500 - d // 2, 900), Contact(4, 1500 + d // 2, 900)), device
            )
        )
    frames.append(ContactFrame(t0 + 170, (), device))
    return frames


@pytest.fixture
def recorder(tmp_path: Path):
    clock = RecordingClock()
    clock.start()
    source, pad = FakeMouseSource(), FakeTouchpadSource()
    inspector = FakeElementInspector(
        elements=[
            FakeElement((0, 0, 2000, 1200), ElementInfo(role="document", name="Map", pid=APP))
        ],
        windows={WIN: WindowInfo("Map - Browser", "msedge.exe", APP)},
    )
    rec = InputRecorder(
        tmp_path / "mouse.jsonl",
        SCREENS,
        source,
        clock,
        own_pid=OWN,
        inspector=inspector,
        touchpad=pad,
    )
    rec.start()
    yield rec, source, pad, inspector
    if rec.writer.is_open:
        rec.stop()


def test_a_pinch_is_written_where_the_pointer_was(recorder):
    rec, source, pad, inspector = recorder
    source.emit(RawMouseEvent(kind="move", t_ms=50, gx=640, gy=400, pid=APP, window=WIN))
    for frame in pinch(100):
        pad.emit(frame)
    deadline = time.monotonic() + 5
    while len(inspector.calls) < 1:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    info = rec.stop()
    log = read_mouse_log(rec.path)
    (gesture,) = log.gestures
    assert (gesture.kind, gesture.direction, gesture.t, gesture.end) == ("pinch", "out", 100, 252)
    assert (gesture.screen, gesture.x, gesture.y) == ("screen1", 640, 400)
    assert log.windows[gesture.win].process == "msedge.exe"
    assert inspector.calls == [(640, 400, ContentRequest.NONE)]
    assert log.element_for(gesture.seq).name == "Map"
    assert info.capabilities.touchpad_gestures
    (action,) = derive_actions(log)
    assert action.kind == "gesture" and action.win == gesture.win and action.touchpad


def test_gestures_over_video_story_are_left_out_and_a_touch_still_down_is_finished(recorder):
    rec, source, pad, _ = recorder
    source.emit(RawMouseEvent(kind="move", t_ms=50, gx=10, gy=10, pid=OWN, window=1))
    for frame in pinch(100)[:-1]:
        pad.emit(frame)
    source.emit(RawMouseEvent(kind="move", t_ms=400, gx=640, gy=400, pid=APP, window=WIN))
    for frame in pinch(500)[:-1]:  # fingers still on the pad when the recording stops
        pad.emit(frame)
    rec.stop()
    gestures = read_mouse_log(rec.path).gestures
    assert [(g.t, g.x) for g in gestures] == [(500, 640)]


def test_no_touchpad_means_no_gesture_capability(tmp_path: Path):
    class NoPad(FakeTouchpadSource):
        geometry = FakeTouchpadSource.geometry.__class__(0, 0)

    clock = RecordingClock()
    clock.start()
    rec = InputRecorder(tmp_path / "m.jsonl", SCREENS, FakeMouseSource(), clock, touchpad=NoPad())
    rec.start()
    for frame in pinch(100):
        rec._on_frame(frame)  # unknown geometry: ignored
    info = rec.stop()
    assert not info.capabilities.touchpad_gestures
    assert read_mouse_log(rec.path).gestures == []


# The Windows source reads touch lines -------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mouse source")
def test_windows_source_hands_touch_frames_to_the_touchpad_sink():
    from video_story.platform.windows_input import WindowsMouseSource, WindowsTouchpadSource

    script = textwrap.dedent(
        """
        import os, sys, time
        print("READY", os.getpid(), flush=True)
        print("PAD 77 3679 2261", flush=True)
        now = time.perf_counter_ns()
        print("T", now, 77, 2, 0, 100, 200, 1, 900, 210, flush=True)
        print("T", now + 8_000_000, 77, 0, flush=True)
        sys.stdin.read()
        """
    )
    mouse = WindowsMouseSource(
        command=lambda: [sys.executable, "-c", script], cursor=lambda: (0, 0)
    )
    pad = WindowsTouchpadSource(mouse)
    frames: list[ContactFrame] = []
    clock = RecordingClock()
    clock.start()
    pad.start(frames.append, clock)  # before the mouse, as InputRecorder does
    mouse.start(lambda e: None, clock)
    deadline = time.monotonic() + 10
    while len(frames) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    mouse.stop()
    pad.stop()
    assert pad.available and pad.geometry_of(77).width == 3679
    first, lift = frames
    assert first.device == 77 and [c.id for c in first.contacts] == [0, 1]
    assert lift.contacts == () and lift.t_ms - first.t_ms == 8


def test_gesture_lines_in_the_timeline(tmp_path: Path):
    from test_events import lines, make_recording

    from video_story.core.input_events import GestureRecord, MouseLogWriter
    from video_story.host.events import build_timeline

    paths = make_recording(tmp_path, {})
    writer = MouseLogWriter(paths.mouse_log_path(paths.final_dir))
    writer.open()
    win = writer.window_id(0, 1, WindowInfo("Map - Browser", "msedge.exe"))
    writer.write(
        GestureRecord(
            t=1000,
            until=1400,
            kind="pinch",
            scale=2.1,
            direction="out",
            screen="screen1",
            x=50,
            y=40,
            gx=50,
            gy=40,
            win=win,
        )
    )
    writer.write(
        GestureRecord(
            t=2000,
            until=2600,
            kind="scroll",
            direction="up",
            distance=0.4,
            screen="screen1",
            x=50,
            y=40,
            gx=50,
            gy=40,
            win=win,
        )
    )
    writer.close()
    gesture_lines = [line for line in lines(build_timeline(paths)) if "GESTURE" in line]
    assert gesture_lines == [
        "1000-1400 GESTURE pinch out 2 fingers x2.1 screen1 50,40 "
        'in "Map - Browser" [msedge.exe] (touchpad)',
        "2000-2600 GESTURE scroll up 2 fingers screen1 50,40 (touchpad)",
    ]
