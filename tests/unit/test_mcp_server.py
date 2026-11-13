"""Tests for the MCP server tools (no network, no GUI, no devices)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import anyio
import pytest
from PIL import Image as PilImage

from video_story.core.config import Config
from video_story.core.manifest import Manifest, write_manifest
from video_story.core.naming import frame_filename, sidecar_filename
from video_story.core.screens import RawScreen, assign_screen_ids
from video_story.core.session import SessionPaths, SessionState, Status, write_state
from video_story.core.transcript import Segment, Transcript, write_transcript
from video_story.host import agent_output, mcp_server, runner
from video_story.host.runner import LaunchInfo, SessionError

SESSION_ID = "20260101-120000"


def make_recording(
    root: Path, session_id: str = SESSION_ID, image_format: str = "png"
) -> SessionPaths:
    """A processed recording: two screens, four ticks, real images, manifest and transcript."""
    paths = SessionPaths(root, session_id)
    paths.create_work_dirs()
    frames = paths.frames_dir(paths.work_dir)
    info = paths.frames_info_dir(paths.work_dir)
    screens = assign_screen_ids([RawScreen(0, 0, 64, 32, primary=True), RawScreen(64, 0, 64, 32)])
    for t in (0, 250, 500, 750):
        cursor = {"screen": "screen2" if t >= 500 else "screen1", "x": 40, "y": 10}
        for sid, colour in (("screen1", "red"), ("screen2", "blue")):
            image = PilImage.new("RGB", (64, 32), colour)
            image.save(frames / frame_filename(t, sid, image_format))
            (info / sidecar_filename(t, sid)).write_text(
                json.dumps({"time_ms": t, "screen": sid, "cursor": cursor}), encoding="utf-8"
            )
    write_manifest(
        paths.manifest_path(paths.work_dir),
        Manifest(
            session_id=session_id,
            started_at="x",
            finished_at="y",
            recorded_duration_ms=900,
            capture_interval_ms=250,
            image_format=image_format,
            jpeg_quality=None,
            screens=screens,
        ),
    )
    write_transcript(
        paths.transcript_txt_path(paths.work_dir),
        paths.transcript_json_path(paths.work_dir),
        Transcript("cs", [Segment(0, 400, "ahoj"), Segment(400, 900, "svete")], "small", "test"),
    )
    write_state(
        paths.state_path,
        SessionState(session_id=session_id, status=Status.PROCESSED, recording_folder=str(root)),
    )
    paths.finalize()
    return paths


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"recording_folder": str(tmp_path / "rec")}), encoding="utf-8")
    monkeypatch.setenv("VIDEO_STORY_CONFIG", str(path))
    return path


@pytest.fixture
def recording(config_file: Path, tmp_path: Path) -> SessionPaths:
    return make_recording(tmp_path / "rec")


# start ------------------------------------------------------------------------


def test_start_ok(config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_launch(config: Config, cfg_file: Path | None) -> LaunchInfo:
        assert config.recording_folder == tmp_path / "rec"
        assert cfg_file == config_file
        return LaunchInfo("20260101-130000", tmp_path / "state.json", 4242, tmp_path / "rec")

    monkeypatch.setattr(mcp_server, "launch_control_window", fake_launch)
    result = mcp_server.tool_start(config_file)
    assert result["ok"] is True
    assert result["session_id"] == "20260101-130000" and result["pid"] == 4242
    assert "video_story_wait" in result["next"]


def test_start_already_running(config_file: Path, tmp_path: Path, monkeypatch):
    paths = SessionPaths(tmp_path / "rec", "20260101-140000")
    paths.work_dir.mkdir(parents=True)
    write_state(
        paths.state_path,
        SessionState(
            session_id=paths.session_id,
            status=Status.RECORDING,
            recording_folder=str(paths.recording_folder),
            pid=7,
        ),
    )
    monkeypatch.setattr(runner, "pid_alive", lambda pid: pid == 7)

    def fake_launch(config: Config, cfg_file: Path | None) -> LaunchInfo:
        raise SessionError("ALREADY_RUNNING", "a window is already open")

    monkeypatch.setattr(mcp_server, "launch_control_window", fake_launch)
    result = mcp_server.tool_start(config_file)
    assert result["ok"] is False and result["code"] == "ALREADY_RUNNING"
    assert result["session_id"] == "20260101-140000" and result["status"] == "recording"


# wait / status ----------------------------------------------------------------


def test_wait_times_out_and_clamps(config_file: Path, tmp_path: Path, monkeypatch):
    paths = SessionPaths(tmp_path / "rec", "20260101-150000")
    paths.work_dir.mkdir(parents=True)
    write_state(
        paths.state_path,
        SessionState(
            session_id=paths.session_id,
            status=Status.RECORDING,
            recording_folder=str(paths.recording_folder),
            pid=1,
        ),
    )
    monkeypatch.setattr(runner, "pid_alive", lambda pid: True)
    monkeypatch.setattr(mcp_server, "WAIT_TIMEOUT_MIN_S", 0.0)
    result = mcp_server.tool_wait(paths.session_id, timeout_s=-5, config_file=config_file)
    assert result["ok"] is True and result["timed_out"] is True
    assert result["status"] == "recording" and "video_story_wait" in result["hint"]
    assert mcp_server.clamp_timeout(1000) == 240.0
    assert mcp_server.clamp_timeout("x") == 60.0


def test_wait_final_and_progress(recording: SessionPaths, config_file: Path, monkeypatch):
    result = mcp_server.tool_wait(recording.session_id, timeout_s=5, config_file=config_file)
    assert result["ok"] is True and result["final"] is True and result["status"] == "processed"
    assert result["timed_out"] is False

    reports: list[tuple[float, str]] = []

    async def progress(elapsed: float, message: str) -> None:
        reports.append((elapsed, message))

    monkeypatch.setattr(mcp_server, "WAIT_PROGRESS_EVERY_S", 0.0)
    monkeypatch.setattr(mcp_server, "POLL_INTERVAL_S", 0.01)
    paths = SessionPaths(recording.recording_folder, "20260101-150001")
    paths.work_dir.mkdir(parents=True)
    write_state(
        paths.state_path,
        SessionState(
            session_id=paths.session_id,
            status=Status.PROCESSING,
            recording_folder=str(paths.recording_folder),
            pid=1,
            message="transcribing",
        ),
    )
    monkeypatch.setattr(runner, "pid_alive", lambda pid: True)
    data = anyio.run(mcp_server.wait_with_progress, paths, 0.05, progress)
    assert data["timed_out"] is True
    assert reports and reports[0][1] == "processing: transcribing"


def test_wait_unknown_session(config_file: Path):
    result = mcp_server.tool_wait("not-a-session", config_file=config_file)
    assert result["ok"] is False and result["code"] == "INVALID_SESSION"
    result = mcp_server.tool_wait("20990101-000000", config_file=config_file)
    assert result["ok"] is False and result["code"] == "UNKNOWN_SESSION"


def test_status(recording: SessionPaths, config_file: Path):
    result = mcp_server.tool_status(recording.session_id, config_file)
    assert result["ok"] is True and result["status"] == "processed" and result["final"] is True
    by_dir = mcp_server.tool_status(str(recording.final_dir), config_file)
    assert by_dir["session_id"] == recording.session_id


# describe / transcript --------------------------------------------------------


def test_describe(recording: SessionPaths, config_file: Path):
    result = mcp_server.tool_describe(recording.session_id, config_file)
    assert result["ok"] is True
    assert result["recording_dir"] == str(recording.final_dir)
    assert result["manifest_content"]["recorded_duration_ms"] == 900
    assert "ahoj" in result["transcript_in_range"] and result["transcript_in_range_inline"]
    assert "transcript" not in result  # not twice: get_transcript has the whole one
    assert Path(result["frames_dir"]).is_dir()


def test_describe_leaves_a_long_transcript_to_the_paged_tools(
    recording: SessionPaths, config_file: Path
):
    result = mcp_server.tool_describe(recording.session_id, config_file, inline_chars=10)
    assert result["ok"] is True and "transcript_in_range" not in result
    assert result["transcript_in_range_inline"] is False
    assert result["transcript_in_range_lines"] == 2
    assert "video_story_timeline" in result["transcript_in_range_note"]
    assert "video_story_get_transcript" in result["transcript_in_range_note"]


def test_get_transcript_formats(recording: SessionPaths, config_file: Path):
    text = mcp_server.tool_get_transcript(recording.session_id, "text", config_file)
    assert text["ok"] is True and text["text"].startswith("[00:00:00.000 --> 00:00:00.400] ahoj")
    data = mcp_server.tool_get_transcript(recording.session_id, "json", config_file)
    assert data["language"] == "cs" and data["model"] == "small"
    assert data["segments"][1] == {"start_ms": 400, "end_ms": 900, "text": "svete"}
    bad = mcp_server.tool_get_transcript(recording.session_id, "xml", config_file)
    assert bad["ok"] is False and bad["code"] == "USAGE"


def test_timeline(recording: SessionPaths, config_file: Path):
    result = mcp_server.tool_timeline(recording.session_id, config_file=config_file)
    assert result["ok"] is True and result["counts"]["speech"] == 2
    assert "0-400 SAY ahoj" in result["timeline"]
    assert "CURSOR -> screen2" in result["timeline"]
    part = mcp_server.tool_timeline(recording.session_id, 500, 900, config_file)
    assert "SAY ahoj" not in part["timeline"] and "SAY svete" in part["timeline"]
    bad = mcp_server.tool_timeline(recording.session_id, 900, 100, config_file)
    assert bad["ok"] is False and bad["code"] == "USAGE"


# paging (hosts without file access: no result grows with the recording) --------


def test_timeline_pages_by_whole_lines(
    recording: SessionPaths, config_file: Path, monkeypatch: pytest.MonkeyPatch
):
    whole = mcp_server.tool_timeline(recording.session_id, config_file=config_file)
    assert (whole["first_line"], whole["next_line"]) == (1, None)
    assert whole["timeline_lines"] == len(whole["timeline"].splitlines()) == 4
    assert whole["timeline_chars"] == len(whole["timeline"])
    assert "page_note" not in whole and "legend" in whole
    # A max_chars below the minimum is raised to it: everything still fits one page.
    tiny = mcp_server.tool_timeline(recording.session_id, config_file=config_file, max_chars=1)
    assert tiny["next_line"] is None and tiny["timeline"] == whole["timeline"]

    monkeypatch.setattr(agent_output, "PAGE_MIN_CHARS", 1)
    text, first, pages = "", 1, 0
    while first is not None:
        page = mcp_server.tool_timeline(
            recording.session_id, config_file=config_file, first_line=first, max_chars=70
        )
        assert page["ok"] is True and page["first_line"] == first
        assert page["timeline"].endswith("\n") and len(page["timeline"]) <= 70
        assert page["timeline_lines"] == 4 and page["counts"]["speech"] == 2
        assert ("legend" in page) == (first == 1)  # explained once, with the first page
        if page["next_line"] is not None:
            assert f"first_line={page['next_line']}" in page["page_note"]
        text += page["timeline"]
        first = page["next_line"]
        pages += 1
    assert text == whole["timeline"] and pages > 1

    past = mcp_server.tool_timeline(recording.session_id, config_file=config_file, first_line=99)
    assert past["ok"] is True and past["timeline"] == "" and past["next_line"] is None
    one = mcp_server.tool_timeline(
        recording.session_id, config_file=config_file, first_line=2, max_chars=5
    )  # a line longer than max_chars comes whole and alone
    assert one["timeline"] == "0-400 SAY ahoj [cursor screen1 40,10]\n" and one["next_line"] == 3


def test_get_transcript_pages(
    recording: SessionPaths, config_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(agent_output, "PAGE_MIN_CHARS", 1)
    session = recording.session_id
    first = mcp_server.tool_get_transcript(session, "text", config_file, max_chars=40)
    assert first["text"] == "[00:00:00.000 --> 00:00:00.400] ahoj\n"
    assert (first["transcript_lines"], first["first_line"], first["next_line"]) == (2, 1, 2)
    rest = mcp_server.tool_get_transcript(session, "text", config_file, first_line=2, max_chars=40)
    assert rest["text"].endswith("svete\n") and rest["next_line"] is None
    data = mcp_server.tool_get_transcript(session, "json", config_file, first_line=2, max_chars=5)
    assert data["segments"] == [{"start_ms": 400, "end_ms": 900, "text": "svete"}]
    assert (data["transcript_lines"], data["next_line"], data["language"]) == (2, None, "cs")


def test_mouse_pages_actions_and_rest_points_in_time_order(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from test_events import make_recording as make_events_recording
    from test_mouse_timeline import Mouse, search

    from video_story.core.input_events import ElementInfo, HoverRecord

    paths = make_events_recording(tmp_path / "rec", {})
    m = Mouse(paths)
    for t in (1000, 2000, 3000):
        m.click(t, 20, 15, search(f"value {t}"))
    hover = m.writer.write(HoverRecord(t=1500, screen="screen1", x=70, y=70, gx=70, gy=70))
    m.writer.write_element(1510, hover, ElementInfo(role="button", name="Save"), win=m.win_notepad)
    m.close()

    whole = mcp_server.tool_mouse(paths.session_id, config_file=config_file)
    assert whole["ok"] is True and whole["next_line"] is None
    assert (whole["action_count"], whole["rest_point_count"], whole["mouse_lines"]) == (3, 1, 4)
    assert len(whole["actions"]) == 3 and len(whole["rest_points"]) == 1

    monkeypatch.setattr(agent_output, "PAGE_MIN_CHARS", 1)
    seen, first = [], 1
    while first is not None:  # every record is longer than max_chars: one per page
        page = mcp_server.tool_mouse(
            paths.session_id, config_file=config_file, first_line=first, max_chars=10
        )
        (record,) = page["actions"] + page["rest_points"]
        seen.append((record["kind"], record["t"]))
        assert (page["action_count"], page["rest_point_count"]) == (3, 1)
        first = page["next_line"]
    assert seen == [("click", 1000), ("rest", 1500), ("click", 2000), ("click", 3000)]

    one = mcp_server.tool_mouse(paths.session_id, at_ms=2000, config_file=config_file)
    assert [a["t"] for a in one["actions"]] == [2000] and "next_line" not in one


# frames -----------------------------------------------------------------------


def test_list_frames(recording: SessionPaths, config_file: Path):
    result = mcp_server.tool_list_frames(recording.session_id, 600, config_file=config_file)
    assert result["ok"] is True and result["cursor_screen"] == "screen2"
    assert result["frames"][0]["time_ms"] == 500
    windowed = mcp_server.tool_list_frames(
        recording.session_id, 300, screen="all", window_ms=260, config_file=config_file
    )
    assert [f["time_ms"] for f in windowed["window"]["screen1"]] == [250, 500]
    bad = mcp_server.tool_list_frames(
        recording.session_id, 0, screen="screen9", config_file=config_file
    )
    assert bad["ok"] is False and bad["code"] == "UNKNOWN_SCREEN"


def _decode(result: list) -> PilImage.Image:
    image = result[0]
    assert isinstance(image, mcp_server.Image)
    content = image.to_image_content()
    assert content.type == "image"
    return PilImage.open(io.BytesIO(image.data))


def test_get_frame_by_at_ms_downscales(recording: SessionPaths, config_file: Path):
    result = mcp_server.tool_get_frame(
        recording.session_id, at_ms=600, max_width=32, config_file=config_file
    )
    assert isinstance(result, list)
    picture = _decode(result)
    assert picture.size == (32, 16) and picture.format == "PNG"
    assert picture.getpixel((0, 0)) == (0, 0, 255)  # screen2 is blue
    meta = result[1]
    assert meta["ok"] is True and meta["screen"] == "screen2" and meta["time_ms"] == 500
    assert meta["cursor"] == {"screen": "screen2", "x": 40, "y": 10}
    assert meta["scale"] == 2.0 and meta["cursor_in_image"] == {"x": 20, "y": 5}
    assert (meta["original_width"], meta["width"]) == (64, 32)
    assert result[0].to_image_content().mime_type == "image/png"


def test_get_frame_by_path_keeps_small_images(recording: SessionPaths, config_file: Path):
    frame = recording.frames_dir() / frame_filename(250, "screen1", "png")
    result = mcp_server.tool_get_frame(
        recording.session_id, path=str(frame), config_file=config_file
    )
    picture = _decode(result)
    assert picture.size == (64, 32) and picture.getpixel((0, 0)) == (255, 0, 0)
    meta = result[1]
    assert meta["screen"] == "screen1" and meta["time_ms"] == 250 and meta["scale"] == 1.0
    assert meta["image"] == str(frame)


def test_get_frame_jpeg_source(config_file: Path, tmp_path: Path):
    paths = make_recording(tmp_path / "rec", "20260101-120001", image_format="jpeg")
    result = mcp_server.tool_get_frame(
        paths.session_id, at_ms=0, screen="screen1", max_width=32, config_file=config_file
    )
    picture = _decode(result)
    assert picture.format == "JPEG" and picture.size == (32, 16)
    assert result[1]["format"] == "jpeg"
    assert result[0].to_image_content().mime_type == "image/jpeg"


def test_get_frame_rejects_outside_paths(
    recording: SessionPaths, config_file: Path, tmp_path: Path
):
    outside = tmp_path / "0_250_screen1.png"
    PilImage.new("RGB", (8, 8)).save(outside)
    result = mcp_server.tool_get_frame(
        recording.session_id, path=str(outside), config_file=config_file
    )
    assert result["ok"] is False and result["code"] == "FRAME_OUTSIDE_SESSION"
    sneaky = recording.frames_dir() / ".." / "manifest.json"
    result = mcp_server.tool_get_frame(
        recording.session_id, path=str(sneaky), config_file=config_file
    )
    assert result["ok"] is False and result["code"] == "FRAME_OUTSIDE_SESSION"
    sidecar = recording.frames_dir() / sidecar_filename(250, "screen1")
    result = mcp_server.tool_get_frame(
        recording.session_id, path=str(sidecar), config_file=config_file
    )
    assert result["ok"] is False and result["code"] == "NOT_A_FRAME"


def test_get_frame_usage_errors(recording: SessionPaths, config_file: Path):
    result = mcp_server.tool_get_frame(recording.session_id, config_file=config_file)
    assert result["ok"] is False and result["code"] == "USAGE"
    result = mcp_server.tool_get_frame(
        recording.session_id, at_ms=0, screen="all", config_file=config_file
    )
    assert result["ok"] is False and result["code"] == "USAGE"
    missing = recording.frames_dir() / frame_filename(999_000, "screen1", "png")
    result = mcp_server.tool_get_frame(
        recording.session_id, path=str(missing), config_file=config_file
    )
    assert result["ok"] is False and result["code"] == "NOT_FOUND"


# config / doctor --------------------------------------------------------------


def test_config_get_set_path(config_file: Path):
    path = mcp_server.tool_config("path")
    assert path == {"ok": True, "path": str(config_file), "exists": True}
    got = mcp_server.tool_config("get", "capture_interval_ms")
    assert got["value"] == 500
    whole = mcp_server.tool_config("get")
    assert whole["config"]["recording_folder"].endswith("rec")
    changed = mcp_server.tool_config("set", "capture_interval_ms", "250")
    assert changed["ok"] is True and changed["config"]["capture_interval_ms"] == 250
    assert json.loads(config_file.read_text(encoding="utf-8"))["capture_interval_ms"] == 250
    assert mcp_server.tool_config("set", "audio_input", "null")["config"]["audio_input"] is None
    invalid = mcp_server.tool_config("set", "capture_interval_ms", "99999")
    assert invalid["ok"] is False and invalid["code"] == "CONFIG_INVALID"
    unknown = mcp_server.tool_config("get", "nope")
    assert unknown["ok"] is False and unknown["code"] == "UNKNOWN_KEY"
    unknown = mcp_server.tool_config("set", "nope", "1")
    assert unknown["ok"] is False and unknown["code"] == "UNKNOWN_KEY"
    usage = mcp_server.tool_config("reset")
    assert usage["ok"] is False and usage["code"] == "USAGE"


def test_doctor_import_failure(monkeypatch: pytest.MonkeyPatch):
    import builtins

    real_import = builtins.__import__

    def failing(name, *args, **kwargs):
        if name == "video_story.host.doctor":
            raise ImportError("boom")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing)
    result = mcp_server.tool_doctor()
    assert result["ok"] is False and result["code"] == "DOCTOR_UNAVAILABLE"


# server -----------------------------------------------------------------------


# export -----------------------------------------------------------------------


def test_export_tool_resolves_relative_output_and_maps_oserror(
    recording: SessionPaths, config_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The engine itself is exercised in test_export_ffmpeg.py; here it is replaced."""
    from video_story.export import ExportResult, ffmpeg_export

    requests = []

    def fake_export(request, config, progress=None, cancel=None):
        requests.append(request)
        if request.output.name == "fail.mp4":
            raise PermissionError(13, "Access is denied", str(request.output))
        return ExportResult(
            output=request.output,
            srt=None,
            screen_id=request.screen_id,
            start_ms=0,
            end_ms=900,
            duration_ms=900,
            width=64,
            height=32,
            fps=24,
            frames_used=4,
            ffmpeg="ffmpeg",
        )

    monkeypatch.setattr(ffmpeg_export, "export_recording", fake_export)
    result = mcp_server.tool_export(
        recording.session_id, output="clips/relative.mp4", config_file=config_file
    )
    assert result["ok"] is True
    assert requests[-1].output == recording.recording_folder / "clips" / "relative.mp4"
    absolute = recording.recording_folder.parent / "elsewhere.mp4"
    assert mcp_server.tool_export(
        recording.session_id, output=str(absolute), config_file=config_file
    )["ok"]
    assert requests[-1].output == absolute
    assert mcp_server.tool_export(recording.session_id, config_file=config_file)["ok"]
    assert requests[-1].output == recording.recording_folder / f"{SESSION_ID}_screen1.mp4"

    failed = mcp_server.tool_export(
        recording.session_id, output="fail.mp4", config_file=config_file
    )
    assert failed["ok"] is False and failed["code"] == "OUTPUT_ERROR"
    assert "Access is denied" in failed["message"] and "fail.mp4" in failed["message"]


