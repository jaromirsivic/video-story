"""Command line interface used by agents, skills and people.

Every command prints exactly one JSON object on stdout. Errors are printed as a JSON
object on stderr (``{"ok": false, "code": ..., "message": ...}``) with a non-zero exit
code. Logging goes to stderr and, while a session is active, to ``recording.log``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from video_story import __version__
from video_story.core.config import (
    Config,
    ConfigError,
    config_path,
    load_config,
    save_config,
    update_config,
)
from video_story.core.session import SessionPaths, new_session_id

log = logging.getLogger("video_story")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNSUPPORTED = 3


class CliError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def emit(payload: dict[str, Any]) -> None:
    payload.setdefault("ok", True)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def emit_error(code: str, message: str) -> None:
    sys.stderr.write(
        json.dumps({"ok": False, "code": code, "message": message}, ensure_ascii=False) + "\n"
    )
    sys.stderr.flush()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def attach_file_log(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    return handler


def _config_file(args: argparse.Namespace) -> Path | None:
    return Path(args.config) if args.config else None


def _load(args: argparse.Namespace) -> Config:
    return load_config(_config_file(args))


def _session_paths(args: argparse.Namespace, cfg: Config) -> SessionPaths:
    from video_story.host.runner import SessionError, active_session, resolve_session

    if args.session:
        try:
            return resolve_session(cfg, args.session)
        except SessionError as exc:
            raise CliError(exc.code, str(exc)) from exc
    running = active_session(cfg, include_editing=True)
    if running is not None:
        return running[0]
    raise CliError("NO_SESSION", "no active session; pass --session ID", EXIT_USAGE)


# Commands --------------------------------------------------------------------


def cmd_config(args: argparse.Namespace) -> dict[str, Any]:
    path = _config_file(args) or config_path()
    if args.action == "path":
        return {"path": str(path), "exists": path.exists()}
    if args.action == "get":
        cfg = load_config(path)
        data = cfg.to_json_dict()
        if args.key:
            if args.key not in data:
                raise CliError("UNKNOWN_KEY", f"unknown configuration key {args.key!r}")
            return {"key": args.key, "value": data[args.key]}
        return {"path": str(path), "config": data}
    if args.action == "set":
        if not args.key:
            raise CliError("USAGE", "config set requires KEY and VALUE", EXIT_USAGE)
        value: Any = args.value
        if value is not None:
            lowered = value.lower()
            if lowered in {"null", "none", ""}:
                value = None
            elif lowered in {"true", "false"}:
                value = lowered == "true"
            elif lowered.lstrip("-").isdigit():
                value = int(lowered)
        if args.key not in Config.__dataclass_fields__:
            raise CliError("UNKNOWN_KEY", f"unknown configuration key {args.key!r}")
        cfg = update_config(path, **{args.key: value})
        return {"path": str(path), "config": cfg.to_json_dict()}
    if args.action == "reset":
        save_config(Config(), path)
        return {"path": str(path), "config": load_config(path).to_json_dict()}
    raise CliError("USAGE", f"unknown config action {args.action!r}", EXIT_USAGE)


def cmd_devices(_args: argparse.Namespace) -> dict[str, Any]:
    from video_story.capture.audio_recorder import AudioError, list_input_devices

    try:
        devices = list_input_devices()
    except AudioError as exc:
        raise CliError("AUDIO_ERROR", str(exc)) from exc
    return {"devices": [d.to_dict() for d in devices]}


def cmd_screens(_args: argparse.Namespace) -> dict[str, Any]:
    from video_story.core.screens import assign_screen_ids
    from video_story.platform import get_platform

    platform = get_platform()
    status = platform.support_status()
    if not status.supported:
        raise CliError("UNSUPPORTED", status.reason, EXIT_UNSUPPORTED)
    platform.prepare_process()
    screens = assign_screen_ids(platform.list_screens())
    cursor = platform.cursor_position()
    return {
        "platform": platform.name,
        "screens": [s.to_manifest() for s in screens],
        "cursor_global": {"x": cursor[0], "y": cursor[1]} if cursor else None,
    }


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    from video_story.host.doctor import run_doctor

    return run_doctor(_load(args), fix=args.fix)


def cmd_setup(args: argparse.Namespace) -> dict[str, Any]:
    from video_story.host.doctor import run_doctor

    return run_doctor(_load(args), fix=True, download_model=True)


def cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    """Open the control window. Returns at once with --detach, else waits for the end."""
    from video_story.host.runner import (
        SessionError,
        launch_control_window,
        wait_for_session,
    )

    cfg = _load(args)
    try:
        info = launch_control_window(cfg, _config_file(args))
    except SessionError as exc:
        raise CliError(exc.code, str(exc)) from exc
    result = info.to_dict()
    if args.detach:
        result["next"] = f"video-story wait --session {info.session_id} --timeout 240"
        return result
    paths = SessionPaths(cfg.recording_folder, info.session_id)
    result.update(wait_for_session(paths, timeout_s=args.timeout if args.timeout else 10**9))
    return result


def cmd_ui(args: argparse.Namespace) -> dict[str, Any]:
    """Internal: the GUI process started by ``run``."""
    from video_story.ui.app import run_ui

    code = run_ui(args.session, _config_file(args))
    return {"session_id": args.session, "exit_code": code}


def cmd_wait(args: argparse.Namespace) -> dict[str, Any]:
    from video_story.host.runner import SessionError, wait_for_session

    if getattr(args, "editor", None):
        from video_story.host.editor_session import wait_for_editor

        path = Path(args.editor).expanduser()
        if not path.exists():
            raise CliError("UNKNOWN_EDITOR_SESSION", f"no editor session file {path}")
        return wait_for_editor(path, timeout_s=args.timeout)
    cfg = _load(args)
    paths = _session_paths(args, cfg)
    try:
        return wait_for_session(paths, timeout_s=args.timeout)
    except SessionError as exc:
        raise CliError(exc.code, str(exc)) from exc


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    from video_story.host.runner import SessionError, session_status

    cfg = _load(args)
    paths = _session_paths(args, cfg)
    try:
        return session_status(paths)
    except SessionError as exc:
        raise CliError(exc.code, str(exc)) from exc


def cmd_sessions(args: argparse.Namespace) -> dict[str, Any]:
    from video_story.core.session import list_sessions
    from video_story.host.runner import session_status

    cfg = _load(args)
    items = []
    for paths in list_sessions(cfg.recording_folder)[: args.limit]:
        try:
            status = session_status(paths)
        except Exception as exc:
            status = {"session_id": paths.session_id, "status": "unknown", "message": str(exc)}
        items.append(status)
    return {"recording_folder": str(cfg.recording_folder), "sessions": items}


def cmd_describe(args: argparse.Namespace) -> dict[str, Any]:
    from video_story.host.runner import describe_recording

    cfg = _load(args)
    return describe_recording(_session_paths(args, cfg))


def cmd_frames(args: argparse.Namespace) -> dict[str, Any]:
    from video_story.host.runner import SessionError, frames_at

    cfg = _load(args)
    paths = _session_paths(args, cfg)
    try:
        return frames_at(
            paths,
            args.at,
            screen=args.screen,
            window_ms=args.window,
            include_drawings=not args.no_drawings,
        )
    except SessionError as exc:
        raise CliError(exc.code, str(exc)) from exc


def cmd_timeline(args: argparse.Namespace) -> dict[str, Any]:
    """Speech and screen events of the cropped range as one compact text timeline."""
    from video_story.host.events import build_timeline
    from video_story.host.runner import SessionError

    cfg = _load(args)
    paths = _session_paths(args, cfg)
    start_ms, end_ms = args.start_ms, args.end_ms
    if args.all:
        start_ms, end_ms = 0, 10**12
    try:
        return build_timeline(paths, start_ms, end_ms)
    except SessionError as exc:
        raise CliError(exc.code, str(exc)) from exc


def _require_audio(paths: Any, base: Path) -> None:
    """A recording without any audio track (microphone or computer) cannot be transcribed."""
    from video_story.core.audio_mix import has_audio

    if not has_audio(paths, base):
        raise CliError("NO_AUDIO", f"no audio file {paths.audio_path(base)}")


def cmd_transcribe(args: argparse.Namespace) -> dict[str, Any]:
    """Re-run transcription for an existing recording directory."""
    from video_story.core.manifest import read_manifest, write_manifest
    from video_story.core.session import now_iso
    from video_story.host.runner import SessionError, resolve_session
    from video_story.transcribe.pipeline import transcribe_recording

    cfg = _load(args)
    try:
        paths = resolve_session(cfg, args.directory)
    except SessionError as exc:
        raise CliError(exc.code, str(exc)) from exc
    base = paths.current_dir
    _require_audio(paths, base)

    def progress(fraction: float, text: str) -> None:
        log.info("%3d%% %s", int(fraction * 100), text)

    started = time.monotonic()
    result = transcribe_recording(paths, cfg, base_dir=base, progress=progress)
    manifest_path = paths.manifest_path(base)
    if manifest_path.exists():
        manifest = read_manifest(manifest_path)
        manifest.transcript = result.info
        manifest.finished_at = manifest.finished_at or now_iso()
        write_manifest(manifest_path, manifest)
    return {
        "recording_dir": str(base),
        "transcript_txt": str(paths.transcript_txt_path(base)),
        "transcript_json": str(paths.transcript_json_path(base)),
        "language": result.transcript.language,
        "segments": len(result.transcript.segments),
        "seconds": round(time.monotonic() - started, 1),
    }


def cmd_transcribe_stream(args: argparse.Namespace) -> dict[str, Any]:
    """Transcribe from ``--from-ms`` on, one JSON event per line (the editor's worker).

    Nothing is written to the recording: the editor that started this process stores
    the segments. The last line is the ``done`` event (``ok`` true); a failure is the
    usual error object on stderr with a non-zero exit code.
    """
    from video_story.host.runner import SessionError, resolve_session
    from video_story.transcribe.pipeline import stream_transcription

    cfg = _load(args)
    try:
        paths = resolve_session(cfg, args.directory)
    except SessionError as exc:
        raise CliError(exc.code, str(exc)) from exc
    base = paths.current_dir
    _require_audio(paths, base)

    def emit_event(event: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    return stream_transcription(
        paths, cfg, base_dir=base, start_ms=max(0, args.from_ms), emit=emit_event
    )


def cmd_archive_stream(args: argparse.Namespace) -> dict[str, Any]:
    """Archive a saved recording, one JSON event per line (the editor's archive worker).

    A recording whose transcript is not complete is transcribed first (the segments
    are stored as they come, so a stopped run resumes where it ended). Events:
    ``phase`` (``transcribing`` / ``archiving``), ``progress`` (``phase``, ``fraction``
    0..1 of that phase) and the returned ``done`` (``status``, ``leftovers``). With
    ``--stop-on-eof`` the run stops cleanly (FFmpeg cancelled, partial files removed)
    once stdin is closed: the editor closes it when it quits.
    """
    import threading

    from video_story.core.transcript import Segment, transcript_status
    from video_story.editor.recording import LoadedRecording
    from video_story.export.archive import ArchiveError, archive_recording
    from video_story.host.runner import SessionError, resolve_session
    from video_story.transcribe.pipeline import stream_transcription

    cfg = _load(args)
    try:
        paths = resolve_session(cfg, args.directory)
    except SessionError as exc:
        raise CliError(exc.code, str(exc)) from exc
    base = paths.final_dir
    stop = threading.Event()

    def emit_event(event: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    if args.stop_on_eof:

        def watch_stdin() -> None:
            try:
                while sys.stdin.read(1):
                    pass
            except (OSError, ValueError):
                pass
            stop.set()

        threading.Thread(target=watch_stdin, name="stdin-eof", daemon=True).start()

    if not transcript_status(paths.transcript_json_path(base)).complete:
        _require_audio(paths, base)
        emit_event({"event": "phase", "phase": "transcribing"})
        recording = LoadedRecording.load(paths)

        def on_transcription(event: dict[str, Any]) -> None:
            kind = event.get("event")
            if kind == "segment":
                segment = Segment.from_dict(event)
                recording.add_segments([segment], segment.end_ms)
            elif kind == "progress":
                total = max(1, recording.duration_ms)
                reached = recording.transcribed_ms
                fraction = max(float(event.get("fraction", 0.0)), reached / total)
                emit_event(
                    {"event": "progress", "phase": "transcribing", "fraction": min(1.0, fraction)}
                )

        done = stream_transcription(
            paths, cfg, base_dir=base, start_ms=recording.transcribed_ms, emit=on_transcription
        )
        recording.finish_transcript(done.get("language"), done.get("model"), done.get("backend"))
        if stop.is_set():
            raise CliError("CANCELLED", "archiving was stopped")

    emit_event({"event": "phase", "phase": "archiving"})

    def on_progress(fraction: float, _text: str) -> None:
        emit_event({"event": "progress", "phase": "archiving", "fraction": round(fraction, 4)})

    try:
        state = archive_recording(paths, cfg, progress=on_progress, cancel=stop.is_set)
    except ArchiveError as exc:
        raise CliError(exc.code, str(exc)) from exc
    return {
        "event": "done",
        "recording_dir": str(base),
        "status": str(state.status),
        "leftovers": state.leftovers,
    }


def default_export_output(paths: SessionPaths, screen: str) -> Path:
    """``<recording_folder>/<session_id>_<screen>.mp4`` when no output is given."""
    return paths.recording_folder / f"{paths.session_id}_{screen}.mp4"


def cmd_export(args: argparse.Namespace) -> dict[str, Any]:
    """Render a recording (one screen, optional range) to MP4 + SRT with ffmpeg."""
    from video_story.export import ExportError, ExportRequest
    from video_story.export.ffmpeg_export import export_recording
    from video_story.host.runner import SessionError, resolve_session

    cfg = _load(args)
    try:
        paths = resolve_session(cfg, args.session)
    except SessionError as exc:
        raise CliError(exc.code, str(exc)) from exc
    output = Path(args.output) if args.output else default_export_output(paths, args.screen)
    request = ExportRequest(
        paths=paths,
        output=output,
        screen_id=args.screen,
        start_ms=args.start_ms,
        end_ms=args.end_ms,
        draw_cursor=not args.no_cursor,
        write_srt=not args.no_srt,
    )

    def progress(fraction: float, text: str) -> None:
        log.info("%3d%% %s", int(fraction * 100), text)

    try:
        result = export_recording(request, cfg, progress=progress)
    except ExportError as exc:
        raise CliError(exc.code, str(exc)) from exc
    return result.to_dict()


def cmd_edit(args: argparse.Namespace) -> dict[str, Any]:
    """Open the Video Story Editor (``--detach``: in its own process, return at once)."""
    from video_story.host.editor_launcher import launch_editor
    from video_story.host.runner import SessionError

    cfg = _load(args)
    try:
        if args.detach:
            return launch_editor(cfg, args.session, _config_file(args), args.recording_session)
        from video_story.editor.app import run_editor

        code = run_editor(
            args.session,
            _config_file(args),
            editor_session=Path(args.editor_session) if args.editor_session else None,
            recording_session=args.recording_session,
        )
    except SessionError as exc:
        raise CliError(exc.code, str(exc)) from exc
    return {"session_id": args.session, "exit_code": code}


def cmd_record(args: argparse.Namespace) -> dict[str, Any]:
    """Headless recording for development and integration tests (no window)."""
    from video_story.capture.recorder import Recorder
    from video_story.core.manifest import write_manifest
    from video_story.platform import get_platform

    cfg = _load(args)
    if args.interval:
        cfg = cfg.with_values(capture_interval_ms=args.interval)
    if args.folder:
        cfg = cfg.with_values(recording_folder=str(args.folder))
    platform = get_platform()
    paths = SessionPaths(cfg.recording_folder, new_session_id(cfg.recording_folder))
    paths.create_work_dirs()
    handler = attach_file_log(paths.log_path(paths.work_dir))
    try:
        recorder = Recorder(cfg, platform, paths, record_audio=not args.no_audio)
        recorder.start()
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline and recorder.running:
            time.sleep(0.05)
        summary = recorder.stop()
        manifest = summary.to_manifest(cfg, platform.name)
        write_manifest(paths.manifest_path(paths.work_dir), manifest)
    finally:
        # The log file must be closed before the work directory is renamed (Windows).
        logging.getLogger().removeHandler(handler)
        handler.close()
    final_dir = paths.finalize() if not args.keep_work else paths.work_dir
    return {
        "session_id": paths.session_id,
        "recording_dir": str(final_dir),
        "recorded_duration_ms": summary.recorded_duration_ms,
        "frames": summary.frame_count,
        "dropped_ticks": summary.dropped_ticks,
        "screens": [s.id for s in summary.screens],
        "audio": summary.audio.__dict__ if summary.audio else None,
        "auto_stopped": summary.auto_stopped,
        "error": summary.error,
    }


def cmd_mcp(args: argparse.Namespace) -> dict[str, Any]:
    """Run the MCP server (stdio for Claude Desktop/Codex, streamable-http for ChatGPT)."""
    from video_story.host.mcp_server import serve

    serve(
        transport=args.transport,
        host=args.host,
        port=args.port,
        config_file=_config_file(args),
    )
    return {"transport": args.transport, "stopped": True}


def cmd_version(_args: argparse.Namespace) -> dict[str, Any]:
    return {"version": __version__, "python": sys.version.split()[0], "platform": sys.platform}


# Parser ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-story",
        description="Video Story: record narrated screen sessions for AI agents.",
    )
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING or ERROR")
    parser.add_argument("--config", help="path to config.json (default: plugin directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("version", help="show the plugin version")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("run", help="open the control window (agents: use --detach, then wait)")
    p.add_argument("--detach", action="store_true", help="return immediately with the session id")
    p.add_argument(
        "--timeout", type=float, help="without --detach: give up waiting after N seconds"
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("wait", help="wait until a session is final or the timeout elapses")
    p.add_argument("--session", help="session id or recording directory (default: active)")
    p.add_argument(
        "--editor", help="editor session file from `edit --detach`: wait for Process / close"
    )
    p.add_argument("--timeout", type=float, default=240.0, help="seconds (default 240)")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("status", help="current state of a session")
    p.add_argument("--session")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("sessions", help="list recent sessions in the recording folder")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("describe", help="paths, manifest and transcript of a recording")
    p.add_argument("--session")
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("frames", help="frames matching a moment of the timeline")
    p.add_argument("--session")
    p.add_argument("--at", type=int, required=True, help="milliseconds since the recording start")
    p.add_argument(
        "--screen",
        default="cursor",
        help="cursor (screen the cursor was on), all, or a screen id like screen2",
    )
    p.add_argument("--window", type=int, default=0, help="also list frames within +/- N ms")
    p.add_argument(
        "--no-drawings",
        action="store_true",
        help="leave out the drawings list of every frame (the timeline already has them)",
    )
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser(
        "timeline", help="speech, drawings, clipboard, cursor and keypoints as one text timeline"
    )
    p.add_argument("--session")
    p.add_argument("--start-ms", type=int, help="start of the range (default: the crop start)")
    p.add_argument("--end-ms", type=int, help="end of the range (default: the crop end)")
    p.add_argument("--all", action="store_true", help="the whole recording, ignoring the crop")
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("transcribe", help="(re)transcribe an existing recording directory")
    p.add_argument("directory", help="session id or recording directory")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("transcribe-stream", help=argparse.SUPPRESS)  # internal: editor worker
    p.add_argument("directory", help="session id or recording directory")
    p.add_argument("--from-ms", type=int, default=0, help="resume at this time")
    p.set_defaults(func=cmd_transcribe_stream)

    p = sub.add_parser("archive-stream", help=argparse.SUPPRESS)  # internal: editor worker
    p.add_argument("directory", help="session id or recording directory")
    p.add_argument("--stop-on-eof", action="store_true", help="stop once stdin is closed")
    p.set_defaults(func=cmd_archive_stream)

    p = sub.add_parser("export", help="render a recording to MP4 (+ SRT) with ffmpeg")
    p.add_argument("--session", required=True, help="session id or recording directory")
    p.add_argument("--screen", default="screen1", help="screen id to render (default screen1)")
    p.add_argument("--start-ms", type=int, default=0, help="start of the range (default 0)")
    p.add_argument("--end-ms", type=int, help="end of the range (default: end of recording)")
    p.add_argument("--output", help="MP4 path (default <recording_folder>/<session>_<screen>.mp4)")
    p.add_argument("--no-cursor", action="store_true", help="do not draw the mouse pointer")
    p.add_argument("--no-srt", action="store_true", help="do not write subtitles next to it")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("edit", help="open the Video Story Editor (agents: use --detach)")
    p.add_argument("--detach", action="store_true", help="start the editor and return at once")
    p.add_argument("--session", help="recording to select (default: the newest)")
    p.add_argument("--editor-session", help=argparse.SUPPRESS)  # internal: event file
    p.add_argument("--recording-session", help=argparse.SUPPRESS)  # internal: editing session
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("config", help="read or change config.json")
    p.add_argument("action", choices=["get", "set", "path", "reset"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("devices", help="list audio input devices")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("screens", help="list screens and the cursor position")
    p.set_defaults(func=cmd_screens)

    p = sub.add_parser("doctor", help="check the environment (ffmpeg, model, screens, audio)")
    p.add_argument("--fix", action="store_true", help="download missing FFmpeg automatically")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("setup", help="prepare the environment: FFmpeg and the whisper model")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("mcp", help="run the MCP server (stdio by default)")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("ui", help=argparse.SUPPRESS)
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("record", help="headless recording for development and tests")
    p.add_argument("--seconds", type=float, default=3.0)
    p.add_argument("--interval", type=int, help="capture interval in ms (overrides config)")
    p.add_argument("--folder", type=Path, help="recording folder (overrides config)")
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--keep-work", action="store_true", help="leave the recording in .work/")
    p.set_defaults(func=cmd_record)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    try:
        result = args.func(args)
    except CliError as exc:
        emit_error(exc.code, exc.message)
        return exc.exit_code
    except ConfigError as exc:
        emit_error("CONFIG_INVALID", str(exc))
        return EXIT_USAGE
    except KeyboardInterrupt:
        emit_error("INTERRUPTED", "interrupted")
        return 130
    except Exception as exc:  # last resort: still produce machine readable output
        log.exception("command failed")
        emit_error("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
        return EXIT_ERROR
    emit(result)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
