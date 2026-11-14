"""Two-finger touchpad gestures from synthetic contact frames (rule from M0)."""

from __future__ import annotations

import math

from video_story.core.gestures import (
    Contact,
    ContactFrame,
    GestureTracker,
    TouchpadGeometry,
)

PAD = TouchpadGeometry(3000, 2000)


def two(t: int, cx: float, cy: float, dist: float) -> ContactFrame:
    half = dist / 2
    return ContactFrame(
        t, (Contact(0, round(cx - half), round(cy)), Contact(1, round(cx + half), round(cy)))
    )


def run(frames: list[ContactFrame], lift: bool = True):
    tracker = GestureTracker(PAD)
    out = []
    for f in frames:
        out.extend(tracker.add(f))
    if lift:
        out.extend(tracker.add(ContactFrame(frames[-1].t_ms + 8, ())))
    return out


def test_pinch_out_and_in():
    out = run([two(i * 8, 1500, 1000, 400 + i * 80) for i in range(25)])
    assert [(g.kind, g.direction) for g in out] == [("pinch", "out")]
    assert math.isclose(out[0].scale, 2320 / 400, rel_tol=0.01)
    pinch_in = run([two(i * 8, 1500, 1000, 2200 - i * 70) for i in range(25)])
    assert [(g.kind, g.direction) for g in pinch_in] == [("pinch", "in")]


def test_pinch_out_and_back_reports_the_larger_change():
    dists = [600 + i * 100 for i in range(10)] + [1500 - i * 100 for i in range(10)]
    out = run([two(i * 8, 1500, 1000, d) for i, d in enumerate(dists)])
    assert len(out) == 1 and out[0].direction == "out" and out[0].scale == 2.5


def test_scroll_while_fingers_spread_a_little_is_still_a_scroll():
    # the M0 case: distance ratio 1.42, but the centre travelled far more
    frames = [two(i * 8, 1500, 1500 - i * 50, 580 + i * 10) for i in range(25)]
    out = run(frames)
    assert [(g.kind, g.direction) for g in out] == [("scroll", "up")]
    assert out[0].distance == 0.6


def test_scroll_down_and_back_up_gives_two_gestures():
    ys = [500 + i * 60 for i in range(15)] + [1340 - i * 60 for i in range(15)]
    out = run([two(i * 8, 1500, y, 700) for i, y in enumerate(ys)])
    assert [(g.kind, g.direction) for g in out] == [("scroll", "down"), ("scroll", "up")]


def test_horizontal_scroll():
    out = run([two(i * 8, 500 + i * 60, 1000, 700) for i in range(20)])
    assert [(g.kind, g.direction) for g in out] == [("scroll", "right")]


def test_resting_fingers_and_short_touches_are_dropped():
    assert run([two(i * 8, 1500, 1000, 700 + (i % 2) * 5) for i in range(30)]) == []
    assert run([two(i * 8, 1500, 1000 - i * 200, 700) for i in range(3)]) == []


def test_one_finger_and_three_fingers_are_ignored():
    one = [ContactFrame(i * 8, (Contact(0, 100 + i * 50, 100),)) for i in range(20)]
    assert run(one) == []
    three = (Contact(0, 1, 1), Contact(1, 2, 2), Contact(2, 3, 3))
    # a thumb brushing the pad for one frame during a pinch: still a pinch
    frames = [two(i * 8, 1500, 1000, 400 + i * 80) for i in range(10)]
    frames.append(ContactFrame(80, three))
    frames += [two(88 + i * 8, 1500, 1000, 1200 + i * 80) for i in range(10)]
    assert [g.kind for g in run(frames)] == ["pinch"]
    # a three-finger system gesture that starts with two fingers: ignored
    swipe = [two(i * 8, 1500, 1000, 400 + i * 80) for i in range(6)]
    swipe += [ContactFrame(48 + i * 8, three) for i in range(20)]
    assert run(swipe) == []


def test_the_rule_on_touches_measured_on_a_real_touchpad():
    from video_story.core.gestures import decide

    # (spread, travel) as shares of the touchpad: the pinches as logged in the manual check
    # after M5, the scrolls converted from the M0 measurements (touchpad units / 3679 x 2261)
    pinches = [
        (0.117, 0.12),
        (0.266, 0.086),
        (0.189, 0.099),
        (0.377, 0.629),
        (0.286, 0.121),
        (0.429, 0.133),
        (0.396, 0.146),
        (0.472, 0.128),
        (0.28, 0.12),
        (0.37, 0.3),
    ]
    scrolls = [(0.08, 0.6), (0.07, 0.53), (0.03, 0.51), (0.05, 0.77), (0.04, 0.76), (0.025, 0.65)]
    assert [decide(s, t) for s, t in pinches] == ["pinch"] * len(pinches)
    assert [decide(s, t) for s, t in scrolls] == ["scroll"] * len(scrolls)
    assert decide(0.001, 0.001) is None  # resting fingers
    assert decide(0.1, 0.25) is None  # neither: dropped rather than guessed


def test_a_pinch_while_the_fingers_drift_is_still_a_pinch():
    # distance 400 -> 1500 while the centre wanders 1.6x as far as the spread
    frames = [two(i * 8, 1000 + i * 40, 1000, 400 + i * 55) for i in range(21)]
    assert [(g.kind, g.direction) for g in run(frames)] == [("pinch", "out")]


def test_the_last_touch_is_described_for_the_log():
    tracker = GestureTracker(PAD)
    for i in range(20):
        tracker.add(two(i * 8, 1500, 1000, 700 + (i % 2) * 5))
    tracker.add(ContactFrame(200, ()))
    last = tracker.last_touch
    assert last is not None and last["two_finger_frames"] == 20
    assert last["result"] == "dropped: no gesture" and last["extra_finger_frames"] == 0


def test_a_lift_without_an_empty_frame_is_found_by_time():
    tracker = GestureTracker(PAD)
    for i in range(20):
        assert tracker.add(two(i * 8, 1500, 1000, 400 + i * 80)) == []
    assert tracker.due(160) == []
    assert [g.kind for g in tracker.due(400)] == ["pinch"]
    # and a new touch after a pause finishes the previous one too
    tracker = GestureTracker(PAD)
    for i in range(20):
        tracker.add(two(i * 8, 1500, 1000, 400 + i * 80))
    assert [g.kind for g in tracker.add(two(1000, 1500, 1000, 400))] == ["pinch"]
