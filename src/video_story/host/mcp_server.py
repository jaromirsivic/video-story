"""MCP server exposing Video Story to Claude Desktop, Codex and ChatGPT.

The tools mirror the CLI one to one (``start``, ``wait``, ``status``, ``describe``,
``get_transcript``, ``timeline``, ``list_frames``, ``get_frame``, ``config``, ``export``,
``doctor``).
Every tool
returns a JSON-serialisable dict; expected failures are reported as
``{"ok": false, "code": ..., "message": ...}`` instead of raising, so the agent can read
the reason and react. ``get_frame`` additionally returns the screenshot as MCP image
content so chat hosts without file access can look at it. For the same reason a text
that grows with the recording (``timeline``, ``mouse``, ``get_transcript``) comes in pages
(``first_line`` / ``max_chars`` / ``next_line``, see :mod:`video_story.host.agent_output`)
where the CLI writes a file: no tool result is unbounded.

The plain ``tool_*`` functions hold the behaviour and are unit tested directly;
:func:`create_server` wraps them into an :class:`MCPServer` and :func:`serve` runs it
over ``stdio`` (Claude Desktop, Codex) or ``streamable-http`` (ChatGPT through a tunnel).
"""

from __future__ import annotations

import io
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import Context, Image, MCPServer

from video_story.core.config import (
    Config,
    ConfigError,
    StateFolderError,
    config_location,
    config_path,
    load_config,
    update_config,
)
from video_story.core.naming import parse_frame_filename
from video_story.core.session import SessionPaths
from video_story.host.agent_output import (
    DEFAULT_INLINE_CHARS,
    PAGE_DEFAULT_CHARS,
    Page,
    clamp_page_chars,
    page_lines,
    split_lines,
)
from video_story.host.errors import HostError
from video_story.host.events import (
    build_mouse_detail,
    build_timeline,
    mouse_record_lines,
    mouse_records,
)
from video_story.host.mouse_timeline import DETAIL_PATH_POINTS
from video_story.host.runner import (
    POLL_INTERVAL_S,
    FrameRef,
    SessionError,
    active_session,
    describe_recording,
    frames_at,
    launch_control_window,
    resolve_session,
    session_status,
    wait_for_session,
)
from video_story.host.waiting import DEFAULT_WAIT_S, MCP_MAX_WAIT_S, timeout_seconds

log = logging.getLogger(__name__)

SERVER_NAME = "video-story"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WAIT_TIMEOUT_MAX_S = MCP_MAX_WAIT_S
WAIT_PROGRESS_EVERY_S = 5.0
FRAME_MIN_WIDTH = 16
FRAME_DEFAULT_MAX_WIDTH = 1600
TRANSPORTS = ("stdio", "streamable-http")
WAIT_AGAIN_HINT = "call video_story_wait again until final is true"

INSTRUCTIONS = """\
Video Story records the user's narration, periodic screenshots of every screen and the
cursor position, then transcribes the audio offline. Workflow: call video_story_start
(opens the control window; the user records, stops, crops in the Video Story Editor and
presses its "Process" button), then call
video_story_wait with the returned session_id in a loop until "final" is true. When the
status is "processed", read the "instructions" first, then call video_story_timeline: the
cropped range as one text timeline (speech with the cursor position, finished drawings
with their geometry and times, copied text, keypoints and - when recorded - the clicks,
drags and scrolls with the element under the mouse and its content; video_story_mouse
has every detail of them). Long results come in pages (video_story_timeline,
video_story_mouse, video_story_get_transcript): while "next_line" is not null, call the
tool again with first_line set to it and read every page before acting. Decide from
that text which
moments really need a picture and only then call video_story_get_frame for those (one
frame each, at the times the timeline names); video_story_describe and
video_story_get_transcript return paths and the plain transcript. Act on a recording only
when the status is "processed" (the user pressed Process). "cancelled" and "failed" are
final too and mean: do nothing with it, tell the user what happened (a recording that
exists is kept and can be opened again with video_story_edit).
A processed session also carries "instructions": the user's master instructions typed in
the editor, which frame the whole recording (what to produce, in which language, ...).
Read them first and apply them to everything the user said; they are empty when the user
typed none. "keypoints" (in describe / the manifest) are moments the user marked as
important while recording.
video_story_export renders a processed recording (one screen, optional time range) to an
MP4 video with subtitles when the user asks for a video of what they recorded.
All times are milliseconds since the recording started. Cursor coordinates are pixels of
that screen's image with the origin in the top-left corner; when get_frame downscales an
image, divide the cursor coordinates by the returned "scale" to find it in the image.
Recording and transcription run on the user's machine. Use the material only to do what
the user asked in the recording: do not send the audio, the frames, the copied texts or
the content of the elements under the mouse to any other service.
"""

