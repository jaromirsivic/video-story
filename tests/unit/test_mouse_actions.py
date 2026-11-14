"""Clicks, double clicks, drags, scrolls and gesture merging derived from raw records."""

from __future__ import annotations

from video_story.core.input_events import (
    ButtonRecord,
    GestureRecord,
    MouseLog,
    MouseSystemParams,
    MoveRecord,
    WheelRecord,
)
from video_story.core.mouse_actions import derive_actions, path_length, simplify_path

PARAMS = MouseSystemParams(double_click_ms=480, double_click_px=(4, 4), drag_px=(4, 4))


class Log:
    """Builds a MouseLog with increasing seq numbers."""

    def __init__(self) -> None:
        self.log = MouseLog()
        self.seq = 0

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def down(self, t, x, y, button="left", **kw):
        self.log.add(
            ButtonRecord(
                seq=self._next(),
                t=t,
                kind="down",
                button=button,
                screen="screen1",
                x=x,
                y=y,
                gx=x,
                gy=y,
                **kw,
            )
        )
        return self

    def up(self, t, x, y, button="left", **kw):
        self.log.add(
            ButtonRecord(
                seq=self._next(),
                t=t,
                kind="up",
                button=button,
                screen="screen1",
                x=x,
                y=y,
                gx=x,
                gy=y,
                **kw,
            )
        )
        return self

    def click(self, t, x, y, button="left", hold=50):
        return self.down(t, x, y, button).up(t + hold, x, y, button)

    def move(self, t, x, y):
        self.log.add(MoveRecord(seq=self._next(), t=t, gx=x, gy=y))
        return self

    def wheel(self, t, delta, axis="v", mods=(), until=None):
        self.log.add(
            WheelRecord(
                seq=self._next(), t=t, axis=axis, delta=delta, mods=mods, until=until, gx=1, gy=1
            )
        )
        return self

    def gesture(self, t, until, kind="pinch", **kw):
        self.log.add(GestureRecord(seq=self._next(), t=t, until=until, kind=kind, **kw))
        return self

    def actions(self, end_ms=None):
        return derive_actions(self.log, PARAMS, end_ms)


def test_single_click_with_refs_to_down_and_up():
    (action,) = Log().click(100, 10, 10).actions()
    assert action.kind == "click" and action.button == "left" and action.clicks == 1
    assert (action.t, action.until, action.ref, action.end_ref) == (100, 150, 1, 2)
    assert action.held_ms is None


def test_small_jitter_is_still_a_click():
    (action,) = Log().down(0, 10, 10).move(20, 13, 12).up(60, 14, 13).actions()
    assert action.kind == "click"


def test_double_and_triple_click():
    (dbl,) = Log().click(0, 10, 10).click(300, 11, 11).actions()
    assert dbl.kind == "double_click" and dbl.clicks == 2 and dbl.end_ref == 4
    (tri,) = Log().click(0, 10, 10).click(300, 10, 10).click(600, 10, 10).actions()
    assert tri.kind == "multi_click" and tri.clicks == 3


def test_double_click_needs_time_and_place_and_the_same_button():
    too_slow = Log().click(0, 10, 10).click(481, 10, 10).actions()
    assert [a.kind for a in too_slow] == ["click", "click"]
    too_far = Log().click(0, 10, 10).click(100, 13, 10).actions()  # rectangle 4 px: +-2 px
    assert [a.kind for a in too_far] == ["click", "click"]
    other_button = Log().click(0, 10, 10).click(100, 10, 10, button="right").actions()
    assert [a.button for a in other_button] == ["left", "right"]
    broken = Log().click(0, 10, 10).click(100, 50, 50, button="right").click(200, 10, 10).actions()
    assert [a.kind for a in broken] == ["click", "click", "click"]


def test_long_press_is_a_held_click_and_does_not_start_a_double_click():
    actions = Log().click(0, 10, 10, hold=1400).click(1450, 10, 10).actions()
    assert [a.kind for a in actions] == ["click", "click"]
    assert actions[0].held_ms == 1400


