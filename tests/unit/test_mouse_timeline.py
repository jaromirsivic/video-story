"""Mouse lines in the timeline (M4) and the detail behind them (`mouse`)."""

from __future__ import annotations

import json
from pathlib import Path

from test_events import lines, make_recording

from video_story.core.input_events import (
    Ancestor,
    ButtonRecord,
    ContentKind,
    ElementInfo,
    GestureRecord,
    HoverRecord,
    MouseCapabilities,
    MouseLogWriter,
    MouseSystemParams,
    MoveRecord,
    WheelRecord,
    WindowInfo,
    make_content,
)
from video_story.core.manifest import MouseInfo, read_manifest, write_manifest
from video_story.core.session import SessionPaths
from video_story.core.transcript import Segment
from video_story.host import mouse_timeline
from video_story.host.events import (
    build_mouse_detail,
    build_timeline,
    mouse_record_lines,
    mouse_records,
)

PARAMS = MouseSystemParams(480, (4, 4), (4, 4))
LONG = "Dlouhý odstavec dokumentu. " * 30  # 810 characters


class Mouse:
    """Writes input/mouse.jsonl of a finished recording."""

    def __init__(self, paths: SessionPaths) -> None:
        self.paths = paths
        self.writer = MouseLogWriter(paths.mouse_log_path(paths.final_dir))
        self.writer.open()
        self.win_notepad = self.writer.window_id(0, 1, WindowInfo("Notes - Notepad", "notepad.exe"))
        self.win_edge = self.writer.window_id(0, 2, WindowInfo("Pull requests", "msedge.exe"))

    def button(self, kind, t, x, y, button="left", win=None, target="app", **kw) -> int:
        return self.writer.write(
            ButtonRecord(
                t=t,
                kind=kind,
                button=button,
                screen="screen1",
                x=x,
                y=y,
                gx=x,
                gy=y,
                win=win if win is not None else self.win_notepad,
                target=target,
                **kw,
            )
        )

    def click(self, t, x, y, element=None, **kw) -> int:
        seq = self.button("down", t, x, y, **kw)
        if element is not None:
            self.writer.write_element(t + 5, seq, element, win=kw.get("win", self.win_notepad))
        self.button("up", t + 60, x, y, **kw)
        return seq

    def close(self) -> None:
        self.writer.close()
        manifest_path = self.paths.manifest_path(self.paths.final_dir)
        manifest = read_manifest(manifest_path)
        manifest.mouse = MouseInfo(
            platform="test", capabilities=MouseCapabilities(elements=True), params=PARAMS
        )
        write_manifest(manifest_path, manifest)


def search(value: str = "video story") -> ElementInfo:
    return ElementInfo(
        role="edit",
        role_raw="Edit",
        name="Search",
        bounds=(10, 10, 60, 12),
        ancestors=(Ancestor("toolbar", "Find bar"),),
        content=make_content(ContentKind.VALUE, value),
    )


def recording(tmp_path: Path, **kw) -> tuple[SessionPaths, Mouse]:
    paths = make_recording(tmp_path, {}, **kw)
    return paths, Mouse(paths)