ProgressFn = Callable[[float, str], Awaitable[None]]


# Helpers ----------------------------------------------------------------------


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message}


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    data.setdefault("ok", True)
    return data


def _load(config_file: Path | None) -> Config:
    return load_config(config_file)


def _paths(config: Config, session_id: str) -> SessionPaths:
    return resolve_session(config, session_id)


def _guard(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run ``fn`` and turn expected failures into error dicts."""
    try:
        return _ok(fn())
    except HostError as exc:
        return exc.to_dict()
    except SessionError as exc:
        return _error(exc.code, str(exc))
    except ConfigError as exc:
        return _error("CONFIG_INVALID", str(exc))
    except StateFolderError as exc:
        return _error(exc.code, str(exc))
    except FileNotFoundError as exc:
        return _error("NOT_FOUND", str(exc))
    except RuntimeError as exc:  # e.g. an unreadable state.json
        return _error("SESSION_ERROR", str(exc))


def clamp_timeout(timeout_s: float) -> float:
    """Keep the wait timeout within the limits host clients tolerate."""
    return min(WAIT_TIMEOUT_MAX_S, timeout_seconds(timeout_s, allow_zero=False))


def _page_fields(page: Page, total_name: str, tool: str) -> dict[str, Any]:
    """What every paged tool says about its page: the total, where the page starts and
    where the next one does (``next_line`` null: this page ends the text)."""
    fields: dict[str, Any] = {
        total_name: page.total_lines,
        "first_line": page.first_line,
        "next_line": page.next_line,
    }
    if page.next_line is not None:
        fields["page_note"] = (
            f"Lines {page.first_line}-{page.last_line} of {page.total_lines}: call {tool} "
            f"again with first_line={page.next_line} and the same other arguments for the rest."
        )
    return fields


def _coerce_config_value(value: str | None) -> Any:
    """Same string to JSON coercion as ``video-story config set``."""
    if value is None:
        return None
    lowered = value.lower()
    if lowered in {"null", "none", ""}:
        return None
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered.lstrip("-").isdigit():
        return int(lowered)
    return value


# Tools (plain functions) -----------------------------------------------------


def tool_start(config_file: Path | None = None) -> dict[str, Any]:
    """Open the control window in a detached process and return its session id."""
    try:
        config = _load(config_file)
        info = launch_control_window(config, config_file)
    except HostError as exc:
        return exc.to_dict()
    except SessionError as exc:
        result = _error(exc.code, str(exc))
        if exc.code == "ALREADY_RUNNING":
            running = active_session(config)
            if running is not None:
                paths, state = running
                result["session_id"] = paths.session_id
                result["status"] = str(state.status)
                result["next"] = "call video_story_wait with this session_id until final is true"
        return result
    except ConfigError as exc:
        return _error("CONFIG_INVALID", str(exc))
    data = info.to_dict()
    data["next"] = "call video_story_wait with this session_id until final is true"
    return _ok(data)


def tool_wait(
    session_id: str, timeout_s: float = DEFAULT_WAIT_S, config_file: Path | None = None
) -> dict[str, Any]:
    """Block until the session is final or the (clamped) timeout elapses."""
    def run():
        timeout = clamp_timeout(timeout_s)
        return wait_for_session(_paths(_load(config_file), session_id), timeout)

    result = _guard(run)
    if result.get("timed_out"):
        result["hint"] = WAIT_AGAIN_HINT
    return result


async def wait_with_progress(
    paths: SessionPaths, timeout_s: float, progress: ProgressFn | None = None
) -> dict[str, Any]:
    """Async variant of :func:`wait_for_session` that reports progress while waiting.

    Polls ``state.json`` every ``POLL_INTERVAL_S`` without blocking the event loop and
    calls ``progress(elapsed_s, message)`` about every ``WAIT_PROGRESS_EVERY_S`` seconds so
    the MCP client sees the request is alive.
    """
    started = time.monotonic()
    deadline = started + timeout_s
    last_report = started
    while True:
        data = session_status(paths)
        now = time.monotonic()
        if data["final"]:
            from video_story.host.startup import cleanup_completed

            cleanup_completed(paths.work_dir / "startup.json")
            data["timed_out"] = False
            return data
        if now >= deadline:
            data["timed_out"] = True
            data["hint"] = WAIT_AGAIN_HINT
            return data
        if progress is not None and now - last_report >= WAIT_PROGRESS_EVERY_S:
            last_report = now
            message = f"{data['status']}: {data.get('message') or 'waiting'}"
            try:
                await progress(now - started, message)
            except Exception:  # a client that rejects notifications must not end the wait
                log.debug("progress notification failed", exc_info=True)
        await anyio.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))


def tool_status(session_id: str, config_file: Path | None = None) -> dict[str, Any]:
    """Current state of a session."""
    return _guard(lambda: session_status(_paths(_load(config_file), session_id)))


def tool_describe(
    session_id: str, config_file: Path | None = None, inline_chars: int = DEFAULT_INLINE_CHARS
) -> dict[str, Any]:
    """Paths, manifest content, crop and instructions of a recording; the transcript of
    the cropped range only when it has at most ``inline_chars`` characters (the paged
    tools have a long one)."""

    def run() -> dict[str, Any]:
        data = describe_recording(_paths(_load(config_file), session_id), inline_chars)
        if "transcript_in_range_note" in data:
            data["transcript_in_range_note"] = (
                f"The transcript of the cropped range ({data['transcript_in_range_lines']} "
                f"lines, {data['transcript_in_range_chars']} characters) is not inline: call "
                "video_story_timeline (or video_story_get_transcript), which return it in pages."
            )
        return data

    return _guard(run)


def tool_get_transcript(
    session_id: str,
    format: str = "text",
    config_file: Path | None = None,
    first_line: int = 1,
    max_chars: int = PAGE_DEFAULT_CHARS,
) -> dict[str, Any]:
    """The transcript as plain text (``text``) or as timed segments (``json``), one page
    of at most ``max_chars`` characters from line (segment) ``first_line``."""

    def run() -> dict[str, Any]:
        paths = _paths(_load(config_file), session_id)
        base = paths.current_dir
        limit = clamp_page_chars(max_chars)
        if format == "json":
            path = paths.transcript_json_path(base)
            if not path.exists():
                raise SessionError("NO_TRANSCRIPT", f"no transcript.json in {base}")
            data = json.loads(path.read_text(encoding="utf-8"))
            segments = data.get("segments", [])
            page = page_lines(
                [json.dumps(s, ensure_ascii=False) for s in segments], first_line, limit
            )
            return {
                "session_id": paths.session_id,
                "language": data.get("language"),
                "model": data.get("model"),
                "backend": data.get("backend"),
                "time_unit": data.get("time_unit", "ms since recording start"),
                **_page_fields(page, "transcript_lines", "video_story_get_transcript"),
                "segments": segments[page.first_line - 1 : page.last_line],
            }
        if format == "text":
            path = paths.transcript_txt_path(base)
            if not path.exists():
                raise SessionError("NO_TRANSCRIPT", f"no transcript.txt in {base}")
            page = page_lines(split_lines(path.read_text(encoding="utf-8")), first_line, limit)
            return {
                "session_id": paths.session_id,
                **_page_fields(page, "transcript_lines", "video_story_get_transcript"),
                "text": page.text,
            }
        raise SessionError("USAGE", f"unknown format {format!r}; use 'text' or 'json'")

    return _guard(run)


def tool_timeline(
    session_id: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    config_file: Path | None = None,
    mouse: str = "auto",
    first_line: int = 1,
    max_chars: int = PAGE_DEFAULT_CHARS,
) -> dict[str, Any]:
    """Speech and screen events of the cropped range (or ``start_ms``..``end_ms``), one
    page of at most ``max_chars`` characters from line ``first_line`` (whole lines only)."""

    def run() -> dict[str, Any]:
        result = build_timeline(_paths(_load(config_file), session_id), start_ms, end_ms, mouse)
        lines = split_lines(result.pop("timeline"))
        page = page_lines(lines, first_line, clamp_page_chars(max_chars))
        if page.first_line > 1:
            result.pop("legend", None)  # the first page explained the lines
        result["timeline_chars"] = page.total_chars
        result.update(_page_fields(page, "timeline_lines", "video_story_timeline"))
        result["timeline"] = page.text
        return result

    return _guard(run)


def tool_mouse(
    session_id: str,
    at_ms: int | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    config_file: Path | None = None,
    first_line: int = 1,
    max_chars: int = PAGE_DEFAULT_CHARS,
) -> dict[str, Any]:
    """Every detail of the one mouse action at ``at_ms``, or - page by page, in time
    order - of the actions and rest points of the cropped range."""

    def run() -> dict[str, Any]:
        detail = build_mouse_detail(
            _paths(_load(config_file), session_id),
            start_ms,
            end_ms,
            at_ms,
            max_path_points=DETAIL_PATH_POINTS,
        )
        if at_ms is not None or not detail.get("recorded"):
            return detail
        records = mouse_records(detail)
        page = page_lines(mouse_record_lines(records), first_line, clamp_page_chars(max_chars))
        shown = records[page.first_line - 1 : page.last_line]
        detail["action_count"] = len(detail["actions"])
        detail["rest_point_count"] = len(detail["rest_points"])
        detail.update(_page_fields(page, "mouse_lines", "video_story_mouse"))
        detail["actions"] = [r for r in shown if r.get("kind") != "rest"]
        detail["rest_points"] = [r for r in shown if r.get("kind") == "rest"]
        return detail

    return _guard(run)


def tool_list_frames(
    session_id: str,
    at_ms: int,
    screen: str = "cursor",
    window_ms: int = 0,
    config_file: Path | None = None,
    include_drawings: bool = True,
) -> dict[str, Any]:
    """Frames matching a moment of the timeline (latest frame at or before ``at_ms``)."""
    return _guard(
        lambda: frames_at(
            _paths(_load(config_file), session_id),
            at_ms,
            screen=screen,
            window_ms=window_ms,
            include_drawings=include_drawings,
        )
    )


def _frame_from_path(paths: SessionPaths, path: str) -> FrameRef:
    """A frame referenced by an absolute path; it must lie inside the session's frames."""
    frames_dir = paths.frames_dir(paths.current_dir).resolve()
    candidate = Path(path).expanduser().resolve()
    if not candidate.is_relative_to(frames_dir):
        raise SessionError(
            "FRAME_OUTSIDE_SESSION",
            f"{candidate} is not inside the frames directory {frames_dir}",
        )
    parsed = parse_frame_filename(candidate.name)
    if parsed is None or parsed.is_sidecar or candidate.parent != frames_dir:
        raise SessionError("NOT_A_FRAME", f"{candidate} is not a frame image")
    if not candidate.is_file():
        raise SessionError("NOT_FOUND", f"frame not found: {candidate}")
    return FrameRef(
        screen_id=parsed.screen_id,
        time_ms=parsed.time_ms,
        image=candidate,
        sidecar=paths.frames_info_dir(paths.current_dir) / f"{parsed.base}.json",
    )


def _frame_at(paths: SessionPaths, at_ms: int, screen: str) -> FrameRef:
    if screen == "all":
        raise SessionError("USAGE", "get_frame returns one image: use 'cursor' or a screen id")
    found = frames_at(paths, at_ms, screen=screen)
    if not found["frames"]:
        raise SessionError("NO_FRAMES", f"no frame for screen {screen!r} at {at_ms} ms")
    ref = found["frames"][0]
    if ref["image"] is None:
        raise SessionError(
            "ARCHIVED",
            "this recording is archived: the frame images are gone; "
            "see `archive` in the result of describe or timeline",
        )
    return FrameRef(
        screen_id=ref["screen"],
        time_ms=ref["time_ms"],
        image=Path(ref["image"]),
        sidecar=Path(ref["sidecar"]),
    )


def _encode_frame(image_path: Path, max_width: int) -> tuple[bytes, str, int, int, int, int]:
    """Load, downscale to ``max_width`` and re-encode a frame.

    Returns ``(data, format, original_width, original_height, width, height)``.
    """
    from PIL import Image as PilImage

    with PilImage.open(image_path) as img:
        source_format = (img.format or "PNG").upper()
        original_width, original_height = img.size
        picture = img
        if original_width > max_width:
            height = max(1, round(original_height * max_width / original_width))
            picture = img.resize((max_width, height), PilImage.Resampling.LANCZOS)
        buffer = io.BytesIO()
        if source_format == "JPEG":
            picture.convert("RGB").save(buffer, format="JPEG", quality=85)
            out_format = "jpeg"
        else:
            picture.save(buffer, format="PNG")
            out_format = "png"
        width, height = picture.size
    return buffer.getvalue(), out_format, original_width, original_height, width, height


def tool_get_frame(
    session_id: str,
    path: str | None = None,
    at_ms: int | None = None,
    screen: str = "cursor",
    max_width: int = FRAME_DEFAULT_MAX_WIDTH,
    config_file: Path | None = None,
) -> list[Any] | dict[str, Any]:
    """One screenshot as image content plus a dict with its metadata.

    Returns ``[Image, metadata]`` on success (the server turns it into an image part and a
    text part) or an error dict. ``metadata["scale"]`` is ``original_width / width``: divide
    the cursor coordinates by it to locate the cursor in the returned image.
    """
    try:
        paths = _paths(_load(config_file), session_id)
        if path is not None:
            ref = _frame_from_path(paths, path)
        elif at_ms is not None:
            ref = _frame_at(paths, at_ms, screen)
        else:
            raise SessionError("USAGE", "pass either path or at_ms (with an optional screen)")
        width_limit = max(FRAME_MIN_WIDTH, int(max_width))
        data, fmt, ow, oh, w, h = _encode_frame(ref.image, width_limit)
    except SessionError as exc:
        return _error(exc.code, str(exc))
    except ConfigError as exc:
        return _error("CONFIG_INVALID", str(exc))
    except (OSError, ValueError) as exc:
        return _error("IMAGE_ERROR", f"cannot read frame: {exc}")
    info = ref.to_dict()
    scale = ow / w if w else 1.0
    cursor = info["cursor"]
    metadata = {
        "ok": True,
        "session_id": paths.session_id,
        "screen": info["screen"],
        "time_ms": info["time_ms"],
        "image": info["image"],
        "cursor": cursor,
        "cursor_in_image": (
            {"x": round(cursor["x"] / scale), "y": round(cursor["y"] / scale)}
            if cursor and cursor.get("screen") == info["screen"]
            else None
        ),
        "format": fmt,
        "original_width": ow,
        "original_height": oh,
        "width": w,
        "height": h,
        "scale": scale,
        "time_unit": "ms since recording start",
    }
    return [Image(data=data, format=fmt), metadata]


def tool_config(
    action: str = "get",
    key: str | None = None,
    value: str | None = None,
    config_file: Path | None = None,
) -> dict[str, Any]:
    """Mirror of ``video-story config get|set|path``."""
    path = config_file or config_path()

    def run() -> dict[str, Any]:
        if action == "path":
            return config_location(config_file, "server --config")
        if action == "get":
            data = load_config(path).to_json_dict()
            if key:
                if key not in data:
                    raise SessionError("UNKNOWN_KEY", f"unknown configuration key {key!r}")
                return {"key": key, "value": data[key]}
            return {"path": str(path), "config": data}
        if action == "set":
            if not key:
                raise SessionError("USAGE", "config set requires key and value")
            if key not in Config.__dataclass_fields__:
                raise SessionError("UNKNOWN_KEY", f"unknown configuration key {key!r}")
            cfg = update_config(path, **{key: _coerce_config_value(value)})
            return {"path": str(path), "config": cfg.to_json_dict()}
        raise SessionError("USAGE", f"unknown config action {action!r}; use get, set or path")

    return _guard(run)


def tool_export(
    session: str,
    output: str | None = None,
    screen: str = "screen1",
    start_ms: int = 0,
    end_ms: int | None = None,
    draw_cursor: bool = True,
    write_srt: bool = True,
    config_file: Path | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Render one screen of a recording to MP4 (+ SRT); mirror of ``video-story export``.

    A relative ``output`` is resolved against the recording folder (never against the
    server's working directory). File-system failures (an output path under an
    existing file, a read-only folder, a full disk) come back as ``OUTPUT_ERROR``.
    """
    from video_story.export import ExportError, ExportRequest
    from video_story.export.ffmpeg_export import export_recording
    from video_story.host.cli import default_export_output

    def run() -> dict[str, Any]:
        paths = _paths(_load(config_file), session)
        if output:
            target = Path(output).expanduser()
            if not target.is_absolute():
                target = paths.recording_folder / target
        else:
            target = default_export_output(paths, screen)
        request = ExportRequest(
            paths=paths,
            output=target,
            screen_id=screen,
            start_ms=int(start_ms),
            end_ms=None if end_ms is None else int(end_ms),
            draw_cursor=bool(draw_cursor),
            write_srt=bool(write_srt),
        )
        try:
            return export_recording(request, _load(config_file), progress=progress).to_dict()
        except ExportError as exc:
            raise SessionError(exc.code, str(exc)) from exc
        except OSError as exc:  # anything the export engine did not map itself
            raise SessionError("OUTPUT_ERROR", f"cannot write {target}: {exc}") from exc

    return _guard(run)


def tool_edit(session: str | None = None, config_file: Path | None = None) -> dict[str, Any]:
    """Open the Video Story Editor in its own process; mirror of ``video-story edit --detach``."""
    from video_story.host.editor_launcher import launch_editor

    return _guard(lambda: launch_editor(_load(config_file), session, config_file))


def tool_wait_editor(editor_session: str, timeout_s: float = DEFAULT_WAIT_S) -> dict[str, Any]:
    """Wait for the editor's Process / close event; mirror of ``wait --editor``."""
    from video_story.host.editor_session import wait_for_editor

    path = Path(editor_session).expanduser()
    if not path.exists():
        return _error("UNKNOWN_EDITOR_SESSION", f"no editor session file {path}")
    result = _guard(lambda: wait_for_editor(path, clamp_timeout(timeout_s)))
    if result.get("timed_out"):
        result["hint"] = "call video_story_wait_editor again until the event is process or closed"
    return result


def tool_doctor(config_file: Path | None = None) -> dict[str, Any]:
    """Environment diagnostics (ffmpeg, whisper model, screens, audio)."""
    try:
        from video_story.host.doctor import run_doctor
    except ImportError as exc:
        return _error("DOCTOR_UNAVAILABLE", f"doctor module not available: {exc}")

    def run() -> dict[str, Any]:
        return run_doctor(_load(config_file))

    try:
        return _guard(run)
    except Exception as exc:  # diagnostics must never take the server down
        log.exception("doctor failed")
        return _error("DOCTOR_FAILED", f"{type(exc).__name__}: {exc}")


# Server -----------------------------------------------------------------------


def create_server(config_file: Path | None = None) -> MCPServer:
    """Build the MCP server with every tool bound to ``config_file`` (not started)."""
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)

    async def video_story_start() -> dict[str, Any]:
        """Open the Video Story control window for the user. Returns the session_id to
        pass to video_story_wait; the user records, stops and presses Process in the editor."""
        return await anyio.to_thread.run_sync(lambda: tool_start(config_file))

    async def video_story_wait(
        session_id: str, timeout_s: float = DEFAULT_WAIT_S, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Wait until the session is final (processed, cancelled or failed) or
        timeout_s (positive, at most 240; default 45) elapses. When "timed_out" is true,
        call it again. Status
        "editing" means the user is cropping the recording in the editor: keep waiting.
        Only "processed" (the user pressed Process) is a request to act.
        A processed session carries the paths of manifest, transcript and frames,
        "crop" {start_ms, end_ms}: act only on that part of the recording, and
        "instructions": the user's master instructions for the whole recording (read
        them first; "" when none)."""
        try:
            timeout = clamp_timeout(timeout_s)
        except HostError as exc:
            return exc.to_dict()

        async def progress(elapsed: float, message: str) -> None:
            if ctx is not None:
                await ctx.report_progress(elapsed, timeout, message)

        try:
            config = _load(config_file)
            paths = _paths(config, session_id)
        except SessionError as exc:
            return _error(exc.code, str(exc))
        except ConfigError as exc:
            return _error("CONFIG_INVALID", str(exc))
        try:
            return _ok(await wait_with_progress(paths, timeout, progress))
        except SessionError as exc:
            return _error(exc.code, str(exc))
        except RuntimeError as exc:
            return _error("SESSION_ERROR", str(exc))

    def video_story_status(session_id: str) -> dict[str, Any]:
        """Current state of a session without waiting."""
        return tool_status(session_id, config_file)

    def video_story_describe(session_id: str) -> dict[str, Any]:
        """Paths, manifest content (screens, duration, capture interval, keypoints), the
        stored crop {start_ms, end_ms} and "instructions" (the user's master
        instructions for the whole recording, "" when none) of a processed recording.
        transcript_in_range (the segments inside the crop) is included only when it is
        short (transcript_in_range_inline); otherwise transcript_in_range_note says
        which paged tool to call."""
        return tool_describe(session_id, config_file)

    def video_story_get_transcript(
        session_id: str,
        format: str = "text",
        first_line: int = 1,
        max_chars: int = PAGE_DEFAULT_CHARS,
    ) -> dict[str, Any]:
        """The transcript of the whole recording: format "text" returns timestamped
        lines ("[lower track]" marks a subtitle the user moved to the second track),
        "json" returns segments with start_ms, end_ms, text and track (1 for the lower
        track, absent for the upper one). Paged: at most
        max_chars characters from line (segment) first_line; transcript_lines is the
        total, next_line the first_line of the next call (null: complete)."""
        return tool_get_transcript(session_id, format, config_file, first_line, max_chars)

    def video_story_timeline(
        session_id: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        mouse: str = "auto",
        first_line: int = 1,
        max_chars: int = PAGE_DEFAULT_CHARS,
    ) -> dict[str, Any]:
        """Read this first after the instructions: the whole cropped range as one text
        timeline (no images). Lines: SAY (speech with the cursor screen and position),
        DRAW #id (a finished shape with its geometry, press/release times and the frame
        times before and after it), REMOVED, CLIPBOARD (copied text), CURSOR (moved to
        another screen), ZOOM, KEYPOINT and, when the mouse was recorded, CLICK,
        DOUBLE-CLICK, DRAG, SCROLL, ZOOM-SCROLL, GESTURE with the element under the mouse
        and its content (and "over ..." in SAY: what the pointer rested on). Times are
        ms; pass them to video_story_get_frame only where the words and coordinates are
        not enough. start_ms / end_ms override the crop; mouse is auto, full, summary
        (scrolls merged per 10 s) or off. Paged: "timeline" holds whole lines from
        first_line (1-based), at most max_chars characters (1000..100000, default
        20000); timeline_lines is the total. While next_line is not null the timeline
        is incomplete: call again with first_line = next_line and the same other
        arguments until it is null, before you act (the legend comes with the first
        page only)."""
        return tool_timeline(
            session_id, start_ms, end_ms, config_file, mouse, first_line, max_chars
        )

    def video_story_mouse(
        session_id: str,
        at_ms: int | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        first_line: int = 1,
        max_chars: int = PAGE_DEFAULT_CHARS,
    ) -> dict[str, Any]:
        """Everything recorded about the mouse actions the timeline lists: the element
        (role, name, bounds in pixels of the screen image, ancestors, window), its whole
        content up to 4096 characters, the selection, drag paths (at most 200 points;
        path_points is the real count). at_ms (the time a timeline line starts with)
        returns that one action - prefer it. Without it the actions and rest points of
        the cropped range (start_ms / end_ms override the crop) come in time order page
        by page: first_line / max_chars / next_line work as in video_story_timeline,
        action_count and rest_point_count are the totals."""
        return tool_mouse(session_id, at_ms, start_ms, end_ms, config_file, first_line, max_chars)

    def video_story_list_frames(
        session_id: str,
        at_ms: int,
        screen: str = "cursor",
        window_ms: int = 0,
        include_drawings: bool = True,
    ) -> dict[str, Any]:
        """Screenshots taken at at_ms (latest frame at or before it). screen: "cursor"
        (the screen the cursor was on), "all" or a screen id like "screen2"; window_ms > 0
        also lists the frames within at_ms +/- window_ms (at most 24, the nearest ones).
        Returns paths and cursor positions, not images: use video_story_get_frame to see
        one. include_drawings false lists only the drawing ids per frame
        (video_story_timeline has the shapes). "truncated" true: the result was
        shortened to stay small, truncated_note says how."""
        return tool_list_frames(session_id, at_ms, screen, window_ms, config_file, include_drawings)

    def video_story_get_frame(
        session_id: str,
        path: str | None = None,
        at_ms: int | None = None,
        screen: str = "cursor",
        max_width: int = FRAME_DEFAULT_MAX_WIDTH,
    ) -> list[Any] | dict[str, Any]:
        """One screenshot as an image plus its metadata (time_ms, screen, cursor, scale).
        Select it by the absolute path from video_story_list_frames or by at_ms and screen.
        Images wider than max_width are downscaled; divide cursor coordinates by "scale"
        (or use "cursor_in_image") to find the cursor in the returned image."""
        return tool_get_frame(session_id, path, at_ms, screen, max_width, config_file)

    def video_story_config(
        action: str = "get", key: str | None = None, value: str | None = None
    ) -> dict[str, Any]:
        """Read or change config.json: action "get" (optionally one key), "set" (key and
        value as a string; "true"/"false"/numbers/"null" are converted) or "path"."""
        return tool_config(action, key, value, config_file)

    async def video_story_export(
        session_id: str,
        output: str | None = None,
        screen: str = "screen1",
        start_ms: int = 0,
        end_ms: int | None = None,
        draw_cursor: bool = True,
        write_srt: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Render a processed recording to an MP4 video (H.264 + AAC, constant 24 fps,
        the screen's resolution) with ffmpeg, plus an SRT subtitle file next to it made
        from the transcript. screen selects the screen ("screen1" by default); start_ms
        and end_ms crop the recording timeline (end_ms omitted = end of the recording);
        output defaults to <recording_folder>/<session_id>_<screen>.mp4, a relative
        output is resolved against the recording folder and ".mp4" is appended when
        the name has another extension. draw_cursor draws the mouse pointer at its
        recorded positions. Takes a while for long recordings; progress is reported
        while it runs. Errors come back as {"ok": false, "code": ...}, e.g.
        OUTPUT_ERROR when the output path cannot be written."""

        async def report(fraction: float, text: str) -> None:
            if ctx is not None:
                await ctx.report_progress(fraction * 100, 100, text)

        def progress(fraction: float, text: str) -> None:
            try:
                anyio.from_thread.run(report, fraction, text)
            except Exception:  # a client that rejects notifications must not end the export
                log.debug("progress notification failed", exc_info=True)

        return await anyio.to_thread.run_sync(
            lambda: tool_export(
                session_id,
                output,
                screen,
                start_ms,
                end_ms,
                draw_cursor,
                write_srt,
                config_file,
                progress=progress,
            )
        )

    async def video_story_edit(session_id: str | None = None) -> dict[str, Any]:
        """Open the Video Story Editor window for the user: a list of the stored
        recordings, a preview with subtitles, a timeline per screen with In/Out crop
        handles, a Render button (MP4 + SRT) and a green Process button. session_id
        preselects a recording (default: the newest). Returns at once with
        editor_session: then call video_story_wait_editor with it until the user
        presses Process (event "process" with session_id, crop {start_ms, end_ms}: act
        only on that range of the recording, and "instructions": the user's master
        instructions for the whole recording) or closes the window (event "closed":
        do nothing). An already open editor is reused (attached). NO_RECORDINGS:
        nothing to edit yet."""
        return await anyio.to_thread.run_sync(lambda: tool_edit(session_id, config_file))

    async def video_story_wait_editor(
        editor_session: str, timeout_s: float = DEFAULT_WAIT_S, ctx: Context | None = None
    ) -> dict[str, Any]:
        """Wait until the editor opened by video_story_edit reports event "process"
        (the user pressed Process: session_id and crop {start_ms, end_ms} tell which
        recording and which part to act on, "instructions" how) or "closed" (nothing to
        do), or until
        timeout_s (positive, at most 240; default 45) elapses ("timed_out": call again)."""
        return await anyio.to_thread.run_sync(lambda: tool_wait_editor(editor_session, timeout_s))

    def video_story_doctor() -> dict[str, Any]:
        """Check the environment: Python, platform support, FFmpeg, whisper model, screens
        and audio input."""
        return tool_doctor(config_file)

    server.add_tool(video_story_start)
    server.add_tool(video_story_wait)
    server.add_tool(video_story_status)
    server.add_tool(video_story_describe)
    server.add_tool(video_story_get_transcript)
    server.add_tool(video_story_timeline)
    server.add_tool(video_story_mouse)
    server.add_tool(video_story_list_frames)
    server.add_tool(video_story_get_frame, structured_output=False)
    server.add_tool(video_story_config)
    server.add_tool(video_story_export)
    server.add_tool(video_story_edit)
    server.add_tool(video_story_wait_editor)
    server.add_tool(video_story_doctor)
    return server


