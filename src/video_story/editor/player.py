"""Playback clock driven by the audio output (sounddevice), with a wall-clock fallback.

The audio (the mix of the switched-on tracks, a
:class:`~video_story.core.audio_mix.MixReader`) is read block by block inside the
PortAudio callback, so nothing is loaded into memory. The playhead position is the
number of samples handed to the device (minus the reported output latency) added to the
start position. Playback only runs inside ``[in_ms, out_ms)``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Signal

from video_story.core.audio_mix import MixReader

from .recording import AudioFile

log = logging.getLogger(__name__)


def _load_sounddevice() -> Any | None:
    """Import sounddevice lazily (tests replace this to force the fallback clock)."""
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001 - missing PortAudio library, etc.
        log.warning("sounddevice unavailable: %s", exc)
        return None
    return sd


class Player(QObject):
    """Owns the output stream; ``position_ms()`` is the playhead while playing."""

    playing_changed = Signal(bool)
    status_message = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._audio: AudioFile | None = None
        self._in_ms = 0
        self._out_ms = 0
        self._playing = False
        self._position_ms = 0  # position while paused / stopped
        self._start_ms = 0
        self._lock = threading.Lock()
        self._wave: MixReader | None = None
        self._stream: Any | None = None
        self._sd: Any | None = None
        self._delivered = 0  # samples handed to the device since start
        self._end_frame = 0
        self._exhausted = False
        self._latency_s = 0.0
        self._clock_t0: float | None = None
        self.using_audio = False
        self._warned = False

    # Configuration ----------------------------------------------------------

    def set_audio(self, audio: AudioFile | None) -> None:
        self.stop()
        self._audio = audio
        self._warned = False

    def set_range(self, in_ms: int, out_ms: int) -> None:
        self._in_ms, self._out_ms = in_ms, out_ms
        if self._playing:
            end_frame = self._frame_of(out_ms)
            with self._lock:
                self._end_frame = end_frame

    @property
    def playing(self) -> bool:
        return self._playing

    # Transport --------------------------------------------------------------

    def play(self, from_ms: int) -> int:
        """Start at ``from_ms`` (or at In when outside the crop); returns the start."""
        self.pause()
        start = from_ms if self._in_ms <= from_ms < self._out_ms else self._in_ms
        self._start_ms = start
        self._position_ms = start
        self._delivered = 0
        self._exhausted = False
        # Past the end of a WAV shorter than the recording there is nothing to play:
        # run on the system clock silently (that is not an audio failure).
        past_audio = self._audio is not None and self._frame_of(start) >= self._audio.frames
        self.using_audio = False if past_audio else self._start_audio(start)
        if not self.using_audio:
            self._clock_t0 = time.perf_counter()
            if not past_audio and not self._warned:
                self._warned = True
                self.status_message.emit(
                    "No audio output available: playing silently with a system clock."
                )
        self._playing = True
        self.playing_changed.emit(True)
        return start

    def pause(self) -> int:
        """Freeze the clock; returns the position."""
        if not self._playing:
            return self._position_ms
        position = self.position_ms()
        self._playing = False
        self._close_stream()
        self._clock_t0 = None
        self._position_ms = position
        self.playing_changed.emit(False)
        return position

    def stop(self) -> int:
        """Stop and return to In."""
        self.pause()
        self._position_ms = self._in_ms
        return self._position_ms

    def position_ms(self) -> int:
        if not self._playing:
            return self._position_ms
        if self.using_audio and self._audio is not None:
            with self._lock:
                delivered = self._delivered
            played = max(0, delivered - int(self._latency_s * self._audio.sample_rate))
            position = self._start_ms + int(played * 1000 / self._audio.sample_rate)
        else:
            elapsed = time.perf_counter() - (self._clock_t0 or time.perf_counter())
            position = self._start_ms + int(elapsed * 1000)
        return min(position, self._out_ms)

    @property
    def reached_end(self) -> bool:
        if not self._playing:
            return False
        if self.using_audio and not self._stream_active():
            if self._exhausted and self._audio_covers_out():
                return True  # the device drained the last block of the crop
            # Either the WAV ended before Out (continue silently) or PortAudio stopped
            # the stream (device lost, callback error): carry on with the system clock.
            self._continue_on_clock(failed=not self._exhausted)
        return self.position_ms() >= self._out_ms

    def _stream_active(self) -> bool:
        stream = self._stream
        try:
            return stream is not None and bool(stream.active)
        except Exception:  # noqa: BLE001
            return False

    def _audio_covers_out(self) -> bool:
        audio = self._audio
        if audio is None:
            return False
        return int(self._out_ms * audio.sample_rate / 1000) <= audio.frames

    def _continue_on_clock(self, failed: bool) -> None:
        """Switch from the audio clock to the wall clock at the current position."""
        audio = self._audio
        assert audio is not None
        with self._lock:
            delivered = self._delivered
        if failed:
            position = self.position_ms()  # what was heard: minus the output latency
        else:
            # Drained: every delivered sample was heard.
            position = self._start_ms + int(delivered * 1000 / audio.sample_rate)
        self._close_stream()
        self.using_audio = False
        self._start_ms = min(position, self._out_ms)
        self._clock_t0 = time.perf_counter()
        if failed:
            log.warning("audio output stopped unexpectedly; continuing with a system clock")
            self.status_message.emit(
                "Audio output stopped: continuing silently with a system clock."
            )

    # Audio stream -----------------------------------------------------------

    def _frame_of(self, time_ms: int) -> int:
        if self._audio is None:
            return 0
        return min(self._audio.frames, int(time_ms * self._audio.sample_rate / 1000))

    def _start_audio(self, start_ms: int) -> bool:
        audio = self._audio
        if audio is None or audio.frames <= 0:
            return False
        if self._sd is None:
            self._sd = _load_sounddevice()
        sd = self._sd
        if sd is None:
            return False
        try:
            wf = audio.open()
            wf.setpos(self._frame_of(start_ms))
            self._wave = wf
            self._end_frame = self._frame_of(self._out_ms)
            stream = sd.OutputStream(
                samplerate=audio.sample_rate,
                channels=1,
                dtype="int16",
                callback=self._callback,
            )
            self._stream = stream  # before start(): a failing start() must not leak it
            stream.start()
            try:
                self._latency_s = float(stream.latency or 0.0)
            except Exception:  # noqa: BLE001
                self._latency_s = 0.0
            return True
        except Exception as exc:  # noqa: BLE001 - no device, unsupported rate, ...
            log.warning("audio output unavailable: %s", exc)
            self._close_stream()
            return False

    def _callback(self, outdata: np.ndarray, frames: int, _time: Any, status: Any) -> None:
        if status:
            log.debug("audio output status: %s", status)
        sd = self._sd
        with self._lock:
            wf = self._wave
            if wf is None:
                outdata[:] = 0
                raise sd.CallbackStop
            remaining = max(0, self._end_frame - wf.tell())
            count = min(frames, remaining)
            if count > 0:
                samples = wf.read(count)
                count = min(count, len(samples))
                outdata[:count, 0] = samples[:count]
            if count < frames:
                outdata[count:] = 0
                self._exhausted = True
            self._delivered += count
        if count < frames:
            raise sd.CallbackStop

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            # abort() discards the buffers still queued in the device instead of
            # draining them (stop() would block the GUI thread for the output latency and
            # play ~latency beyond the position we store), so what was heard matches
            # position_ms().
            halt = getattr(stream, "abort", None) or getattr(stream, "stop", None)
            try:
                if halt is not None:
                    halt()
            except Exception:  # noqa: BLE001
                log.debug("aborting audio stream failed", exc_info=True)
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                log.debug("closing audio stream failed", exc_info=True)
        with self._lock:
            wf, self._wave = self._wave, None
        if wf is not None:
            wf.close()


__all__ = ["Player"]
