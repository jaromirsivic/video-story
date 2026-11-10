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
    frames = [two(i * 8, 1500, 1000, 400 + i * 80) for i in range(10)]
    frames.append(ContactFrame(80, (Contact(0, 1, 1), Contact(1, 2, 2), Contact(2, 3, 3))))
    frames += [two(88 + i * 8, 1500, 1000, 1200 + i * 80) for i in range(10)]
    assert run(frames) == []


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
