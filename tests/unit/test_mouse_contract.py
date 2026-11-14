"""The platform contract end to end without an OS: fake sources -> JSONL -> actions.

Every real platform (Windows now, macOS and Linux later) must make the same scenarios
pass when its sources replace the fakes (see implementation-plan-mouse.md, ch. 9).
"""

from __future__ import annotations

from pathlib import Path

from mouse_fakes import FakeElement, FakeElementInspector, FakeMouseSource, FakeTouchpadSource

from video_story.core.config import Config, config_from_dict
from video_story.core.cursor import locate_cursor
from video_story.core.gestures import Contact, ContactFrame, GestureTracker
from video_story.core.input_events import (
    ButtonRecord,
    ContentRequest,
    ElementInfo,
    GestureRecord,
    MouseButton,
    MouseLogWriter,
    MoveRecord,
    MoveThrottle,
    RawMouseEvent,
    WheelCoalescer,
    WheelRecord,
    WindowInfo,
    read_mouse_log,
)
from video_story.core.manifest import Manifest, MouseInfo, read_manifest, write_manifest
from video_story.core.mouse_actions import derive_actions
from video_story.core.screens import Screen
from video_story.core.session import SessionPaths
from video_story.core.timeline import RecordingClock
from video_story.platform.base import Platform

SCREENS = [
    Screen("screen1", 0, 0, 0, 1920, 1080, True, 1.0),
    Screen("screen2", 1, 1920, 0, 1920, 1080, False, 1.0),
]


class MiniRecorder:
    """The smallest glue a recorder needs (the real one is capture/input_recorder, M2)."""

    def __init__(self, writer: MouseLogWriter, inspector: FakeElementInspector) -> None:
        self.writer = writer
        self.inspector = inspector
        self.wheels = WheelCoalescer()
        self.throttle = MoveThrottle()
        self.pressed = 0

    def _where(self, ev: RawMouseEvent) -> dict:
        pos = locate_cursor(SCREENS, ev.gx, ev.gy)
        return {
            "screen": pos.screen_id if pos else None,
            "x": pos.x if pos else None,
            "y": pos.y if pos else None,
            "gx": ev.gx,
            "gy": ev.gy,
        }

    def __call__(self, ev: RawMouseEvent) -> None:
        for done in self.wheels.due(ev.t_ms):
            self.writer.write(done)
        where = self._where(ev)
        win = None
        if ev.window is not None:
            info = self.inspector.window_info(ev.window)
            if info is not None:
                win = self.writer.window_id(ev.t_ms, ev.window, info)
        if ev.kind == "move":
            if self.pressed and self.throttle.accept(ev.t_ms, ev.gx, ev.gy):
                self.writer.write(MoveRecord(t=ev.t_ms, **where))
            return
        if ev.kind == "wheel":
            for done in self.wheels.add(
                WheelRecord(
                    t=ev.t_ms,
                    axis=ev.axis or "v",
                    delta=ev.delta,
                    mods=ev.modifiers,
                    win=win,
                    injected=ev.injected,
                    **where,
                )
            ):
                self.writer.write(done)
            return
        assert ev.button is not None
        seq = self.writer.write(
            ButtonRecord(
                t=ev.t_ms,
                kind=ev.kind,
                button=str(ev.button),
                mods=ev.modifiers,
                win=win,
                injected=ev.injected,
                **where,
            )
        )
        if ev.kind == "down":
            self.pressed += 1
            self.throttle.reset()
            info = self.inspector.element_at(ev.gx, ev.gy, ContentRequest.VALUE_AND_TEXT, 400)
            if info is not None:
                self.writer.write_element(ev.t_ms, seq, info, win=win)
        else:
            self.pressed = max(0, self.pressed - 1)

    def close(self) -> None:
        for done in self.wheels.flush():
            self.writer.write(done)


def _ev(kind, t, x, y, button=MouseButton.LEFT, **kw) -> RawMouseEvent:
    return RawMouseEvent(kind=kind, t_ms=t, gx=x, gy=y, button=button, window=7, **kw)


def test_platforms_offer_no_mouse_by_default():
    class Bare(Platform):
        def prepare_process(self): ...
        def support_status(self): ...
        def list_screens(self):
            return []

        def cursor_position(self):
            return None

    bare = Bare()
    assert bare.mouse_source() is None
    assert bare.element_inspector() is None
    assert bare.touchpad_source() is None