def test_clicks_drags_and_scrolls_become_lines_with_elements_and_content(tmp_path: Path):
    paths, m = recording(tmp_path, segments=[Segment(3400, 3900, "Tohle tlačítko nefunguje.")])
    m.click(1000, 20, 15, search())
    m.click(1500, 20, 15, search())  # the same content again (outside double-click time)
    doc = ElementInfo(
        role="document", name="Návrh.docx", content=make_content(ContentKind.TEXT, LONG)
    )
    m.click(2000, 50, 50, doc)
    # a double click selecting a word
    m.click(2600, 60, 60)
    m.click(2800, 60, 60)
    sel = ElementInfo(
        role="document", name="Návrh.docx", content=make_content(ContentKind.SELECTION, "odstavec")
    )
    m.writer.write_element(2865, m.writer._seq, sel, win=m.win_notepad)  # at the last up
    # a drag on the second window
    down = m.button("down", 4000, 10, 80, win=m.win_edge)
    for i in range(1, 5):
        m.writer.write(
            MoveRecord(
                t=4000 + i * 50, screen="screen1", x=10 + i * 20, y=80, gx=10 + i * 20, gy=80
            )
        )
    end = m.button("up", 4300, 90, 80, win=m.win_edge)
    m.writer.write_element(
        4002, down, ElementInfo(role="list_item", name="report.pdf"), win=m.win_edge
    )
    m.writer.write_element(4305, end, ElementInfo(role="tree_item", name="Archive"), win=m.win_edge)
    # a scroll over the page and a back button
    seq = m.writer.next_seq()
    m.writer.write(
        WheelRecord(
            seq=seq,
            t=4500,
            axis="v",
            delta=-360,
            count=3,
            until=4600,
            screen="screen1",
            x=30,
            y=30,
            gx=30,
            gy=30,
            win=m.win_edge,
        )
    )
    m.writer.write_element(
        4510, seq, ElementInfo(role="document", name="Pull requests"), win=m.win_edge
    )
    m.click(5000, 30, 30, button="x1", win=m.win_edge)
    # a click into Video Story's own window and onto the drawing layer: left out
    m.click(5200, 5, 5, target="video_story")
    m.click(5300, 5, 5, target="drawing")
    # the pointer rested on a button just before the sentence
    hover = m.writer.write(HoverRecord(t=3000, screen="screen1", x=70, y=70, gx=70, gy=70))
    m.writer.write_element(3010, hover, ElementInfo(role="button", name="Save"), win=m.win_notepad)
    m.close()

    result = build_timeline(paths)
    text = [
        line
        for line in lines(result)
        if " SAY " in line or any(w in line for w in ("CLICK", "DRAG", "SCROLL"))
    ]
    assert text[0] == (
        '1000 CLICK left screen1 20,15 on edit "Search" in toolbar "Find bar" '
        'in "Notes - Notepad" [notepad.exe] value "video story" (frames before 500, after 1500)'
    )
    assert text[1] == (
        '1500 CLICK left screen1 20,15 on edit "Search" in toolbar "Find bar" value same as 1000 '
        "(frames before 1000, after 2000)"
    )
    assert text[2].startswith('2000 CLICK left screen1 50,50 on document "Návrh.docx" text "Dlouhý')
    assert "(810 chars, `mouse --at 2000`)" in text[2]
    assert text[3].startswith('2600-2860 DOUBLE-CLICK left screen1 60,60 selected "odstavec"')
    assert text[4] == (
        "3400-3900 SAY Tohle tlačítko nefunguje. "
        '[cursor screen1 1,2 over button "Save" (notepad.exe)]'
    )
    assert text[5].startswith(
        "4000-4300 DRAG left screen1 10,80 -> screen1 90,80 (300 ms, 80 px) "
        'on list_item "report.pdf" in "Pull requests" [msedge.exe] to tree_item "Archive"'
    )
    assert text[6] == '4500-4600 SCROLL down 3 notches screen1 30,30 over document "Pull requests"'
    assert text[7].startswith("5000 CLICK x1 (back) screen1 30,30 (frames")
    assert len(text) == 8  # own window and drawing layer left out
    assert result["counts"]["clicks"] == 5 and result["counts"]["drags"] == 1
    assert result["counts"]["scrolls"] == 1
    assert result["mouse"] == {"recorded": True, "mode": "full"}
    assert "CLICK / DOUBLE-CLICK" in result["legend"]

    detail = build_mouse_detail(paths, at_ms=2000)
    (action,) = detail["actions"]
    assert (
        action["element"]["content"]["text"] == LONG
        and action["element"]["content"]["chars"] == 810
    )
    bounds = build_mouse_detail(paths, at_ms=1000)["actions"][0]["element"]["bounds"]
    assert bounds == {"screen": "screen1", "x": 10, "y": 10, "width": 60, "height": 12}
    drag = build_mouse_detail(paths, at_ms=4000)["actions"][0]
    assert drag["kind"] == "drag" and drag["end_element"]["name"] == "Archive"
    assert drag["path"][0] == {"screen": "screen1", "x": 10, "y": 80}
    everything = build_mouse_detail(paths)
    assert len(everything["actions"]) == 7 and len(everything["rest_points"]) == 1
    assert everything["rest_points"][0]["element"]["name"] == "Save"
    assert build_mouse_detail(paths, at_ms=2150)["actions"][0]["t"] == 2000  # nearest within 1 s
    assert build_mouse_detail(paths, at_ms=50_000)["actions"] == []