def serve(
    transport: str = "stdio",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    config_file: Path | None = None,
) -> None:
    """Run the MCP server until the client disconnects (stdio) or the process is killed.

    ``transport``: ``stdio`` for Claude Desktop / Codex, ``streamable-http`` (alias
    ``http``) for ChatGPT, served at ``http://host:port/mcp``. With stdio nothing may be
    written to stdout by anyone else; logging goes to stderr.
    """
    if transport == "http":
        transport = "streamable-http"
    if transport not in TRANSPORTS:
        raise ValueError(f"unknown transport {transport!r}; use {' or '.join(TRANSPORTS)}")
    server = create_server(config_file)
    if transport == "stdio":
        log.info("MCP server %s on stdio", SERVER_NAME)
        server.run(transport="stdio")
    else:
        log.info("MCP server %s on http://%s:%s/mcp", SERVER_NAME, host, port)
        server.run(transport="streamable-http", host=host, port=port)


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "INSTRUCTIONS",
    "SERVER_NAME",
    "TRANSPORTS",
    "clamp_timeout",
    "create_server",
    "serve",
    "tool_config",
    "tool_describe",
    "tool_doctor",
    "tool_edit",
    "tool_wait_editor",
    "tool_export",
    "tool_get_frame",
    "tool_get_transcript",
    "tool_list_frames",
    "tool_mouse",
    "tool_start",
    "tool_status",
    "tool_timeline",
    "tool_wait",
    "wait_with_progress",
]