def test_fake_sources_through_file_to_actions(tmp_path: Path):
    paths = SessionPaths(tmp_path, "20261110-120000")
    writer = MouseLogWriter(paths.mouse_log_path(paths.work_dir))
    writer.open()
    inspector = FakeElementInspector(
        elements=[
            FakeElement(
                (100, 100, 300, 30),
                ElementInfo(role="edit", role_raw="Edit", name="Search"),
                value="video story",
            ),
            FakeElement(
                (2000, 100, 600, 400),
                ElementInfo(role="document", name="Notes"),
                text="Paragraph " * 600,
            ),
            FakeElement(
                (100, 300, 200, 30),
                ElementInfo(role="edit", name="pw", is_password=True),
                value="secret",
            ),
        ],
        windows={7: WindowInfo("Settings", "notepad.exe", 1)},
    )
    source = FakeMouseSource()
    recorder = MiniRecorder(writer, inspector)
    source.start(recorder, RecordingClock())

    # double click into the search field
    for t in (1000, 1300):
        source.emit(_ev("down", t, 150, 110))
        source.emit(_ev("up", t + 60, 150, 110))
    # a drag on the second screen
    source.emit(_ev("down", 3000, 2100, 200))
    for i in range(1, 30):
        source.emit(_ev("move", 3000 + i * 10, 2100 + i * 10, 200, button=None))
    source.emit(_ev("up", 3300, 2390, 200))
    # a click into the password field, then a scroll
    source.emit(_ev("down", 4000, 150, 310))
    source.emit(_ev("up", 4050, 150, 310))
    for i in range(6):
        source.emit(_ev("wheel", 5000 + i * 20, 900, 500, button=None, axis="v", delta=-40))
    source.emit(_ev("wheel", 7000, 900, 500, button=None, axis="v", delta=-120))
    recorder.close()
    source.stop()
    writer.close()

    log = read_mouse_log(paths.mouse_log_path(paths.work_dir))
    actions = derive_actions(log, FakeMouseSource.params, end_ms=8000)
    assert [a.kind for a in actions] == ["double_click", "drag", "click", "scroll", "scroll"]

    dbl, drag, pw, scroll, _ = actions
    element = log.element_for(dbl.ref)
    assert element is not None and element.name == "Search"
    assert log.content_of(element) == "video story"
    assert log.windows[element.win].title == "Settings"
    assert dbl.at.screen == "screen1" and (dbl.at.x, dbl.at.y) == (150, 110)

    assert drag.at.screen == "screen2" and drag.at.x == 180 and drag.end.x == 470
    doc = log.element_for(drag.ref)
    text = log.content_of(doc)
    assert doc.content["truncated"] and text.endswith("...") and len(text) == 4099

    pw_el = log.element_for(pw.ref)
    assert pw_el.password and log.content_of(pw_el) is None
    assert "secret" not in paths.mouse_log_path(paths.work_dir).read_text(encoding="utf-8")

    assert (scroll.direction, scroll.notches) == ("down", 2.0)
    assert writer.counts["events"] == len(log.buttons) + len(log.moves) + len(log.wheels)


def test_touchpad_frames_become_gesture_records(tmp_path: Path):
    pad = FakeTouchpadSource()
    tracker = GestureTracker(pad.geometry)
    found = []
    pad.start(lambda f: found.extend(tracker.add(f)), RecordingClock())
    for i in range(20):
        d = 400 + i * 90
        pad.emit(
            ContactFrame(i * 8, (Contact(3, 1500 - d // 2, 900), Contact(4, 1500 + d // 2, 900)))
        )
    pad.emit(ContactFrame(170, ()))
    (gesture,) = found
    record = GestureRecord(
        t=gesture.t_ms,
        until=gesture.until_ms,
        kind=gesture.kind,
        scale=gesture.scale,
        direction=gesture.direction,
        gx=10,
        gy=10,
    )
    assert record.kind == "pinch" and record.direction == "out" and record.end == 152


def test_mouse_setting_is_on_by_default_and_can_be_switched_off():
    assert Config().mouse_actions is True
    assert config_from_dict({"mouse_actions": False}).mouse_actions is False
    assert config_from_dict({"mouse_actions": "true"}).mouse_actions is True


def test_manifest_keeps_the_mouse_section(tmp_path: Path):
    base = Manifest(
        session_id="20261110-120000",
        started_at="",
        finished_at=None,
        recorded_duration_ms=1,
        capture_interval_ms=500,
        image_format="jpeg",
        jpeg_quality=70,
        screens=SCREENS,
    )
    path = tmp_path / "manifest.json"
    write_manifest(path, base)
    assert read_manifest(path).mouse is None
    info = MouseInfo(
        platform="fake",
        capabilities=FakeMouseSource.capabilities,
        params=FakeMouseSource.params,
        events=12,
        elements=3,
        contents=2,
        windows=1,
        element_timeouts=1,
        hook_restarts=1,
    )
    base.mouse = info
    write_manifest(path, base)
    loaded = read_manifest(path).mouse
    assert loaded == info
    assert loaded.file == "input/mouse.jsonl" and loaded.max_content_chars == 4096
    failed = MouseInfo.from_dict({"error": "hook could not be installed", "events": "x"})
    assert failed.error and failed.events == 0
    assert "error" in failed.to_dict()


def test_session_paths_know_the_input_folder(tmp_path: Path):
    paths = SessionPaths(tmp_path, "20261110-120000")
    assert paths.mouse_log_path(paths.work_dir) == paths.work_dir / "input" / "mouse.jsonl"
