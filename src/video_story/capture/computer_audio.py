"""Recording what this computer plays (system audio) to ``audio/computer.wav``.

Windows only for now: the default output device is opened as a WASAPI loopback
"microphone" with the ``soundcard`` package. Linux (a PulseAudio/PipeWire monitor
source) and macOS (ScreenCaptureKit or a virtual device) are left for later:
:func:`computer_audio_support` reports them as unsupported and the recording simply
has no computer track.

The track is a mono 16-bit WAV at the microphone's sample rate, so that the editor can
add the two sample by sample. It is kept on the recording timeline by the wall clock:
WASAPI may deliver nothing while nothing plays, and the two devices run on clocks of
their own, so missing time is filled with silence and a surplus is dropped whenever
the track drifts more than :data:`DRIFT_TOLERANCE_S` from the clock. The time between
:meth:`ComputerAudioRecorder.pause` and ``resume`` does not count (the recording cuts
it out) and nothing is written meanwhile; ``muted`` writes silence of the same length.

A failure of this track never stops the recording: it is logged, the track ends there
(padded with silence up to the stop) and the microphone and the screens go on.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import warnings
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .audio_recorder import AudioResult

log = logging.getLogger(__name__)

DRIFT_TOLERANCE_S = 0.1  # the track may run this far ahead of or behind the clock
OPEN_TIMEOUT_S = 5.0  # start() waits this long for the loopback stream to open
BLOCK_FRAMES = 1024


class ComputerAudioError(RuntimeError):
    """The sound of this computer cannot be recorded."""


@dataclass(frozen=True)
class ComputerAudioSupport:
    supported: bool
    reason: str = ""  # why not, when not supported
    device: str | None = None  # the output device that would be recorded


def _soundcard() -> Any:
    """The ``soundcard`` module, usable from the calling thread.

    soundcard initialises COM (multithreaded) when it is first imported and treats any
    other answer than S_OK as an error, so it must be imported in a thread of ours
    (never the GUI thread, whose COM Qt initialised single-threaded); every later
    thread joins the multithreaded apartment here before it talks to WASAPI.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import soundcard
    _init_com()
    return soundcard


def _init_com() -> None:
    """Join the multithreaded COM apartment (S_FALSE when already done: harmless)."""
    if sys.platform != "win32":
        return
    import ctypes

    coinit_multithreaded = 0x0
    ctypes.windll.ole32.CoInitializeEx(None, coinit_multithreaded)


def computer_audio_support() -> ComputerAudioSupport:
    """Whether this computer's sound can be recorded, and from which device."""
    if sys.platform != "win32":
        return ComputerAudioSupport(
            False, "recording the computer's sound is implemented on Windows only so far"
        )
    result: list[ComputerAudioSupport] = []

    def probe() -> None:
        try:
            sc = _soundcard()
            speaker = sc.default_speaker()
            if speaker is None:
                result.append(ComputerAudioSupport(False, "no audio output device"))
                return
            result.append(ComputerAudioSupport(True, "", str(speaker.name)))
        except Exception as exc:  # noqa: BLE001 - missing package, no device, COM error
            result.append(ComputerAudioSupport(False, f"cannot open the audio output: {exc}"))

    # A thread of its own: COM of the GUI thread is initialised by Qt in another mode.
    thread = threading.Thread(target=probe, name="computer-audio-probe", daemon=True)
    thread.start()
    thread.join(OPEN_TIMEOUT_S)
    return result[0] if result else ComputerAudioSupport(False, "the audio output did not answer")


