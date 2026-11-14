"""Two narration tracks: the data model, the rules of editing subtitles with the mouse
(zones, minimum length, 10 ms steps, magnet, overlaps), the cues a viewer reads and
what the agent gets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_story.core.transcript import (
    LOWER_TRACK,
    UPPER_TRACK,
    Segment,
    Transcript,
    display_cues,
    read_transcript,
    write_transcript,
)
from video_story.editor.timeline.narration_edit import (
    Drag,
    Hit,
    Magnets,
    Zone,
    apply,
    hit_test,
    own_zone,
    validate,
)
from video_story.export.srt import srt_text

PX = 0.1  # px per ms: 100 px per second

# Data model ---------------------------------------------------------------------------


def test_track_is_stored_only_for_the_lower_track(tmp_path: Path):
    txt, js = tmp_path / "t.txt", tmp_path / "t.json"
    upper, lower = Segment(0, 1000, "nahoře"), Segment(500, 1500, "dole", LOWER_TRACK)
    write_transcript(txt, js, Transcript("cs", [upper, lower]))
    data = json.loads(js.read_text(encoding="utf-8"))
    assert "track" not in data["segments"][0] and data["segments"][1]["track"] == 1
    assert read_transcript(js).segments == [upper, lower]
    assert upper.track == UPPER_TRACK and lower.lower
    assert "[00:00:00.500 --> 00:00:01.500] [lower track] dole" in txt.read_text("utf-8")


def test_an_old_transcript_reads_and_writes_back_unchanged(tmp_path: Path):
    txt, js = tmp_path / "t.txt", tmp_path / "t.json"
    write_transcript(txt, js, Transcript("en", [Segment(0, 1000, "a"), Segment(1000, 2000, "b")]))
    before_json, before_txt = js.read_bytes(), txt.read_bytes()
    write_transcript(txt, js, read_transcript(js))
    assert js.read_bytes() == before_json and txt.read_bytes() == before_txt
    assert "[lower track]" not in txt.read_text("utf-8")


# Cues for the preview, the SRT and the render -----------------------------------------


def test_display_cues_without_the_lower_track_are_the_segments():
    segments = [Segment(1000, 2000, "b"), Segment(0, 1000, "a")]
    assert display_cues(segments) == [Segment(0, 1000, "a"), Segment(1000, 2000, "b")]


def test_display_cues_join_the_tracks_where_they_overlap():
    upper = Segment(0, 2000, "Horní")
    lower = Segment(1000, 3000, "Spodní", LOWER_TRACK)
    assert display_cues([upper, lower]) == [
        Segment(0, 1000, "Horní"),
        Segment(1000, 2000, "Horní Spodní"),
        Segment(2000, 3000, "Spodní"),
    ]


def test_display_cues_lower_inside_upper_only_lower_and_touching():
    assert display_cues([Segment(0, 3000, "A"), Segment(1000, 2000, "B", LOWER_TRACK)]) == [
        Segment(0, 1000, "A"),
        Segment(1000, 2000, "A B"),
        Segment(2000, 3000, "A"),
    ]
    assert display_cues([Segment(500, 900, "only lower", LOWER_TRACK)]) == [
        Segment(500, 900, "only lower")
    ]
    # Touching subtitles of the two tracks are not joined.
    assert display_cues([Segment(0, 1000, "A"), Segment(1000, 2000, "B", LOWER_TRACK)]) == [
        Segment(0, 1000, "A"),
        Segment(1000, 2000, "B"),
    ]


def test_srt_shows_the_joined_text():
    transcript = Transcript("cs", [Segment(0, 2000, "Horní"), Segment(1000, 3000, "Dole", 1)])
    text = srt_text(transcript, 0, 3000)
    assert "00:00:01,000 --> 00:00:02,000\nHorní Dole\n" in text
    assert text.count("-->") == 3


# Zones --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width", "offset", "zone"),
    [
        (16, 8, Zone.NONE),  # 16 px or less: nothing
        (15, 8, Zone.NONE),
        (17, 1, Zone.MOVE),  # wider than 16 and narrower than 48: green as a whole
        (47, 46, Zone.MOVE),
        (48, 0, Zone.RESIZE_START),  # from 48 px: yellow, green, yellow
        (48, 15.9, Zone.RESIZE_START),
        (48, 16, Zone.MOVE),
        (48, 32, Zone.MOVE),
        (48, 32.1, Zone.RESIZE_END),
        (48, 48, Zone.RESIZE_END),
        (100, 101, Zone.NONE),
    ],
)
def test_own_zone_by_width(width, offset, zone):
    assert own_zone(width, offset) is zone


def test_hit_test_zones_around_a_shared_edge():
    a, b = Segment(0, 1000, "a"), Segment(1000, 2000, "b")  # 100 px each at PX
    segments = [a, b]
    # The last 6 px of a and the first 6 px of b are orange (roll a and b).
    for time_ms in (941, 999, 1000, 1059):
        hit = hit_test(segments, 0, time_ms, PX)
        assert hit.zone is Zone.ROLL, time_ms
        assert hit.roll_pair == (a, b)
    # The rest of the yellow zones moves one edge only.
    assert hit_test(segments, 0, 900, PX) == Hit(Zone.RESIZE_END, a)
    assert hit_test(segments, 0, 1100, PX) == Hit(Zone.RESIZE_START, b)
    assert hit_test(segments, 0, 500, PX) == Hit(Zone.MOVE, a)
    assert hit_test(segments, 1, 500, PX).zone is Zone.NONE  # the lower track is empty
    assert hit_test(segments, 0, 2500, PX).zone is Zone.NONE


def test_a_narrow_subtitle_is_rolled_from_its_wide_neighbour():
    small = Segment(0, 150, "small")  # 15 px: no zone of its own
    big = Segment(150, 1150, "big")  # 100 px
    segments = [small, big]
    assert hit_test(segments, 0, 75, PX).zone is Zone.NONE
    hit = hit_test(segments, 0, 180, PX)  # 3 px into big: its orange part
    assert hit.zone is Zone.ROLL and hit.roll_pair == (small, big)
    assert hit_test(segments, 0, 250, PX).zone is Zone.RESIZE_START  # 10 px: yellow only
    # An 18 px neighbour (green only) next to a 50 px one: orange only in the wide one.
    green = Segment(0, 180, "green")
    wide = Segment(180, 680, "wide")
    hit = hit_test([green, wide], 0, 170, PX)
    assert hit.zone is Zone.MOVE and hit.segment == green
    assert hit_test([green, wide], 0, 200, PX).zone is Zone.ROLL


# Dragging ---------------------------------------------------------------------------


def _drag(segments, track, time_ms, duration=10_000, magnets=None) -> Drag:
    return Drag(hit_test(segments, track, time_ms, PX), segments, duration, magnets)


def test_move_keeps_the_length_in_10_ms_steps_and_inside_the_recording():
    a = Segment(1000, 2000, "a")
    drag = _drag([a], 0, 1500)
    proposal = drag.propose(1234, 0)
    assert proposal.valid and proposal.new == (Segment(2230, 3230, "a"),)
    assert drag.propose(-5000, 0).new == (Segment(0, 1000, "a"),)
    assert drag.propose(50_000, 0).new == (Segment(9000, 10_000, "a"),)


def test_move_to_the_lower_track_may_overlap_the_upper_one():
    a, b = Segment(0, 1000, "a"), Segment(1000, 2000, "b")
    drag = _drag([a, b], 0, 500)
    assert not drag.propose(500, 0).valid  # over b in the same track: red
    proposal = drag.propose(500, 1)
    assert proposal.valid and proposal.new == (Segment(500, 1500, "a", LOWER_TRACK),)


def test_resize_keeps_half_a_second_and_cannot_overlap():
    a, b = Segment(0, 1000, "a"), Segment(2000, 3000, "b")
    end = _drag([a, b], 0, 950)  # yellow at the end of a
    assert end.zone is Zone.RESIZE_END
    assert end.propose(-900, 0).new == (Segment(0, 500, "a"),)  # stops at 0.5 s
    assert end.propose(504, 0).new == (Segment(0, 1500, "a"),)  # 10 ms steps
    assert not end.propose(1500, 0).valid  # into b
    assert end.propose(1000, 0).valid  # touching b is fine
    with pytest.raises(ValueError):
        _drag([a, b], 1, 500)  # nothing to drag on the empty lower track


def test_resize_start_and_a_subtitle_shorter_than_the_minimum():
    short = Segment(1000, 1300, "short")  # 30 px, green only: can be moved only
    assert _drag([short], 0, 1150).zone is Zone.MOVE
    long = Segment(1000, 3000, "long")
    start = _drag([long], 0, 1050)
    assert start.zone is Zone.RESIZE_START
    assert start.propose(-2000, 0).new == (Segment(0, 3000, "long"),)
    assert start.propose(5000, 0).new == (Segment(2500, 3000, "long"),)


def test_roll_moves_the_shared_edge_and_keeps_both_long_enough():
    a, b = Segment(0, 1000, "a"), Segment(1000, 2000, "b")
    drag = _drag([a, b], 0, 999)
    assert drag.zone is Zone.ROLL
    proposal = drag.propose(300, 0)
    assert proposal.valid and proposal.new == (Segment(0, 1300, "a"), Segment(1300, 2000, "b"))
    assert drag.propose(5000, 0).new == (Segment(0, 1500, "a"), Segment(1500, 2000, "b"))
    assert drag.propose(-5000, 0).new == (Segment(0, 500, "a"), Segment(500, 2000, "b"))


def test_magnet_pulls_edges_onto_neighbours_keypoints_pauses_and_the_playhead():
    a, b = Segment(0, 1000, "a"), Segment(2000, 3000, "b")
    end = _drag([a, b], 0, 950, magnets=Magnets.of(keypoints=[1234], pauses=[1500], playhead=1777))
    snap = 80  # ms (8 px at 0.1 px/ms)
    assert end.propose(950, 0, snap).new[0].end_ms == 2000  # onto b's start
    assert end.propose(220, 0, snap).new[0].end_ms == 1230  # keypoint, in 10 ms steps
    assert end.propose(480, 0, snap).new[0].end_ms == 1500  # pause
    assert end.propose(760, 0, snap).new[0].end_ms == 1780  # playhead
    assert end.propose(350, 0, snap).new[0].end_ms == 1350  # nothing near
    move = _drag([a, b], 0, 500)
    moved = move.propose(960, 0, snap).new[0]
    assert moved == Segment(1000, 2000, "a")  # its end snaps onto b's start


def test_apply_and_validate():
    a, b = Segment(0, 1000, "a"), Segment(1000, 2000, "b")
    segments = apply([a, b], (a,), (Segment(0, 900, "a"),))
    assert segments == [Segment(0, 900, "a"), b] and validate(segments) is None
    assert validate([a, Segment(500, 1500, "x")]) is not None
    assert validate([a, Segment(500, 1500, "x", LOWER_TRACK)]) is None
    with pytest.raises(ValueError):
        apply([a], (b,), (a,))


# What the agent gets ------------------------------------------------------------------


def test_timeline_marks_the_lower_track_and_overlaps(tmp_path: Path):
    from test_events import lines, make_recording

    from video_story.host.events import LOWER_TRACK_LEGEND, build_timeline

    paths = make_recording(
        tmp_path,
        {},
        segments=[
            Segment(0, 2000, "Horní text."),
            Segment(1000, 3000, "Spodní text.", LOWER_TRACK),
            Segment(3000, 4000, "Jen dotyk."),
        ],
    )
    result = build_timeline(paths)
    say = [line for line in lines(result) if " SAY " in line]
    assert say[0].startswith(
        "0-2000 SAY Horní text. (at the same time as lower track 1000-3000) [cursor"
    )
    assert say[1].startswith(
        "1000-3000 SAY [lower track] Spodní text. (at the same time as 0-2000) [cursor"
    )
    assert say[2].startswith("3000-4000 SAY Jen dotyk. [cursor")  # touching: unrelated
    assert result["counts"]["speech"] == 3 and result["counts"]["speech_lower"] == 1
    assert LOWER_TRACK_LEGEND in result["legend"]


def test_timeline_without_the_lower_track_says_nothing_about_tracks(tmp_path: Path):
    from test_events import lines, make_recording

    from video_story.host.events import LOWER_TRACK_LEGEND, build_timeline

    paths = make_recording(tmp_path, {}, segments=[Segment(0, 2000, "Jen nahoře.")])
    result = build_timeline(paths)
    assert any(line.startswith("0-2000 SAY Jen nahoře. [cursor") for line in lines(result))
    assert "speech_lower" not in result["counts"]
    assert LOWER_TRACK_LEGEND not in result["legend"]
    assert "lower track" not in result["timeline"]
