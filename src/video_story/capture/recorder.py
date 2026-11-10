"""Recording orchestration: screens + cursor + audio on one timeline.

The :class:`Recorder` owns a capture thread that ticks every ``capture_interval_ms``,
reads the cursor once, grabs every screen and hands the raw frames to a bounded
thread pool for encoding and writing. Audio is recorded by :class:`AudioRecorder`; what
the computer plays meanwhile by :class:`ComputerAudioRecorder` (Windows; when it is
switched on and can be opened - a failure there never stops the recording); the mouse by
:class:`InputRecorder` into ``input/mouse.jsonl`` (when Mouse is switched on and the
platform offers it - again never a reason to stop).
The recording never pauses; it ends on :meth:`stop`, :meth:`discard`, when the
auto-stop limit is reached or when less than :data:`MIN_FREE_BYTES` are left on the disk
of the recording folder (``stop_reason`` says which).

A screen that cannot be grabbed (the secure desktop of a UAC prompt or the lock screen,
a monitor that went away) does not end the recording: that screen simply has no frame
for the tick (``failed_grabs`` counts them) and the next tick tries again.

``manifest.json`` exists in the work directory from the first moment (screens, settings,
start time, the keypoints so far; ``finished_at`` null), so a recording whose process
died can still be finished (see :mod:`video_story.host.recovery`).
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from video_story import __version__
from video_story.core.config import Config
from video_story.core.cursor import CursorPosition, locate_cursor
from video_story.core.manifest import (
    COMPUTER_AUDIO_FILE,
    AudioInfo,
    FramesInfo,
    Manifest,
    MouseInfo,
    write_manifest,
)
from video_story.core.naming import frame_filename, sidecar_filename
from video_story.core.screens import Screen, assign_screen_ids
from video_story.core.session import SessionPaths, now_iso
from video_story.core.timeline import RecordingClock
from video_story.platform.base import Platform, PlatformError

from .audio_recorder import AudioError, AudioRecorder, AudioResult
from .computer_audio import ComputerAudioError, ComputerAudioRecorder
from .input_recorder import InputRecorder
from .screen_grabber import RawFrame, ScreenGrabber, encode_frame

log = logging.getLogger(__name__)

ENCODE_WORKERS = 4
MAX_PENDING_TICKS = 8  # bounded backlog; beyond this, ticks are dropped and counted
MIN_FREE_BYTES = 1024**3  # recording needs at least 1 GB of free disk space
DISK_CHECK_INTERVAL_NS = 2_000_000_000  # how often the free space is looked at
GRAB_REOPEN_AFTER = 10  # ticks without any frame before the screen grabber is reopened
GRAB_LOG_EVERY = 100  # failed grabs between two log lines
MOUSE_START_WAIT_S = 30  # Stop waits at most this long for a mouse source still starting


class RecorderEvent(StrEnum):
    STARTED = "started"
    AUTO_STOPPED = "auto_stopped"
    LOW_DISK = "low_disk"
    STOPPED = "stopped"
    ERROR = "error"


class StopReason(StrEnum):
    """Why a recording ended by itself (``None``: the user stopped it)."""

    AUTO_STOP = "auto_stop"
    LOW_DISK = "low_disk"
    CAPTURE_FAILED = "capture_failed"


class LowDiskSpaceError(RuntimeError):
    """Less than :data:`MIN_FREE_BYTES` free on the disk of the recording folder."""


def free_disk_bytes(path: Path) -> int | None:
    """Free bytes on the volume holding ``path``; ``None`` when that cannot be told."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def low_disk_message(folder: Path) -> str:
    return (
        f"At least 1 GB of free space is needed on the disk of the recording folder "
        f"({folder}); recording cannot continue without it."
    )