def test_drag_with_simplified_path_and_length():
    log = Log().down(0, 0, 0)
    for i in range(1, 11):
        log.move(i * 16, i * 10, 0)
    log.up(200, 100, 0)
    (drag,) = log.actions()
    assert drag.kind == "drag" and drag.end is not None and drag.end.gx == 100
    assert drag.path == ((0, 0), (100, 0))  # a straight line keeps its ends only
    assert drag.length_px == 100
    assert (drag.ref, drag.end_ref) == (1, 12)


def test_right_click_during_a_left_drag_is_separate():
    log = Log().down(0, 0, 0).move(20, 50, 0).click(40, 50, 0, button="right").move(60, 80, 0)
    actions = log.up(80, 80, 0).actions()
    assert sorted(a.kind for a in actions) == ["click", "drag"]
    assert next(a for a in actions if a.kind == "click").button == "right"


def test_press_without_release_ends_with_the_recording():
    (held,) = Log().down(100, 5, 5).actions(end_ms=2000)
    assert held.kind == "click" and held.unfinished and held.held_ms == 1900
    (drag,) = Log().down(0, 0, 0).move(10, 40, 0).actions(end_ms=500)
    assert drag.kind == "drag" and drag.unfinished and drag.until == 500


def test_release_without_press_is_ignored():
    assert Log().up(10, 1, 1).actions() == []


def test_scroll_bursts_split_at_gaps_and_direction_changes():
    log = Log().wheel(0, -120).wheel(200, -240, until=260).wheel(1000, -120).wheel(1100, 120)
    actions = log.actions()
    assert [(a.kind, a.direction, a.notches) for a in actions] == [
        ("scroll", "down", 3.0),
        ("scroll", "down", 1.0),
        ("scroll", "up", 1.0),
    ]
    assert actions[0].until == 260


def test_fine_wheel_notches_have_one_decimal_and_horizontal_axis():
    (action,) = Log().wheel(0, 30, axis="h").wheel(50, 30, axis="h").actions()
    assert (action.kind, action.axis, action.direction, action.notches) == (
        "scroll",
        "h",
        "right",
        0.5,
    )


def test_ctrl_wheel_is_zoom():
    (zoom,) = Log().wheel(0, 120, mods=("ctrl",)).wheel(100, 120, mods=("ctrl",)).actions()
    assert (zoom.kind, zoom.direction, zoom.notches) == ("zoom", "in", 2.0)


def test_gesture_absorbs_the_wheel_events_the_system_made_of_it():
    log = Log().wheel(100, 60, mods=("ctrl",)).wheel(300, 60, mods=("ctrl",))
    log.gesture(80, 400, kind="pinch", scale=1.8, direction="out")
    log.wheel(5000, -120)  # an unrelated scroll stays
    actions = log.actions()
    assert [a.kind for a in actions] == ["gesture", "scroll"]
    pinch = actions[0]
    assert pinch.gesture == "pinch" and pinch.touchpad and pinch.scale == 1.8
    assert pinch.notches == 1.0


def test_gesture_alone_and_scroll_gesture_does_not_take_a_zoom():
    log = Log().gesture(0, 500, kind="scroll", direction="up").wheel(100, 120, mods=("ctrl",))
    assert sorted(a.kind for a in log.actions()) == ["gesture", "zoom"]


def test_injected_is_kept():
    log = Log()
    log.down(0, 1, 1, injected=True).up(10, 1, 1)
    assert log.actions()[0].injected


def test_simplify_path_keeps_corners():
    pts = [(0, 0), (5, 0), (10, 0), (10, 5), (10, 10)]
    assert simplify_path(pts) == [(0, 0), (10, 0), (10, 10)]
    assert simplify_path([(0, 0)]) == [(0, 0)]
    assert path_length(pts) == 20
