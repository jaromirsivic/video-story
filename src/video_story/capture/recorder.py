"""Recording orchestration: screens + cursor + audio on one timeline.

The :class:`Recorder` owns a capture thread that ticks every ``capture_interval_ms``,
reads the cursor once, grabs every screen and hands the raw frames to a bounded
thread pool for encoding and writing. Audio is recorded by :class:`AudioRecorder`.
The recording never pauses; it ends on :meth:`stop`, :meth:`discard` or when the
auto-stop limit is reached.
"""

from __future__ import annotations

import json
import logging
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
from video_story.core.manifest import AudioInfo, FramesInfo, Manifest
from video_story.core.naming import frame_filename, sidecar_filename
from video_story.core.screens import Screen, assign_screen_ids
from video_story.core.session import SessionPaths, now_iso
from video_story.core.timeline import RecordingClock
from video_story.platform.base import Platform, PlatformError

from .audio_recorder import AudioError, AudioRecorder, AudioResult
from .screen_grabber import RawFrame, ScreenGrabber, encode_frame

log = logging.getLogger(__name__)

ENCODE_WORKERS = 4
MAX_PENDING_TICKS = 8  # bounded backlog; beyond this, ticks are dropped and counted


class RecorderEvent(StrEnum):
    STARTED = "started"
    AUTO_STOPPED = "auto_stopped"
    STOPPED = "stopped"
    ERROR = "error"


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
            frames=FramesInfo(count=self.frame_count, dropped_ticks=self.dropped_ticks),
            auto_stopped=self.auto_stopped,
            auto_stop_minutes=config.auto_stop_minutes,
            platform=platform_name,
            plugin_version=__version__,
            notes=list(notes or []),
        )


DrawingsProvider = Callable[[], list[dict[str, Any]]]


@dataclass
class _Tick:
    time_ms: int
    cursor: CursorPosition | None
    frames: list[RawFrame] = field(default_factory=list)
    drawings: list[dict[str, Any]] = field(default_factory=list)


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
        self._frame_count = 0
        self._dropped_ticks = 0
        self._auto_stopped = False
        self._error: str | None = None
        self._lock = threading.Lock()
        self._summary: RecordingSummary | None = None
        # Called from the capture thread at every tick; returns what is drawn on the
        # screen right now (see ui.overlay.DrawingOverlay.drawings). Stored in the
        # sidecar of every frame of the tick as ``drawings``.
        self.drawings_provider: DrawingsProvider | None = None

    # Properties -------------------------------------------------------------

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

    # Lifecycle --------------------------------------------------------------

    def start(self) -> None:
        if self._capture_thread is not None:
            raise RuntimeError("recorder already started")
        status = self.platform.support_status()
        if not status.supported:
            raise PlatformError(status.reason)
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
        # The timeline starts once the audio stream is live, so audio and frames align.
        self.started_at = now_iso()
        self.clock.start()
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
        duration_ms = self.clock.stop() if self.clock.running or self.clock.elapsed_ns() else 0
        if self._audio is not None and self._audio_result is None:
            try:
                self._audio_result = self._audio.stop()
            except Exception as exc:  # pragma: no cover
                log.exception("audio stop failed")
                self._error = self._error or f"audio stop failed: {exc}"
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
        try:
            with ScreenGrabber() as grabber:
                while not self._stop_event.is_set():
                    target_ns = tick_index * interval_ns
                    now_ns = self.clock.elapsed_ns()
                    if now_ns < target_ns:
                        time.sleep(min((target_ns - now_ns) / 1e9, 0.5))
                        continue
                    if now_ns >= auto_stop_ns:
                        self._auto_stopped = True
                        self._stop_event.set()
                        self._on_event(
                            RecorderEvent.AUTO_STOPPED,
                            f"auto stop after {self.config.auto_stop_minutes} min",
                        )
                        break
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
                    tick = self._grab_tick(grabber, time_ms)
                    if not self._pending.acquire(blocking=False):
                        with self._lock:
                            self._dropped_ticks += 1
                        continue
                    assert self._pool is not None
                    future = self._pool.submit(self._write_tick, tick)
                    future.add_done_callback(self._tick_done)
        except Exception as exc:
            log.exception("capture loop failed")
            self._error = f"capture failed: {exc}"
            self._stop_event.set()
            self._on_event(RecorderEvent.ERROR, self._error)

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
        for screen in self.screens:
            tick.frames.append(grabber.grab(screen))
        return tick

    def _write_tick(self, tick: _Tick) -> int:
        frames_dir = self.paths.frames_dir(self.paths.work_dir)
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
                "captured_at": now_iso(),
            }
            (frames_dir / sidecar_filename(tick.time_ms, frame.screen_id)).write_text(
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


__all__ = ["ENCODE_WORKERS", "DrawingsProvider", "Recorder", "RecorderEvent", "RecordingSummary"]