def test_mouse_off_and_summary(tmp_path: Path, monkeypatch):
    paths, m = recording(tmp_path)
    for i in range(6):
        m.writer.write(
            WheelRecord(
                t=1000 + i * 500, axis="v", delta=-120, gx=1, gy=1, screen="screen1", x=1, y=1
            )
        )
    m.click(4500, 20, 20)
    m.writer.write(
        GestureRecord(
            t=5000,
            until=5400,
            kind="pinch",
            scale=1.8,
            direction="out",
            screen="screen1",
            x=5,
            y=5,
            gx=5,
            gy=5,
        )
    )
    m.close()

    off = build_timeline(paths, mouse="off")
    assert not any("SCROLL" in line or "CLICK" in line for line in lines(off))
    assert off["counts"]["scrolls"] == 6 and off["mouse"]["mode"] == "off"

    summary = lines(build_timeline(paths, mouse="summary"))
    merged = [line for line in summary if "SCROLLS" in line]
    assert merged == ["1000-3500 SCROLLS 6x (down 6) screen1 1,1"]
    assert any(
        "GESTURE pinch out 2 fingers x1.8 screen1 5,5 (touchpad)" in line for line in summary
    )

    monkeypatch.setattr(mouse_timeline, "SUMMARY_AFTER_LINES", 3)
    auto = build_timeline(paths)
    assert auto["mouse"]["mode"] == "summary" and "8 mouse actions" in auto["mouse_note"]
    full = build_timeline(paths, mouse="full")
    assert sum("SCROLL down" in line for line in lines(full)) == 6


def test_a_recording_without_mouse_says_so(tmp_path: Path):
    paths = make_recording(tmp_path, {})
    result = build_timeline(paths)
    assert result["mouse"] == {"recorded": False, "mode": "off"}
    assert "CLICK" not in result["legend"]
    assert build_mouse_detail(paths)["recorded"] is False


def test_unknown_mouse_mode_is_a_usage_error(tmp_path: Path):
    import pytest

    from video_story.host.runner import SessionError

    paths = make_recording(tmp_path, {})
    with pytest.raises(SessionError) as err:
        build_timeline(paths, mouse="loud")
    assert err.value.code == "USAGE"


def test_held_injected_and_modifiers_are_noted(tmp_path: Path):
    paths, m = recording(tmp_path)
    m.button("down", 1000, 20, 20, mods=("ctrl",), injected=True)
    m.button("up", 2500, 20, 20)
    m.close()
    (line,) = [x for x in lines(build_timeline(paths)) if "CLICK" in x]
    assert "(held 1.5 s, Ctrl, injected)" in line
    raw = json.loads(
        paths.mouse_log_path(paths.final_dir).read_text(encoding="utf-8").splitlines()[-1]
    )
    assert raw["type"] == "up"


def test_a_long_drag_path_is_thinned_only_where_no_file_takes_it(tmp_path: Path):
    paths, m = recording(tmp_path)
    m.button("down", 1000, 0, 0)
    for i in range(1, 60):  # a zigzag: every point survives the simplification
        x, y = i, (i % 2) * 40
        m.writer.write(MoveRecord(t=1000 + i * 20, screen="screen1", x=x, y=y, gx=x, gy=y))
    m.button("up", 2300, 60, 0)
    m.close()

    (full,) = build_mouse_detail(paths, at_ms=1000)["actions"]
    assert len(full["path"]) > 50 and "path_points" not in full
    (thin,) = build_mouse_detail(paths, at_ms=1000, max_path_points=10)["actions"]
    assert len(thin["path"]) == 10 and thin["path_points"] == len(full["path"])
    assert thin["path"][0] == full["path"][0] and thin["path"][-1] == full["path"][-1]
    assert thin["length_px"] == full["length_px"]  # measured on the whole path
    assert mouse_timeline._thin_path([1, 2, 3], 5) == [1, 2, 3]
    assert mouse_timeline._thin_path(list(range(11)), 3) == [0, 5, 10]


def test_mouse_records_merge_actions_and_rest_points_by_time():
    detail = {
        "actions": [{"kind": "click", "t": 10}, {"kind": "scroll", "t": 30}],
        "rest_points": [{"t": 5, "at": {}}, {"t": 30, "at": {}}],
    }
    records = mouse_records(detail)
    assert [(r["kind"], r["t"]) for r in records] == [
        ("rest", 5),
        ("click", 10),
        ("scroll", 30),  # an action before a rest point of the same time
        ("rest", 30),
    ]
    assert mouse_record_lines(records)[0] == '{"kind": "rest", "t": 5, "at": {}}'
    assert mouse_records({"actions": [], "rest_points": []}) == []
