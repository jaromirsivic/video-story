"""Recording the mouse during a recording (M2) with fake sources: the file, targets,
the Recorder, Settings-independent failures and crash recovery."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from mouse_fakes import FakeMouseSource

from video_story.capture import recorder as recorder_mod
from video_story.capture.input_recorder import InputRecorder
from video_story.capture.recorder import Recorder
from video_story.capture.screen_grabber import RawFrame
from video_story.core.config import Config
from video_story.core.input_events import MouseButton, RawMouseEvent, read_mouse_log
from video_story.core.manifest import MouseInfo, read_manifest
from video_story.core.mouse_actions import derive_actions
from video_story.core.screens import RawScreen, Screen
from video_story.core.session import SessionPaths
from video_story.core.timeline import RecordingClock
from video_story.platform.base import Platform, PlatformError, SupportStatus
from video_story.platform.windows_mouse_hook import parse_line

SESSION = "20261110-120000"
SCREENS = [
    Screen("screen1", 0, 0, 0, 100, 80, True, 1.0),
    Screen("screen2", 1, 100, 0, 100, 80, False, 1.0),
]
OWN_PID = 4242


def ev(kind, t, x, y, button=MouseButton.LEFT, pid=999, window=1, **kw) -> RawMouseEvent:
    return RawMouseEvent(kind=kind, t_ms=t, gx=x, gy=y, button=button, pid=pid, window=window, **kw)


# Helper protocol -------------------------------------------------------------------


def test_hook_lines_are_parsed():
    down = parse_line("123456789 513 -1500 300 0 0 1")
    assert down is not None and (down.kind, down.button, down.x, down.mods) == (
        "down",
        "left",
        -1500,
        ("ctrl",),
    )
    assert parse_line("1 517 0 0 0 1 0").button == "right"  # WM_RBUTTONUP, injected flag
    assert parse_line("1 517 0 0 0 1 0").injected
    assert parse_line("1 523 0 0 2 0 0").button == "x2"
    wheel = parse_line("1 522 5 5 -120 0 2")
    assert (wheel.kind, wheel.axis, wheel.delta, wheel.mods) == ("wheel", "v", -120, ("shift",))
    assert parse_line("1 526 5 5 30 0 0").axis == "h"
    assert parse_line("1 512 5 5 0 0 0").kind == "move"
    assert parse_line("READY 1234") is None
    assert parse_line("1 523 0 0 7 0 0") is None  # unknown X button
    assert parse_line("1 999 0 0 0 0 0") is None
    assert parse_line("garbage a b c d e f") is None


# InputRecorder ----------------------------------------------------------------------


@pytest.fixture
def started(tmp_path: Path):
    clock = RecordingClock()
    clock.start()
    source = FakeMouseSource()
    drawing = frozenset({77})
    rec = InputRecorder(
        tmp_path / "input" / "mouse.jsonl",
        SCREENS,
        source,
        clock,
        own_pid=OWN_PID,
        drawing_windows=lambda: drawing,
    )
    rec.start()
    yield rec, source
    if rec.writer.is_open:
        rec.stop()


def test_events_are_written_with_screen_target_and_drag_path(started):
    rec, source = started
    source.emit(ev("move", 5, 10, 10, button=None))  # no button down: not stored
    source.emit(ev("down", 10, 10, 10))
    for i in range(1, 6):
        source.emit(ev("move", 10 + i * 20, 10 + i * 30, 10, button=None))
    source.emit(ev("up", 200, 160, 10))  # released on screen2
    source.emit(ev("down", 300, 50, 50, pid=OWN_PID, window=5))  # Video Story's own window
    source.emit(ev("up", 320, 50, 50, pid=OWN_PID, window=5))
    source.emit(ev("down", 400, 60, 60, pid=OWN_PID, window=77))  # the drawing layer
    source.emit(ev("up", 420, 60, 60, pid=OWN_PID, window=77))
    info = rec.stop()

    log = read_mouse_log(rec.path)
    assert [(b.kind, b.target) for b in log.buttons] == [
        ("down", "app"),
        ("up", "app"),
        ("down", "video_story"),
        ("up", "video_story"),
        ("down", "drawing"),
        ("up", "drawing"),
    ]
    assert len(log.moves) == 5 and log.moves[0].t == 30
    up = log.buttons[1]
    assert (up.screen, up.x, up.y, up.gx) == ("screen2", 60, 10, 160)
    assert [s.state for s in log.statuses] == ["started", "stopped"]
    assert info.events == 11 and info.platform == "fake" and info.error is None
    assert info.params.double_click_ms == 480
    (drag, own, drawing) = derive_actions(log, info.params)
    assert drag.kind == "drag" and drag.end.screen == "screen2"
    assert (own.target, drawing.target) == ("video_story", "drawing")


def test_wheel_lines_are_closed_by_time_without_another_event(started):
    rec, source = started
    t = rec.clock.elapsed_ms()
    for i in range(3):
        source.emit(ev("wheel", t + i * 10, 20, 20, button=None, axis="v", delta=-120))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        rec.writer.flush()
        if '"type":"wheel"' in rec.path.read_text(encoding="utf-8"):
            break
        time.sleep(0.05)
    log = read_mouse_log(rec.path)
    assert len(log.wheels) == 1 and log.wheels[0].delta == -360 and log.wheels[0].count == 3


def test_status_from_the_source_is_written(started):
    rec, source = started
    source.on_status("restarted", "no events for 5 s while the pointer moved")
    rec.stop()
    states = [s.state for s in read_mouse_log(rec.path).statuses]
    assert states == ["started", "restarted", "stopped"]


def test_a_source_that_cannot_start_leaves_no_file(tmp_path: Path):
    class Refuses(FakeMouseSource):
        def start(self, sink, clock, on_status=None):
            raise PlatformError("mouse hook not installed: blocked")

    clock = RecordingClock()
    clock.start()
    rec = InputRecorder(tmp_path / "input" / "mouse.jsonl", SCREENS, Refuses(), clock)
    with pytest.raises(PlatformError):
        rec.start()
    assert not rec.path.exists()
    assert rec.info(error="blocked").error == "blocked"


# Recorder ----------------------------------------------------------------------------


class Grabber:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def close(self) -> None:
        pass

    def grab(self, screen) -> RawFrame:
        return RawFrame(
            screen.id, screen.width, screen.height, bytes(screen.width * screen.height * 4)
        )


class MousePlatform(Platform):
    name = "fake"

    def __init__(self, source: FakeMouseSource | None) -> None:
        self.source = source

    def prepare_process(self) -> None:
        pass

    def support_status(self) -> SupportStatus:
        return SupportStatus(True)

    def list_screens(self):
        return [RawScreen(0, 0, 8, 6, primary=True)]

    def cursor_position(self):
        return (1, 1)

    def mouse_source(self):
        return self.source


def _recorder(tmp_path: Path, monkeypatch, source, **config) -> Recorder:
    monkeypatch.setattr(recorder_mod, "ScreenGrabber", Grabber)
    cfg = Config(recording_folder=tmp_path, capture_interval_ms=20, image_format="png", **config)
    return Recorder(cfg, MousePlatform(source), SessionPaths(tmp_path, SESSION), record_audio=False)


def test_recorder_writes_the_mouse_and_the_manifest_section(tmp_path: Path, monkeypatch):
    source = FakeMouseSource()
    rec = _recorder(tmp_path, monkeypatch, source)
    rec.start()
    deadline = time.monotonic() + 5
    while not source.running:  # the source starts on a thread of its own
        assert time.monotonic() < deadline
        time.sleep(0.01)
    provisional = read_manifest(rec.paths.manifest_path(rec.paths.work_dir))
    assert provisional.mouse is not None and provisional.mouse.platform == "fake"
    t = rec.clock.elapsed_ms()
    source.emit(ev("down", t, 2, 2))
    source.emit(ev("up", t + 50, 2, 2))
    summary = rec.stop()
    assert not source.running
    assert summary.mouse is not None and summary.mouse.events == 2
    manifest = summary.to_manifest(rec.config, "fake")
    assert manifest.to_dict()["mouse"]["file"] == "input/mouse.jsonl"
    log = read_mouse_log(rec.paths.mouse_log_path(rec.paths.work_dir))
    assert [a.kind for a in derive_actions(log)] == ["click"]


def test_mouse_switched_off_or_missing_records_nothing(tmp_path: Path, monkeypatch):
    source = FakeMouseSource()
    rec = _recorder(tmp_path, monkeypatch, source, mouse_actions=False)
    rec.start()
    assert not source.running
    assert rec.stop().mouse is None
    assert not rec.paths.mouse_log_path(rec.paths.work_dir).exists()

    other = tmp_path / "other"
    rec = _recorder(other, monkeypatch, None)
    rec.start()
    assert rec.stop().mouse is None


def test_a_mouse_that_fails_never_stops_the_recording(tmp_path: Path, monkeypatch):
    class Refuses(FakeMouseSource):
        def start(self, sink, clock, on_status=None):
            raise PlatformError("mouse hook not installed: blocked by policy")

    rec = _recorder(tmp_path, monkeypatch, Refuses())
    rec.start()
    deadline = time.monotonic() + 5
    while rec.frame_count < 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    summary = rec.stop()
    assert summary.error is None and summary.frame_count >= 2
    assert summary.mouse is not None and "blocked by policy" in summary.mouse.error
    assert summary.to_manifest(rec.config, "fake").to_dict()["mouse"]["error"]


# Recovery ------------------------------------------------------------------------------


def test_recovery_counts_the_mouse_file(tmp_path: Path):
    from video_story.host.recovery import _mouse_info

    path = tmp_path / "mouse.jsonl"
    path.write_text(
        '{"seq":1,"t":0,"type":"status","state":"started"}\n'
        '{"seq":2,"t":10,"type":"down","button":"left","gx":1,"gy":1,"target":"app"}\n'
        '{"seq":3,"t":20,"type":"up","button":"left","gx":1,"gy":1,"target":"app"}\n'
        '{"seq":4,"t":30,"type":"status","state":"restarted","reason":"x"}\n'
        '{"seq":5,"t":40,"type":"wheel","axis":"v","delta":-120,"gx":1,"gy":1,"tar',
        encoding="utf-8",
    )
    info = _mouse_info(path, MouseInfo(platform="windows-ll-hook"))
    assert info is not None and info.events == 2 and info.hook_restarts == 1
    assert info.platform == "windows-ll-hook"
    assert _mouse_info(tmp_path / "missing.jsonl", MouseInfo()) is None
    failed = MouseInfo(error="blocked")
    assert _mouse_info(tmp_path / "missing.jsonl", failed) is failed