@dataclass
class RecordingSummary:
    session_id: str
    started_at: str
    finished_at: str
    recorded_duration_ms: int
    screens: list[Screen]
    frame_count: int
    dropped_ticks: int
    audio: AudioResult | None
    auto_stopped: bool
    error: str | None = None
    keypoints: list[int] = field(default_factory=list)
    failed_grabs: int = 0  # screen grabs that failed (that screen has no frame then)
    stop_reason: str | None = None  # a StopReason value, None when the user stopped
    computer_audio: AudioResult | None = None  # what the computer played (loopback)
    mouse: MouseInfo | None = None  # input/mouse.jsonl; None when not recorded

    def to_manifest(
        self, config: Config, platform_name: str, notes: list[str] | None = None
    ) -> Manifest:
        audio = AudioInfo()
        if self.audio:
            audio = AudioInfo(
                device=self.audio.device,
                sample_rate=self.audio.sample_rate,
                channels=self.audio.channels,
                duration_ms=self.audio.duration_ms,
            )
        computer = None
        if self.computer_audio:
            computer = AudioInfo(
                file=COMPUTER_AUDIO_FILE,
                device=self.computer_audio.device,
                sample_rate=self.computer_audio.sample_rate,
                channels=self.computer_audio.channels,
                duration_ms=self.computer_audio.duration_ms,
            )
        return Manifest(
            session_id=self.session_id,
            started_at=self.started_at,
            finished_at=self.finished_at,
            recorded_duration_ms=self.recorded_duration_ms,
            capture_interval_ms=config.capture_interval_ms,
            image_format=config.image_format,
            jpeg_quality=config.jpeg_quality if config.image_format == "jpeg" else None,
            screens=self.screens,
            audio=audio,
            computer_audio=computer,
            frames=FramesInfo(
                count=self.frame_count,
                dropped_ticks=self.dropped_ticks,
                failed_grabs=self.failed_grabs,
            ),
            auto_stopped=self.auto_stopped,
            stop_reason=self.stop_reason,
            auto_stop_minutes=config.auto_stop_minutes,
            platform=platform_name,
            plugin_version=__version__,
            notes=list(notes or []),
            keypoints=list(self.keypoints),
            mouse=self.mouse,
        )


DrawingsProvider = Callable[[], list[dict[str, Any]]]
DrawingNowProvider = Callable[[], dict[str, Any] | None]
ClipboardProvider = Callable[[], str | None]
KEYPOINT_MIN_GAP_MS = 3000  # a keypoint can follow the previous one after three seconds


@dataclass
class _Tick:
    time_ms: int
    cursor: CursorPosition | None
    frames: list[RawFrame] = field(default_factory=list)
    drawings: list[dict[str, Any]] = field(default_factory=list)
    drawing_now: dict[str, Any] | None = None
    zoom: list[dict[str, Any]] = field(default_factory=list)
    clipboard: str | None = None


