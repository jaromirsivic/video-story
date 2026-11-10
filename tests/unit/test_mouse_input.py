"""input/mouse.jsonl: records, writer, reader, content limits, coalescing (no OS)."""

from __future__ import annotations

import json
from pathlib import Path

from video_story.core.input_events import (
    MAX_ELEMENT_TEXT_CHARS,
    PASSWORD_NAME,
    Ancestor,
    ButtonRecord,
    ContentKind,
    ElementInfo,
    GestureRecord,
    MouseCapabilities,
    MouseLogWriter,
    MouseSystemParams,
    MoveRecord,
    MoveThrottle,
    StatusRecord,
    WheelCoalescer,
    WheelRecord,
    WindowInfo,
    content_hash,
    make_content,
    normalize_modifiers,
    normalize_role,
    read_mouse_log,
    record_from_dict,
)
from video_story.core.textcut import truncate_text
from video_story.ui.clipboard import MAX_CLIPBOARD_CHARS, truncate_clipboard


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# Content --------------------------------------------------------------------------


def test_content_of_4096_chars_is_kept_and_longer_is_cut_with_dots():
    exact = make_content(ContentKind.VALUE, "a" * MAX_ELEMENT_TEXT_CHARS)
    assert exact is not None and exact.text == "a" * 4096 and not exact.truncated
    longer = make_content(ContentKind.TEXT, "b" * (MAX_ELEMENT_TEXT_CHARS + 1))
    assert longer is not None
    assert longer.text == "b" * 4096 + "..."
    assert longer.truncated and longer.chars == 4097
    assert make_content(ContentKind.VALUE, "") is None
    assert make_content(ContentKind.VALUE, None) is None


def test_clipboard_uses_the_same_cut():
    assert truncate_clipboard("x" * (MAX_CLIPBOARD_CHARS + 5)) == "x" * MAX_CLIPBOARD_CHARS + "..."
    assert truncate_text("short", 10) == "short"


def test_password_field_keeps_neither_name_nor_content():
    info = ElementInfo(
        role="edit",
        name="Heslo",
        is_password=True,
        content=make_content(ContentKind.VALUE, "secret"),
    ).sanitized()
    assert info.name == PASSWORD_NAME
    assert info.content is None


def test_sanitized_limits_names_ancestors_and_titles():
    info = ElementInfo(
        role="button",
        name="n" * 500,
        window_title="w" * 500,
        ancestors=tuple(Ancestor("pane", f"a{i}") for i in range(5)) + (Ancestor("pane", ""),),
    ).sanitized()
    assert info.name == "n" * 200 + "..."
    assert info.window_title == "w" * 120 + "..."
    assert [a.name for a in info.ancestors] == ["a0", "a1", "a2"]


def test_roles_are_normalised_for_every_platform():
    assert normalize_role("Button") == "button"
    assert normalize_role("UIA_HyperlinkControlTypeId") == "link"
    assert normalize_role("AXTextField") == "edit"
    assert normalize_role("AXWebArea") == "document"
    assert normalize_role("push button") == "button"
    assert normalize_role("ROLE_PAGE_TAB") == "tab"
    assert normalize_role("SomethingNew") == "other"
    assert normalize_role(None) == "other"


def test_modifiers_are_known_and_ordered():
    assert normalize_modifiers(["Shift", "ctrl", "hyper"]) == ("ctrl", "shift")
    assert normalize_modifiers(None) == ()


# Writer and reader ------------------------------------------------------------------


def test_writer_stores_each_content_and_window_once(tmp_path: Path):
    path = tmp_path / "input" / "mouse.jsonl"
    writer = MouseLogWriter(path)
    writer.open()
    win = writer.window_id(10, 0x1234, WindowInfo("Settings", "notepad.exe", 42))
    assert writer.window_id(11, 0x1234, WindowInfo("Settings", "notepad.exe", 42)) == win
    renamed = writer.window_id(12, 0x1234, WindowInfo("Settings *", "notepad.exe", 42))
    assert renamed != win
    down = writer.write(
        ButtonRecord(t=100, kind="down", screen="screen1", x=5, y=6, gx=5, gy=6, win=win)
    )
    element = ElementInfo(
        role="edit",
        role_raw="Edit",
        name="Search",
        bounds=(0, 0, 100, 20),
        content=make_content(ContentKind.VALUE, "hello"),
    )
    writer.write_element(101, down, element, win=win, ms=7)
    writer.write_element(300, down + 5, element, win=win)  # same text again
    writer.write_element_error(400, down + 6, "timeout", ms=400)
    writer.write(StatusRecord(t=500, state="restarted", reason="no events"))
    writer.close()

    lines = _lines(path)
    assert [line["seq"] for line in lines] == sorted(line["seq"] for line in lines)
    assert [line["type"] for line in lines].count("content") == 1
    assert [line["type"] for line in lines].count("window") == 2
    content = next(line for line in lines if line["type"] == "content")
    assert content == {
        "seq": content["seq"],
        "t": 101,
        "type": "content",
        "hash": content_hash("hello"),
        "text": "hello",
    }
    first = next(line for line in lines if line["type"] == "element")
    assert first["content"] == {"kind": "value", "hash": content_hash("hello"), "chars": 5}
    assert "password" not in first  # False flags are left out
    assert writer.counts == {"events": 1, "elements": 2, "contents": 1, "windows": 2}

    log = read_mouse_log(path)
    assert log.buttons[0].kind == "down" and log.buttons[0].win == win
    assert log.windows[win].title == "Settings"
    el = log.element_for(down)
    assert el is not None and el.bounds == (0, 0, 100, 20) and el.ms == 7
    assert log.content_of(el) == "hello"
    assert log.elements[down + 6][0].error == "timeout"
    assert log.statuses[0].state == "restarted"


