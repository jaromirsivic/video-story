"""Unit tests for the pure core layer (no OS, no Qt, no devices)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_story.core import config as cfg
from video_story.core import fileio
from video_story.core.cursor import locate_cursor
from video_story.core.manifest import Manifest, read_manifest, write_manifest
from video_story.core.naming import (
    frame_filename,
    parse_frame_filename,
    screen_id,
    sidecar_filename,
)
from video_story.core.screens import RawScreen, assign_screen_ids
from video_story.core.session import (
    SessionPaths,
    SessionState,
    Status,
    is_session_id,
    new_session_id,
    read_state,
    write_state,
)
from video_story.core.timeline import RecordingClock, format_timestamp, parse_timestamp
from video_story.core.transcript import Segment, Transcript, normalize_segments, transcript_to_text

# Config ------------------------------------------------------------------------


def test_config_defaults():
    c = cfg.Config()
    assert c.capture_interval_ms == 500
    assert c.image_format == "jpeg"
    assert c.jpeg_quality == 70
    assert c.auto_stop_minutes == 120
    assert c.archive_after_days == 30
    assert c.drawing_grid_px == 16
    assert c.audio_input is None
    assert c.recording_folder.name == "VideoStory"


@pytest.mark.parametrize("value", [1, 250, 1000, "1000", 1.0])
def test_capture_interval_bounds_accepted(value):
    assert cfg.config_from_dict({"capture_interval_ms": value}).capture_interval_ms == int(value)


@pytest.mark.parametrize("value", [0, 1001, -5, "abc", 2.5, True])
def test_capture_interval_bounds_rejected(value):
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.config_from_dict({"capture_interval_ms": value})
    assert exc.value.field_name == "capture_interval_ms"


@pytest.mark.parametrize("value,ok", [(20, True), (100, True), (19, False), (101, False)])
def test_jpeg_quality_bounds(value, ok):
    if ok:
        assert cfg.config_from_dict({"jpeg_quality": value}).jpeg_quality == value
    else:
        with pytest.raises(cfg.ConfigError):
            cfg.config_from_dict({"jpeg_quality": value})


def test_image_format_choices():
    assert cfg.config_from_dict({"image_format": "JPEG"}).image_format == "jpeg"
    with pytest.raises(cfg.ConfigError):
        cfg.config_from_dict({"image_format": "gif"})


def test_auto_stop_bounds():
    assert cfg.config_from_dict({"auto_stop_minutes": 1440}).auto_stop_minutes == 1440
    with pytest.raises(cfg.ConfigError):
        cfg.config_from_dict({"auto_stop_minutes": 0})


@pytest.mark.parametrize("value", [0, "0", 1, 30, 3650])
def test_archive_after_accepts_zero_for_never(value):
    assert cfg.config_from_dict({"archive_after_days": value}).archive_after_days == int(value)
    assert cfg.ARCHIVE_AFTER_NEVER == cfg.ARCHIVE_AFTER_MIN_DAYS == 0


@pytest.mark.parametrize("value", [-1, 3651, "never", 1.5])
def test_archive_after_bounds_rejected(value):
    with pytest.raises(cfg.ConfigError):
        cfg.config_from_dict({"archive_after_days": value})


def test_cli_config_set_archive_after_zero(tmp_path: Path, capsys):
    from video_story.host import cli

    path = tmp_path / "config.json"
    assert cli.main(["--config", str(path), "config", "set", "archive_after_days", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["config"]["archive_after_days"] == 0
    assert cfg.load_config(path).archive_after_days == 0
    assert cli.main(["--config", str(path), "config", "set", "archive_after_days", "-1"]) != 0
    assert cfg.load_config(path).archive_after_days == 0


def test_config_roundtrip(tmp_path: Path):
    path = tmp_path / "config.json"
    c = cfg.Config(
        recording_folder=tmp_path / "rec",
        capture_interval_ms=1,
        image_format="jpeg",
        jpeg_quality=42,
    )
    cfg.save_config(c, path)
    loaded = cfg.load_config(path)
    assert loaded == c
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["capture_interval_ms"] == 1 and raw["image_format"] == "jpeg"


def test_update_config(tmp_path: Path):
    path = tmp_path / "config.json"
    cfg.save_config(cfg.Config(recording_folder=tmp_path), path)
    updated = cfg.update_config(path, capture_interval_ms=1000, audio_input="Mic A")
    assert updated.capture_interval_ms == 1000 and updated.audio_input == "Mic A"
    assert cfg.load_config(path) == updated


def test_missing_config_gives_defaults(tmp_path: Path):
    assert cfg.load_config(tmp_path / "none.json") == cfg.Config()


def test_config_path_env_override(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(cfg.CONFIG_PATH_ENV, str(tmp_path / "x.json"))
    assert cfg.config_path() == tmp_path / "x.json"


# Timeline ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "ms,text",
    [
        (0, "00:00:00.000"),
        (250, "00:00:00.250"),
        (61_001, "00:01:01.001"),
        (3_600_000 + 5, "01:00:00.005"),
    ],
)
def test_format_timestamp(ms, text):
    assert format_timestamp(ms) == text
    assert parse_timestamp(text) == ms


def test_clock():
    clock = RecordingClock()
    assert clock.elapsed_ms() == 0
    clock.start()
    assert clock.running
    total = clock.stop()
    assert total >= 0 and not clock.running
    assert clock.elapsed_ms() == total


# Naming ------------------------------------------------------------------------


def test_frame_names():
    assert screen_id(1) == "screen1"
    assert frame_filename(0, "screen1", "png") == "0_000_screen1.png"
    assert frame_filename(250, "screen2", "png") == "0_250_screen2.png"
    assert frame_filename(61_005, "screen1", "jpeg") == "61_005_screen1.jpg"
    assert sidecar_filename(250, "screen1") == "0_250_screen1.json"


def test_parse_frame_filename():
    parsed = parse_frame_filename("61_005_screen2.jpg")
    assert parsed is not None
    assert (parsed.time_ms, parsed.screen_id, parsed.extension) == (61_005, "screen2", "jpg")
    assert parse_frame_filename("0_250_screen1.json").is_sidecar
    assert parse_frame_filename("manifest.json") is None
    assert parse_frame_filename("1_25_screen1.png") is None  # millis must have 3 digits


# Screens and cursor ------------------------------------------------------------


def two_screens():
    # Mirrors the development machine: primary 2560x1600 at 150 %, secondary to the right.
    raw = [
        RawScreen(x=2560, y=0, width=1920, height=1080, primary=False, scale=1.0, name="B"),
        RawScreen(x=0, y=0, width=2560, height=1600, primary=True, scale=1.5, name="A"),
    ]
    return assign_screen_ids(raw)


def test_screen_numbering_primary_first():
    screens = two_screens()
    assert [s.id for s in screens] == ["screen1", "screen2"]
    assert screens[0].primary and screens[0].name == "A"
    assert screens[1].x == 2560


def test_screen_numbering_without_primary_flag():
    raw = [
        RawScreen(x=-1920, y=0, width=1920, height=1080),
        RawScreen(x=0, y=0, width=1920, height=1080),
    ]
    screens = assign_screen_ids(raw)
    assert screens[0].x == 0 and screens[0].primary
    assert screens[1].x == -1920


def test_cursor_mapping():
    screens = two_screens()
    assert locate_cursor(screens, 10, 20) is not None
    assert locate_cursor(screens, 10, 20).to_json() == {"screen": "screen1", "x": 10, "y": 20}
    pos = locate_cursor(screens, 2560 + 412, 388)
    assert pos is not None and (pos.screen_id, pos.x, pos.y) == ("screen2", 412, 388)
    assert locate_cursor(screens, 2559, 1599).screen_id == "screen1"
    assert locate_cursor(screens, 2560, 1599) is None  # gap below the smaller screen
    assert locate_cursor(screens, 5000, 0) is None


# Session -----------------------------------------------------------------------


def test_session_id_and_paths(tmp_path: Path):
    sid = new_session_id(tmp_path)
    assert is_session_id(sid)
    paths = SessionPaths(tmp_path, sid)
    paths.create_work_dirs()
    assert paths.frames_dir(paths.work_dir).is_dir()
    assert paths.work_dir == tmp_path / ".work" / sid
    (paths.work_dir / "audio.wav").write_bytes(b"x")
    paths.frames_dir(paths.work_dir).joinpath("0_000_screen1.png").write_bytes(b"y")
    state = SessionState(session_id=sid, status=Status.RECORDING, recording_folder=str(tmp_path))
    write_state(paths.state_path, state)
    final = paths.finalize()
    assert final == tmp_path / "default" / sid  # the main folder (default at first)
    assert (final / "frames" / "0_000_screen1.png").exists()
    assert paths.state_path.exists()  # state survives for `wait`
    assert read_state(paths.state_path).status is Status.RECORDING
    assert (
        new_session_id(
            tmp_path, when=__import__("datetime").datetime.strptime(sid, "%Y%m%d-%H%M%S")
        )
        == f"{sid}-2"
    )


def test_finalize_with_open_ui_log(tmp_path: Path):
    """The detached window process keeps ``ui.log`` open as its stdout; on Windows that
    blocks renaming the work directory, so finalisation must move the entries instead
    and leave ``ui.log`` (and ``state.json``) behind."""
    paths = SessionPaths(tmp_path, "20260101-000000")
    paths.create_work_dirs()
    paths.audio_path(paths.work_dir).write_bytes(b"x")
    (paths.work_dir / "manifest.json").write_text("{}", encoding="utf-8")
    paths.frames_dir(paths.work_dir).joinpath("0_000_screen1.png").write_bytes(b"y")
    paths.frames_info_dir(paths.work_dir).joinpath("0_000_screen1.json").write_bytes(b"{}")
    paths.lock_path.write_text("1", encoding="utf-8")
    write_state(
        paths.state_path,
        SessionState(session_id=paths.session_id, status=Status.PROCESSING, recording_folder="x"),
    )
    with open(paths.work_dir / "ui.log", "ab") as ui_log:  # held open like the child's stdout
        ui_log.write(b"window output\n")
        ui_log.flush()
        final = paths.finalize()
    assert final == tmp_path / "default" / paths.session_id
    assert sorted(p.name for p in final.iterdir()) == [
        "audio",
        "frames",
        "frames_info",
        "manifest.json",
    ]
    assert (final / "frames" / "0_000_screen1.png").read_bytes() == b"y"
    assert (final / "frames_info" / "0_000_screen1.json").read_bytes() == b"{}"
    assert (final / "audio" / "audio.wav").read_bytes() == b"x"
    assert list((final / "frames").glob("*.json")) == []
    assert sorted(p.name for p in paths.work_dir.iterdir()) == ["lock", "state.json", "ui.log"]
    assert read_state(paths.state_path).status is Status.PROCESSING
    assert paths.current_dir == final
    with pytest.raises(FileExistsError):
        paths.finalize()


def test_replace_with_retry_survives_transient_permission_error(tmp_path: Path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    real_replace = fileio.os.replace
    calls: list[int] = []

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "Access is denied")
        real_replace(a, b)

    monkeypatch.setattr(fileio.os, "replace", flaky)
    fileio.replace_with_retry(src, dst, attempts=5, delay=0)
    assert len(calls) == 3 and dst.read_text(encoding="utf-8") == "new" and not src.exists()

    calls.clear()
    src.write_text("newer", encoding="utf-8")
    with pytest.raises(PermissionError):
        fileio.replace_with_retry(src, dst, attempts=2, delay=0)
    assert len(calls) == 2 and dst.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_removes_tmp_on_failure(tmp_path: Path, monkeypatch):
    target = tmp_path / "sub" / "state.json"
    fileio.atomic_write_text(target, "{}")
    assert target.read_text(encoding="utf-8") == "{}" and list(target.parent.iterdir()) == [target]

    def always_denied(a, b):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(fileio.os, "replace", always_denied)
    monkeypatch.setattr(fileio, "REPLACE_ATTEMPTS", 2)
    monkeypatch.setattr(fileio, "REPLACE_DELAY_S", 0)
    with pytest.raises(PermissionError):
        fileio.atomic_write_text(target, "{broken")
    assert target.read_text(encoding="utf-8") == "{}" and list(target.parent.iterdir()) == [target]


def test_write_state_while_another_reader_holds_the_file(tmp_path: Path):
    """The agent polls ``state.json`` while the window rewrites it. On Windows a reader
    without ``FILE_SHARE_DELETE`` makes the rename fail with WinError 5; the write
    must wait for the reader instead of failing the recording."""
    import threading

    path = tmp_path / "state.json"
    state = SessionState(
        session_id="20260101-000000", status=Status.PROCESSING, recording_folder="x"
    )
    write_state(path, state)
    stop = threading.Event()
    seen: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                with open(path, "rb") as f:  # like the agent's ``video-story wait``
                    seen.append(f.read().decode("utf-8"))
            except FileNotFoundError:  # pragma: no cover - never expected: rename is atomic
                seen.append("MISSING")
            except PermissionError:  # the mirror image: opened while being replaced
                seen.append("")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for i in range(150):
            state.progress = i / 150
            write_state(path, state)
    finally:
        stop.set()
        thread.join(5)
    assert "MISSING" not in seen
    assert all(s == "" or json.loads(s)["session_id"] == "20260101-000000" for s in seen)
    assert read_state(path).progress == 149 / 150


def test_read_json_with_retry(tmp_path: Path, monkeypatch):
    path = tmp_path / "x.json"
    with pytest.raises(FileNotFoundError):
        fileio.read_json_with_retry(path)
    path.write_text('{"a": 1}', encoding="utf-8")
    monkeypatch.setattr(fileio, "READ_DELAY_S", 0)
    real_read_text = Path.read_text
    calls: list[int] = []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(13, "Access is denied")
        if len(calls) == 2:
            return '{"a": '  # torn read
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert fileio.read_json_with_retry(path) == {"a": 1} and len(calls) == 3
    calls.clear()
    with pytest.raises(PermissionError):
        fileio.read_json_with_retry(path, attempts=1)
    monkeypatch.setattr(Path, "read_text", real_read_text)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        fileio.read_json_with_retry(path, attempts=2)


def test_session_discard(tmp_path: Path):
    paths = SessionPaths(tmp_path, "20260101-000000")
    paths.create_work_dirs()
    (paths.work_dir / "audio.wav").write_bytes(b"x")
    write_state(
        paths.state_path,
        SessionState(
            session_id="20260101-000000", status=Status.READY, recording_folder=str(tmp_path)
        ),
    )
    paths.discard()
    assert paths.work_dir.exists() and paths.state_path.exists()
    assert (
        not (paths.work_dir / "audio.wav").exists()
        and not paths.frames_dir(paths.work_dir).exists()
    )
    paths.discard(keep_state=False)
    assert not paths.work_dir.exists()


def test_state_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    state = SessionState(
        session_id="20260101-000000", status=Status.PROCESSED, recording_folder=str(tmp_path)
    )
    write_state(path, state)
    loaded = read_state(path)
    assert loaded.status is Status.PROCESSED and loaded.status.final
    assert json.loads(path.read_text())["final"] is True


# Transcript and manifest -------------------------------------------------------


def test_transcript_text_format():
    t = Transcript(
        language="cs",
        segments=[Segment(0, 3480, "Otevři soubor."), Segment(3480, 7120, "Přidej tlačítko.")],
    )
    text = transcript_to_text(t)
    assert text.splitlines()[0] == "[00:00:00.000 --> 00:00:03.480] Otevři soubor."
    assert text.endswith("\n")
    assert t.segments_at(3480) == t.segments


def test_normalize_segments():
    segs = normalize_segments(
        [Segment(500, 400, " b "), Segment(-10, 100, "a"), Segment(0, 0, "   ")]
    )
    assert segs == [Segment(0, 100, "a"), Segment(500, 500, "b")]


def test_manifest_roundtrip(tmp_path: Path):
    m = Manifest(
        session_id="20260101-000000",
        started_at="2026-01-01T00:00:00.000+01:00",
        finished_at=None,
        recorded_duration_ms=1234,
        capture_interval_ms=250,
        image_format="png",
        jpeg_quality=None,
        screens=two_screens(),
        auto_stopped=True,
        auto_stop_minutes=30,
    )
    path = tmp_path / "manifest.json"
    write_manifest(path, m)
    loaded = read_manifest(path)
    assert loaded.session_id == m.session_id and loaded.auto_stopped
    assert [s.id for s in loaded.screens] == ["screen1", "screen2"]
    assert loaded.screens[0].scale == 1.5


# macOS style screens: bounds in points, images in pixels ----------------------


def test_pixel_ratio_mapping():
    raw = [
        RawScreen(x=0, y=0, width=1440, height=900, primary=True, scale=2.0, pixel_ratio=2.0),
        RawScreen(x=1440, y=0, width=1920, height=1080, scale=1.0, pixel_ratio=1.0),
    ]
    screens = assign_screen_ids(raw)
    retina, external = screens
    assert (retina.image_width, retina.image_height) == (2880, 1800)
    assert (external.image_width, external.image_height) == (1920, 1080)
    pos = locate_cursor(screens, 100, 50)
    assert pos is not None and (pos.screen_id, pos.x, pos.y) == ("screen1", 200, 100)
    edge = locate_cursor(screens, 1439, 899)
    assert edge is not None and (edge.x, edge.y) == (2878, 1798)
    ext = locate_cursor(screens, 1440 + 10, 20)
    assert ext is not None and (ext.screen_id, ext.x, ext.y) == ("screen2", 10, 20)
    data = retina.to_manifest()
    assert data["image_size"] == {"width": 2880, "height": 1800} and data["pixel_ratio"] == 2.0
    from video_story.core.screens import Screen

    assert Screen.from_manifest(data).pixel_ratio == 2.0


@pytest.mark.parametrize("value", [8, 16, 33, 128])
def test_drawing_grid_any_value_in_range_is_accepted(value):
    assert cfg.config_from_dict({"drawing_grid_px": value}).drawing_grid_px == value


@pytest.mark.parametrize("value", [7, 129, 0, "abc", 2.5])
def test_drawing_grid_out_of_range_is_rejected(value):
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.config_from_dict({"drawing_grid_px": value})
    assert exc.value.field_name == "drawing_grid_px"