class ComputerAudioRecorder:
    """Records the default output device (loopback) until :meth:`stop`."""

    def __init__(self, path: Path, sample_rate: int) -> None:
        self.path = path
        self.sample_rate = int(sample_rate)
        self.channels = 1
        self.device = ""
        self._thread: threading.Thread | None = None
        self._opened = threading.Event()
        self._stop = threading.Event()
        self._open_error: str | None = None
        self._error: str | None = None
        self._frames_written = 0
        self._origin: float | None = None  # monotonic time of sample 0
        self._lock = threading.Lock()
        # Pause: nothing is written and the paused time does not count for the clock the
        # track follows. Mute: silence of the same length is written instead.
        self._paused_since: float | None = None
        self._paused_total = 0.0
        self.muted = False

    @property
    def paused(self) -> bool:
        return self._paused_since is not None

    def pause(self) -> None:
        with self._lock:
            if self._paused_since is None:
                self._paused_since = time.monotonic()

    def resume(self) -> None:
        with self._lock:
            if self._paused_since is not None:
                self._paused_total += time.monotonic() - self._paused_since
                self._paused_since = None

    @property
    def error(self) -> str | None:
        """Why the track stopped being recorded (``None`` while everything is fine)."""
        return self._error

    @property
    def frames_written(self) -> int:
        with self._lock:
            return self._frames_written

    def start(self) -> None:
        """Open the loopback stream; raises :class:`ComputerAudioError` when it cannot."""
        support = computer_audio_support()
        if not support.supported:
            raise ComputerAudioError(support.reason)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="computer-audio", daemon=True)
        self._thread.start()
        if not self._opened.wait(OPEN_TIMEOUT_S):
            self._stop.set()
            raise ComputerAudioError("the audio output did not open in time")
        if self._open_error is not None:
            self._thread.join(timeout=5)
            raise ComputerAudioError(self._open_error)
        log.info("Computer audio recording started: %s @ %d Hz", self.device, self.sample_rate)

    def stop(self) -> AudioResult:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        result = AudioResult(
            device=self.device,
            sample_rate=self.sample_rate,
            channels=self.channels,
            frames_written=self.frames_written,
            dropped_blocks=0,
            error=self._error,
        )
        log.info("Computer audio recording stopped: %d ms", result.duration_ms)
        return result

    # Recording thread -------------------------------------------------------

    def _run(self) -> None:
        try:
            sc = _soundcard()
            speaker = sc.default_speaker()
            loopback = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            self.device = str(speaker.name)
            channels = max(1, int(getattr(loopback, "channels", 2) or 2))
            wav = wave.open(str(self.path), "wb")
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
        except Exception as exc:  # noqa: BLE001
            self._open_error = f"cannot open the audio output: {exc}"
            self._opened.set()
            return
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # soundcard's "data discontinuity"
                with loopback.recorder(
                    samplerate=self.sample_rate, channels=channels, blocksize=BLOCK_FRAMES
                ) as recorder:
                    self._origin = time.monotonic()
                    self._opened.set()
                    while not self._stop.is_set():
                        data = recorder.record(numframes=None)
                        if len(data) == 0:
                            time.sleep(0.01)  # never spin while the device has nothing
                            continue
                        if self.paused:
                            continue  # drained, not written: the pause is cut out
                        self._write(wav, data, time.monotonic())
        except Exception as exc:  # noqa: BLE001 - device gone, driver error, disk full
            if not self._opened.is_set():
                self._open_error = f"cannot open the audio output: {exc}"
                self._opened.set()
            else:
                log.exception("computer audio failed; the rest of the track is silence")
                self._error = f"computer audio failed: {exc}"
        finally:
            try:
                if self._origin is not None and self._error is None:
                    self._pad_to(wav, time.monotonic())
            except Exception:  # noqa: BLE001 - best effort
                log.debug("padding the computer audio failed", exc_info=True)
            try:
                wav.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("computer audio file close failed: %s", exc)

    def _expected_frames(self, now: float) -> int:
        """Samples the track should hold at monotonic time ``now`` (paused time left out)."""
        assert self._origin is not None
        with self._lock:
            paused = self._paused_total
            if self._paused_since is not None:
                paused += max(0.0, now - self._paused_since)
        return max(0, int((now - self._origin - paused) * self.sample_rate))

    def _append(self, wav: wave.Wave_write, samples: np.ndarray) -> None:
        if len(samples) == 0:
            return
        wav.writeframes(samples.astype("<i2").tobytes())
        with self._lock:
            self._frames_written += len(samples)

    def _pad_to(self, wav: wave.Wave_write, now: float) -> None:
        missing = self._expected_frames(now) - self.frames_written
        if missing > 0:
            self._append(wav, np.zeros(missing, dtype=np.int16))

    def _write(self, wav: wave.Wave_write, data: Any, now: float) -> None:
        """Append one block (float -1..1, frames x channels) ending at ``now``."""
        block = np.asarray(data, dtype=np.float32)
        if block.ndim == 2:
            block = block.mean(axis=1) if block.shape[1] > 1 else block[:, 0]
        samples = (np.clip(block, -1.0, 1.0) * 32767.0).astype(np.int16)
        if self.muted:
            samples = np.zeros_like(samples)
        tolerance = int(DRIFT_TOLERANCE_S * self.sample_rate)
        expected = self._expected_frames(now)
        written = self.frames_written
        gap = expected - (written + len(samples))
        if gap > tolerance:  # nothing came for a while (silence) or the device is slow
            self._append(wav, np.zeros(gap, dtype=np.int16))
        elif gap < -tolerance:  # the device runs ahead of the clock: drop the surplus
            samples = samples[min(len(samples), -gap) :]
        self._append(wav, samples)


__all__ = [
    "ComputerAudioError",
    "ComputerAudioRecorder",
    "ComputerAudioSupport",
    "computer_audio_support",
]