def test_writer_never_stores_password_content(tmp_path: Path):
    path = tmp_path / "mouse.jsonl"
    writer = MouseLogWriter(path)
    writer.open()
    writer.write_element(
        1,
        1,
        ElementInfo(
            role="edit",
            name="pw",
            is_password=True,
            content=make_content(ContentKind.VALUE, "hunter2"),
        ),
    )
    writer.close()
    text = path.read_text(encoding="utf-8")
    assert "hunter2" not in text and "pw" not in text
    assert '"password":true' in text


def test_reader_skips_a_cut_last_line_and_unknown_types(tmp_path: Path):
    path = tmp_path / "mouse.jsonl"
    path.write_text(
        '{"seq":1,"t":10,"type":"down","button":"left","gx":1,"gy":2,"target":"app"}\n'
        '{"seq":2,"t":11,"type":"future_thing","x":1}\n'
        '{"seq":3,"t":12,"type":"up","button":"left","gx":1,"gy":2,"target":"app"}\n'
        '{"seq":4,"t":13,"type":"wheel","axis":"v","del',
        encoding="utf-8",
    )
    log = read_mouse_log(path)
    assert [b.kind for b in log.buttons] == ["down", "up"]
    assert log.bad_lines == 1
    assert read_mouse_log(tmp_path / "missing.jsonl").buttons == []


def test_records_roundtrip_through_json():
    records = [
        ButtonRecord(
            seq=1,
            t=2,
            kind="up",
            button="x1",
            screen="screen2",
            x=3,
            y=4,
            gx=5,
            gy=6,
            mods=("ctrl",),
            target="drawing",
            injected=True,
        ),
        WheelRecord(seq=2, t=3, axis="h", delta=-30, count=3, until=40, gx=1, gy=1),
        MoveRecord(seq=3, t=4, gx=7, gy=8),
        GestureRecord(seq=4, t=5, kind="pinch", until=900, scale=1.8, direction="out"),
    ]
    for record in records:
        data = json.loads(json.dumps(record.to_dict()))
        assert record_from_dict(data) == record
    assert records[0].to_dict()["type"] == "up"


def test_capabilities_and_params_roundtrip():
    caps = MouseCapabilities(buttons=(), horizontal_wheel=True, touchpad_gestures=True)
    assert MouseCapabilities.from_dict(caps.to_dict()) == caps
    assert MouseCapabilities.from_dict({"buttons": ["left", "b42"]}).buttons == ("left",)
    params = MouseSystemParams(480, (4, 4), (6, 6))
    assert MouseSystemParams.from_dict(params.to_dict()) == params
    assert (
        MouseSystemParams.from_dict({"double_click_ms": "x", "drag_px": [1]}) == MouseSystemParams()
    )


# Thinning --------------------------------------------------------------------------


def _wheel(t: int, delta: int = -40, axis: str = "v", mods: tuple[str, ...] = ()) -> WheelRecord:
    return WheelRecord(t=t, axis=axis, delta=delta, mods=mods, gx=1, gy=1)  # type: ignore[arg-type]


def test_wheel_events_within_40_ms_become_one_line():
    co = WheelCoalescer()
    assert co.add(_wheel(0)) == []
    assert co.add(_wheel(30)) == []
    assert co.add(_wheel(65)) == []
    done = co.add(_wheel(200))  # gap > 40 ms
    assert len(done) == 1 and done[0].delta == -120 and done[0].count == 3 and done[0].until == 65
    assert co.add(_wheel(210, axis="h")) == [_wheel(200)]  # another axis closes the line
    assert co.add(_wheel(220, delta=40, axis="h")) != []  # direction change too
    assert co.due(240) == []
    assert len(co.due(300)) == 1
    assert co.flush() == []


def test_ctrl_wheel_is_not_merged_with_plain_wheel():
    co = WheelCoalescer()
    co.add(_wheel(0))
    assert len(co.add(_wheel(10, mods=("ctrl",)))) == 1


def test_move_throttle_keeps_16_ms_and_2_px_then_thins_long_drags():
    th = MoveThrottle(points_per_step=3)
    assert th.accept(0, 0, 0)
    assert not th.accept(10, 50, 0)  # too soon
    assert not th.accept(20, 1, 1)  # too close
    assert th.accept(20, 5, 0)
    assert th.accept(40, 10, 0)
    # three points stored: the interval doubles to 32 ms
    assert not th.accept(60, 20, 0)
    assert th.accept(72, 20, 0)
    th.reset()
    assert th.points == 0 and th.accept(73, 20, 0)