def test_create_server_registers_tools(config_file: Path, recording: SessionPaths):
    server = mcp_server.create_server(config_file)
    assert server.name == "video-story"
    assert "video_story_wait" in server.instructions
    tools = anyio.run(server.list_tools)
    names = sorted(t.name for t in tools)
    assert names == [
        "video_story_config",
        "video_story_describe",
        "video_story_doctor",
        "video_story_edit",
        "video_story_export",
        "video_story_get_frame",
        "video_story_get_transcript",
        "video_story_list_frames",
        "video_story_mouse",
        "video_story_start",
        "video_story_status",
        "video_story_timeline",
        "video_story_wait",
        "video_story_wait_editor",
    ]
    wait = next(t for t in tools if t.name == "video_story_wait")
    assert set(wait.input_schema["properties"]) == {"session_id", "timeout_s"}
    for name in ("video_story_timeline", "video_story_mouse", "video_story_get_transcript"):
        paged = next(t for t in tools if t.name == name)
        assert {"first_line", "max_chars"} <= set(paged.input_schema["properties"])
    describe = next(t for t in tools if t.name == "video_story_describe")
    assert set(describe.input_schema["properties"]) == {"session_id"}
    page = anyio.run(
        server.call_tool,
        "video_story_timeline",
        {"session_id": recording.session_id, "first_line": 3},
    )
    assert page.structured_content["first_line"] == 3
    assert page.structured_content["timeline"].startswith("400-900 SAY svete")

    # Calling through the server exercises the wrappers and result conversion.
    status = anyio.run(server.call_tool, "video_story_status", {"session_id": recording.session_id})
    assert status.structured_content["status"] == "processed"
    frame = anyio.run(
        server.call_tool,
        "video_story_get_frame",
        {"session_id": recording.session_id, "at_ms": 0, "max_width": 16},
    )
    assert [c.type for c in frame.content] == ["image", "text"]
    assert json.loads(frame.content[1].text)["width"] == 16
    waited = anyio.run(
        server.call_tool, "video_story_wait", {"session_id": recording.session_id, "timeout_s": 1}
    )
    assert waited.structured_content["final"] is True


def test_serve_rejects_unknown_transport():
    with pytest.raises(ValueError):
        mcp_server.serve(transport="carrier-pigeon")