class Recorder:
    """Drives one recording session. All public methods are safe to call from any thread."""

    def __init__(
        self,
        config: Config,
        platform: Platform,
        paths: SessionPaths,
        on_event: Callable[[RecorderEvent, str], None] | None = None,
        record_audio: bool = True,
    ) -> None:
        self.config = config
        self.platform = platform
        self.paths = paths
        self._on_event = on_event or (lambda _e, _m: None)
        self._record_audio = record_audio
        self.clock = RecordingClock()
        self.screens: list[Screen] = []
        self.started_at: str | None = None
        self._stop_event = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._pending = threading.Semaphore(MAX_PENDING_TICKS)
        self._audio: AudioRecorder | None = None
        self._audio_result: AudioResult | None = None
        self._computer: ComputerAudioRecorder | None = None
        self._computer_result: AudioResult | None = None
        self._input: InputRecorder | None = None
        self._mouse_info: MouseInfo | None = None
        self._mouse_thread: threading.Thread | None = None
        self._frame_count = 0
        self._dropped_ticks = 0
        self._failed_grabs = 0
        self._grab_failing = False
        self._auto_stopped = False
        self._stop_reason: StopReason | None = None
        self._error: str | None = None
        self._lock = threading.Lock()
        self._summary: RecordingSummary | None = None
        # Called from the capture thread at every tick; returns the finished shapes on
        # the screen right now (see ui.overlay.DrawingOverlay.drawings). Stored in the
        # sidecar of every frame of the tick as ``drawings``.
        self.drawings_provider: DrawingsProvider | None = None
        # The shape being drawn right now ({tool, color, screen, started_ms}, no
        # geometry; ui.overlay.DrawingOverlay.drawing_now), stored as ``drawing_now``
        # (null while nothing is being drawn).
        self.drawing_now_provider: DrawingNowProvider | None = None
        # Same for the zoom of the drawing canvas (ui.overlay.DrawingOverlay.zoom):
        # which screens show a magnified region, stored in the sidecars as ``zoom``.
        self.zoom_provider: DrawingsProvider | None = None
        # The text clipboard (ui.clipboard.ClipboardTracker.take): a text the user copied
        # since the previous tick (at most 65535 characters, "..." when cut), stored as
        # ``clipboard`` in the sidecars of this tick only; None (null) in every other tick.
        self.clipboard_provider: ClipboardProvider | None = None
        # Native handles of the drawing layer's windows (set by the control window on
        # the GUI thread, read by the mouse thread): a click there is "drawing", not a
        # click into Video Story's own window.
        self.drawing_windows: frozenset[int] = frozenset()
        self._keypoints: list[int] = []

    # Properties -------------------------------------------------------------

    @property
    def keypoints(self) -> list[int]:
        """Moments marked with Keypoint (ms of the recording timeline, ascending)."""
        with self._lock:
            return list(self._keypoints)

    def add_keypoint(self) -> int | None:
        """Mark the current moment; ``None`` when the previous keypoint is less than
        :data:`KEYPOINT_MIN_GAP_MS` ago (or the recording is not running)."""
        if not self.running:
            return None
        now = self.clock.elapsed_ms()
        with self._lock:
            if self._keypoints and now - self._keypoints[-1] < KEYPOINT_MIN_GAP_MS:
                return None
            self._keypoints.append(now)
        log.info("keypoint %d at %d ms", len(self._keypoints), now)
        self._write_provisional_manifest()  # a keypoint survives a crash of the process
        return now

    @property
    def elapsed_ms(self) -> int:
        return self.clock.elapsed_ms()

    @property
    def frame_count(self) -> int:
        with self._lock:
            return self._frame_count

    @property
    def dropped_ticks(self) -> int:
        with self._lock:
            return self._dropped_ticks

    @property
    def running(self) -> bool:
        return self._capture_thread is not None and self._capture_thread.is_alive()

    @property
    def auto_stopped(self) -> bool:
        return self._auto_stopped

    @property
    def failed_grabs(self) -> int:
        with self._lock:
            return self._failed_grabs

    @property
    def stop_reason(self) -> StopReason | None:
        """Why the recording ended by itself; ``None`` while it runs or after a manual stop."""
        return self._stop_reason

    @property
    def low_disk(self) -> bool:
        return self._stop_reason is StopReason.LOW_DISK

    @property
    def error(self) -> str | None:
        """The first write or capture error (frames or audio), ``None`` while all is fine."""
        if self._error:
            return self._error
        return self._audio.error if self._audio is not None else None

    # Manifest ---------------------------------------------------------------

    def _write_provisional_manifest(self) -> None:
        """``manifest.json`` of a recording in progress (``finished_at`` null).

        Holds what only this process knows - screens, capture settings, start time,
        keypoints - so that the recording can be finished even when the process dies.
        """
        if self.started_at is None:
            return
        audio = None
        if self._audio is not None:
            audio = AudioResult(
                device=self._audio.device.name,
                sample_rate=self._audio.sample_rate,
                channels=self._audio.channels,
                frames_written=0,
                dropped_blocks=0,
            )
        computer = None
        if self._computer is not None:
            computer = AudioResult(
                device=self._computer.device,
                sample_rate=self._computer.sample_rate,
                channels=self._computer.channels,
                frames_written=0,
                dropped_blocks=0,
            )
        summary = RecordingSummary(
            session_id=self.paths.session_id,
            started_at=self.started_at,
            finished_at="",
            recorded_duration_ms=0,
            screens=list(self.screens),
            frame_count=0,
            dropped_ticks=0,
            audio=audio,
            auto_stopped=False,
            keypoints=self.keypoints,
            computer_audio=computer,
            mouse=self._provisional_mouse(),
        )
        manifest = summary.to_manifest(self.config, self.platform.name)
        manifest.finished_at = None
        try:
            write_manifest(self.paths.manifest_path(self.paths.work_dir), manifest)
        except OSError as exc:  # never let bookkeeping stop a recording
            log.warning("provisional manifest not written: %s", exc)

    def _provisional_mouse(self) -> MouseInfo | None:
        with self._lock:
            recorder, info = self._input, self._mouse_info
        return recorder.info() if recorder is not None else info

    # Lifecycle --------------------------------------------------------------

    def start(self) -> None:
        if self._capture_thread is not None:
            raise RuntimeError("recorder already started")
        status = self.platform.support_status()
        if not status.supported:
            raise PlatformError(status.reason)
        free = free_disk_bytes(self.paths.work_dir)
        if free is not None and free < MIN_FREE_BYTES:
            raise LowDiskSpaceError(low_disk_message(self.paths.recording_folder))
        self.platform.prepare_process()
        self.screens = assign_screen_ids(self.platform.list_screens())
        self.paths.create_work_dirs()
        log.info("Screens: %s", [s.to_manifest() for s in self.screens])

        if self._record_audio:
            self._audio = AudioRecorder(
                self.paths.audio_path(self.paths.work_dir), self.config.audio_input
            )
        self._pool = ThreadPoolExecutor(max_workers=ENCODE_WORKERS, thread_name_prefix="encode")
        if self._audio is not None:
            try:
                self._audio.start()
            except AudioError:
                self._pool.shutdown(wait=False)
                self._pool = None
                raise
            self._start_computer_audio()
        # The timeline starts once the audio stream is live, so audio and frames align.
        self.started_at = now_iso()
        self.clock.start()
        self._start_mouse()
        self._write_provisional_manifest()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="capture", daemon=True
        )
        self._capture_thread.start()
        self._on_event(RecorderEvent.STARTED, "")

    def stop(self) -> RecordingSummary:
        """Stop recording and return the summary (idempotent)."""
        if self._summary is not None:
            return self._summary
        self._stop_event.set()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=60)
        self._stop_mouse()
        duration_ms = self.clock.stop() if self.clock.running or self.clock.elapsed_ns() else 0
        if self._audio is not None and self._audio_result is None:
            try:
                self._audio_result = self._audio.stop()
            except Exception as exc:  # pragma: no cover
                log.exception("audio stop failed")
                self._error = self._error or f"audio stop failed: {exc}"
        if self._audio_result is not None and self._audio_result.error:
            self._error = self._error or self._audio_result.error
        if self._computer is not None and self._computer_result is None:
            try:
                self._computer_result = self._computer.stop()
            except Exception:  # pragma: no cover - never let this track fail the recording
                log.exception("computer audio stop failed")
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None
        self._summary = RecordingSummary(
            session_id=self.paths.session_id,
            started_at=self.started_at or now_iso(),
            finished_at=now_iso(),
            recorded_duration_ms=duration_ms,
            screens=list(self.screens),
            frame_count=self.frame_count,
            dropped_ticks=self.dropped_ticks,
            audio=self._audio_result,
            auto_stopped=self._auto_stopped,
            error=self._error,
            keypoints=self.keypoints,
            failed_grabs=self.failed_grabs,
            stop_reason=str(self._stop_reason) if self._stop_reason else None,
            computer_audio=self._computer_result,
            mouse=self._mouse_info,
        )
        self._on_event(RecorderEvent.STOPPED, "")
        log.info(
            "Recording stopped: %d ms, %d frames, %d dropped ticks, auto_stopped=%s",
            duration_ms,
            self._summary.frame_count,
            self._summary.dropped_ticks,
            self._auto_stopped,
        )
        return self._summary

    def _start_computer_audio(self) -> None:
        """Start recording what the computer plays, when switched on; a failure is only
        logged (the recording then has no computer track)."""
        if not self.config.computer_audio or self._audio is None:
            return
        recorder = ComputerAudioRecorder(
            self.paths.computer_audio_path(self.paths.work_dir), self._audio.sample_rate
        )
        try:
            recorder.start()
        except Exception as exc:  # never let this track stop the recording
            if isinstance(exc, ComputerAudioError):
                log.warning("computer audio is not recorded: %s", exc)
            else:  # pragma: no cover
                log.exception("computer audio is not recorded")
            try:
                recorder.path.unlink(missing_ok=True)
            except OSError:  # still held by a stream that did not open in time
                log.debug("cannot remove %s", recorder.path, exc_info=True)
            return
        self._computer = recorder

    def _start_mouse(self) -> None:
        """Record the mouse into ``input/mouse.jsonl`` when switched on and the platform
        offers it. The source starts on a thread of its own (a helper process may take a
        while on a slow machine) so frames and audio are not held up; a failure is logged
        and noted in the manifest, never raised."""
        if not self.config.mouse_actions:
            return
        try:
            source = self.platform.mouse_source()
        except Exception:  # noqa: BLE001 - never let the mouse stop a recording
            log.exception("mouse source unavailable")
            return
        if source is None:
            return
        recorder = InputRecorder(
            self.paths.mouse_log_path(self.paths.work_dir),
            self.screens,
            source,
            self.clock,
            drawing_windows=lambda: self.drawing_windows,
        )
        self._input = recorder

        def start() -> None:
            try:
                recorder.start()
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, PlatformError):
                    log.warning("mouse is not recorded: %s", exc)
                else:  # pragma: no cover
                    log.exception("mouse is not recorded")
                with self._lock:
                    self._mouse_info = recorder.info(error=str(exc) or type(exc).__name__)
                    self._input = None
                return
            log.info("mouse recorded by %s", source.name)

        self._mouse_thread = threading.Thread(target=start, name="mouse-start", daemon=True)
        self._mouse_thread.start()

    def _stop_mouse(self) -> None:
        if self._mouse_thread is not None:
            self._mouse_thread.join(timeout=MOUSE_START_WAIT_S)
            self._mouse_thread = None
        with self._lock:
            recorder, self._input = self._input, None
        if recorder is None:
            return
        try:
            self._mouse_info = recorder.stop()
        except Exception:  # pragma: no cover - never let the mouse fail the recording
            log.exception("mouse recording did not stop cleanly")
            self._mouse_info = recorder.info(error="did not stop cleanly")
        log.info(
            "mouse: %d events, %d restarts", self._mouse_info.events, self._mouse_info.hook_restarts
        )

    def discard(self) -> None:
        """Stop and delete every file of this recording."""
        self.stop()
        self.paths.discard()
        log.info("Recording %s discarded", self.paths.session_id)

    # Capture thread ---------------------------------------------------------

    def _capture_loop(self) -> None:
        interval_ns = self.config.capture_interval_ms * 1_000_000
        auto_stop_ns = self.config.auto_stop_ms * 1_000_000
        tick_index = 0
        last_time_ms = -1
        next_disk_check_ns = 0
        empty_ticks = 0
        frames_dir = self.paths.frames_dir(self.paths.work_dir)
        try:
            with ScreenGrabber() as grabber:
                while not self._stop_event.is_set():
                    target_ns = tick_index * interval_ns
                    now_ns = self.clock.elapsed_ns()
                    if now_ns >= auto_stop_ns:
                        self._auto_stopped = True
                        self._stop_reason = StopReason.AUTO_STOP
                        self._stop_event.set()
                        self._on_event(
                            RecorderEvent.AUTO_STOPPED,
                            f"auto stop after {self.config.auto_stop_minutes} min",
                        )
                        break
                    if now_ns >= next_disk_check_ns:
                        next_disk_check_ns = now_ns + DISK_CHECK_INTERVAL_NS
                        free = free_disk_bytes(frames_dir)
                        if free is not None and free < MIN_FREE_BYTES:
                            message = low_disk_message(self.paths.recording_folder)
                            log.warning("recording stopped: %s", message)
                            self._stop_reason = StopReason.LOW_DISK
                            self._stop_event.set()
                            self._on_event(RecorderEvent.LOW_DISK, message)
                            break
                    if now_ns < target_ns:
                        time.sleep(min((target_ns - now_ns) / 1e9, 0.5))
                        continue
                    # Skip ticks we are too late for (keeps the schedule aligned).
                    missed = (now_ns - target_ns) // interval_ns
                    if missed > 0:
                        with self._lock:
                            self._dropped_ticks += int(missed)
                        tick_index += int(missed)
                    tick_index += 1

                    time_ms = self.clock.elapsed_ms()
                    if time_ms <= last_time_ms:
                        continue  # 1 ms interval edge case: never reuse a frame name
                    last_time_ms = time_ms
                    # The backlog is checked first: a tick that is dropped must not have
                    # consumed anything (the copied text waits for the next tick).
                    if not self._pending.acquire(blocking=False):
                        with self._lock:
                            self._dropped_ticks += 1
                        continue
                    tick = self._grab_tick(grabber, time_ms)
                    if not tick.frames:
                        # No screen could be grabbed (lock screen, UAC prompt): nothing
                        # to store for this tick; a fresh grabber often helps afterwards.
                        self._pending.release()
                        empty_ticks += 1
                        if empty_ticks >= GRAB_REOPEN_AFTER:
                            empty_ticks = 0
                            try:
                                grabber.close()  # the next grab() opens a new one
                            except Exception:  # pragma: no cover - best effort
                                log.debug("screen grabber close failed", exc_info=True)
                        continue
                    empty_ticks = 0
                    assert self._pool is not None
                    future = self._pool.submit(self._write_tick, tick)
                    future.add_done_callback(self._tick_done)
        except Exception as exc:
            log.exception("capture loop failed")
            self._error = f"capture failed: {exc}"
            self._stop_reason = StopReason.CAPTURE_FAILED
            self._stop_event.set()
            self._on_event(RecorderEvent.ERROR, self._error)

    def _grab_screens(self, grabber: ScreenGrabber) -> list[RawFrame]:
        """Every screen that can be grabbed right now; a failing one is skipped."""
        frames: list[RawFrame] = []
        for screen in self.screens:
            try:
                frames.append(grabber.grab(screen))
            except Exception as exc:
                with self._lock:
                    self._failed_grabs += 1
                    count = self._failed_grabs
                if not self._grab_failing or count % GRAB_LOG_EVERY == 0:
                    log.warning(
                        "cannot grab %s (%d failed grabs so far): %s", screen.id, count, exc
                    )
                self._grab_failing = True
        if self._grab_failing and len(frames) == len(self.screens):
            self._grab_failing = False
            log.info("screen capture works again")
        return frames

    def _grab_tick(self, grabber: ScreenGrabber, time_ms: int) -> _Tick:
        position = self.platform.cursor_position()
        cursor = locate_cursor(self.screens, *position) if position else None
        tick = _Tick(time_ms=time_ms, cursor=cursor)
        provider = self.drawings_provider
        if provider is not None:
            try:
                tick.drawings = list(provider())
            except Exception:  # pragma: no cover - never let drawing data stop capture
                log.exception("drawings provider failed")
        drawing_now_provider = self.drawing_now_provider
        if drawing_now_provider is not None:
            try:
                now = drawing_now_provider()
                tick.drawing_now = dict(now) if now else None
            except Exception:  # pragma: no cover - never let drawing data stop capture
                log.exception("drawing_now provider failed")
        zoom_provider = self.zoom_provider
        if zoom_provider is not None:
            try:
                tick.zoom = list(zoom_provider())
            except Exception:  # pragma: no cover - never let zoom data stop capture
                log.exception("zoom provider failed")
        tick.frames = self._grab_screens(grabber)
        clipboard_provider = self.clipboard_provider
        if clipboard_provider is not None and tick.frames:
            # Taken only when the tick is stored: the provider hands a text out once.
            try:
                tick.clipboard = clipboard_provider()
            except Exception:  # pragma: no cover - never let clipboard data stop capture
                log.exception("clipboard provider failed")
        return tick

    def _write_tick(self, tick: _Tick) -> int:
        frames_dir = self.paths.frames_dir(self.paths.work_dir)
        info_dir = self.paths.frames_info_dir(self.paths.work_dir)
        written = 0
        for frame in tick.frames:
            data = encode_frame(frame, self.config.image_format, self.config.jpeg_quality)
            image_path = frames_dir / frame_filename(
                tick.time_ms, frame.screen_id, self.config.image_format
            )
            image_path.write_bytes(data)
            sidecar = {
                "time_ms": tick.time_ms,
                "screen": frame.screen_id,
                "cursor": tick.cursor.to_json() if tick.cursor else None,
                "drawings": tick.drawings,
                "drawing_now": tick.drawing_now,
                "zoom": tick.zoom,
                "clipboard": tick.clipboard,
                "captured_at": now_iso(),
            }
            (info_dir / sidecar_filename(tick.time_ms, frame.screen_id)).write_text(
                json.dumps(sidecar), encoding="utf-8"
            )
            written += 1
        return written

    def _tick_done(self, future: Future[int]) -> None:
        self._pending.release()
        try:
            written = future.result()
        except Exception as exc:
            log.exception("frame write failed")
            self._error = self._error or f"frame write failed: {exc}"
            return
        with self._lock:
            self._frame_count += written


def write_sidecar_index(frames_dir: Path) -> int:
    """Count frames in a directory (used by status reporting)."""
    return sum(1 for p in frames_dir.iterdir() if p.suffix.lower() in {".png", ".jpg"})


__all__ = [
    "ENCODE_WORKERS",
    "KEYPOINT_MIN_GAP_MS",
    "MIN_FREE_BYTES",
    "ClipboardProvider",
    "DrawingNowProvider",
    "DrawingsProvider",
    "LowDiskSpaceError",
    "Recorder",
    "RecorderEvent",
    "RecordingSummary",
    "StopReason",
    "free_disk_bytes",
    "low_disk_message",
]
