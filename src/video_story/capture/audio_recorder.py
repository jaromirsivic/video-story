"""Microphone recording to a 16-bit PCM mono WAV file (sounddevice / PortAudio)."""

from __future__ import annotations

import logging
import queue
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# Preferred host APIs on Windows: WASAPI gives the most faithful device list.
_HOST_API_PRIORITY = ("Windows WASAPI", "Windows DirectSound", "MME", "ALSA", "Core Audio")


class AudioError(RuntimeError):
    """Audio device problems that prevent recording."""


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    host_api: str
    default_samplerate: int
    max_input_channels: int
    is_default: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "host_api": self.host_api,
            "default_samplerate": self.default_samplerate,
            "max_input_channels": self.max_input_channels,
            "is_default": self.is_default,
        }


@dataclass
class AudioResult:
    device: str
    sample_rate: int
    channels: int
    frames_written: int
    dropped_blocks: int
    error: str | None = None  # why writing stopped early (disk full, ...), else None

    @property
    def duration_ms(self) -> int:
        return int(self.frames_written * 1000 / self.sample_rate) if self.sample_rate else 0


def _sd() -> Any:
    import sounddevice as sd

    return sd


def _preferred_host_api(host_apis: list[dict[str, Any]]) -> int | None:
    """Index of the host API to list devices from (one API => no duplicate names)."""
    names = [h["name"] for h in host_apis]
    for wanted in _HOST_API_PRIORITY:
        if wanted in names:
            return names.index(wanted)
    return None


def list_input_devices() -> list[AudioDevice]:
    """Input devices of the preferred host API, system default first.

    On Windows only WASAPI devices are listed: MME truncates names to 31 characters
    and WDM-KS exposes outputs and raw endpoints that are useless as choices.
    """
    sd = _sd()
    try:
        devices = sd.query_devices()
        host_apis = list(sd.query_hostapis())
    except Exception as exc:  # PortAudio errors
        raise AudioError(f"cannot query audio devices: {exc}") from exc
    preferred = _preferred_host_api(host_apis)
    if preferred is not None:
        default_index = host_apis[preferred].get("default_input_device", -1)
    else:
        try:
            default_index = sd.default.device[0]
        except Exception:
            default_index = -1
    result: list[AudioDevice] = []
    for index, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        if preferred is not None and dev.get("hostapi") != preferred:
            continue
        host_name = host_apis[dev["hostapi"]]["name"] if dev.get("hostapi") is not None else ""
        result.append(
            AudioDevice(
                index=index,
                name=dev["name"],
                host_api=host_name,
                default_samplerate=int(dev.get("default_samplerate") or 48000),
                max_input_channels=int(dev["max_input_channels"]),
                is_default=index == default_index,
            )
        )
    return sorted(result, key=lambda d: (not d.is_default, d.name.lower()))


def default_input_device() -> AudioDevice | None:
    for dev in list_input_devices():
        if dev.is_default:
            return dev
    return None


def resolve_input_device(name: str | None) -> AudioDevice:
    """Find the configured device by name, or the system default input."""
    if name:
        for dev in list_input_devices():
            if dev.name == name:
                return dev
        log.warning("Audio input %r not found; falling back to the default input", name)
    default = default_input_device()
    if default is None:
        devices = list_input_devices()
        if not devices:
            raise AudioError("No audio input device is available.")
        return devices[0]
    return default


class AudioRecorder:
    """Records a mono 16-bit WAV from the chosen device until :meth:`stop`."""

    def __init__(
        self, path: Path, device_name: str | None = None, queue_seconds: float = 30.0
    ) -> None:
        self.path = path
        self.device = resolve_input_device(device_name)
        self.sample_rate = self.device.default_samplerate
        self.channels = 1
        self._queue: queue.Queue[np.ndarray | None] = queue.Queue(
            maxsize=max(10, int(queue_seconds * self.sample_rate / 1024))
        )
        self._stream: Any = None
        self._writer: threading.Thread | None = None
        self._wave: wave.Wave_write | None = None
        self._frames_written = 0
        self._dropped_blocks = 0
        self._write_error: str | None = None
        self._lock = threading.Lock()

    # Callbacks run on the PortAudio thread: keep them tiny.
    def _callback(self, indata: np.ndarray, frames: int, _time: Any, status: Any) -> None:
        if status:
            log.debug("audio status: %s", status)
        try:
            self._queue.put_nowait(indata.copy())
        except queue.Full:
            self._dropped_blocks += 1

    def _write_loop(self) -> None:
        assert self._wave is not None
        while True:
            block = self._queue.get()
            if block is None:
                break
            if self._write_error is not None:
                continue  # keep draining, so that stop() never blocks on a full queue
            if block.ndim == 2 and block.shape[1] > 1:
                block = block.mean(axis=1).astype(np.int16)
            try:
                self._wave.writeframes(block.tobytes())
            except Exception as exc:  # disk full, folder gone, WAV size limit
                log.exception("audio write failed; the rest of the audio is lost")
                self._write_error = f"audio write failed: {exc}"
                continue
            with self._lock:
                self._frames_written += len(block)

    @property
    def error(self) -> str | None:
        """Why the audio stopped being written (``None`` while everything is fine)."""
        return self._write_error

    def start(self) -> None:
        sd = _sd()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wave = wave.open(str(self.path), "wb")
        self._wave.setnchannels(self.channels)
        self._wave.setsampwidth(2)
        self._wave.setframerate(self.sample_rate)
        try:
            self._stream = sd.InputStream(
                device=self.device.index,
                channels=self.channels,
                samplerate=self.sample_rate,
                dtype="int16",
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            self._wave.close()
            raise AudioError(f"cannot open audio input {self.device.name!r}: {exc}") from exc
        self._writer = threading.Thread(target=self._write_loop, name="audio-writer", daemon=True)
        self._writer.start()
        log.info("Audio recording started: %s @ %d Hz", self.device.name, self.sample_rate)

    @property
    def frames_written(self) -> int:
        with self._lock:
            return self._frames_written

    def stop(self) -> AudioResult:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:  # pragma: no cover
                log.warning("audio stream close failed: %s", exc)
            self._stream = None
        if self._writer is not None:
            try:
                self._queue.put(None, timeout=10)
            except queue.Full:  # the writer is gone; nothing is left to flush
                log.warning("audio writer did not take the stop marker")
            self._writer.join(timeout=30)
            self._writer = None
        if self._wave is not None:
            try:
                self._wave.close()
            except Exception as exc:  # the header of a file that could not be written
                log.warning("audio file close failed: %s", exc)
                self._write_error = self._write_error or f"audio close failed: {exc}"
            self._wave = None
        result = AudioResult(
            device=self.device.name,
            sample_rate=self.sample_rate,
            channels=self.channels,
            frames_written=self.frames_written,
            dropped_blocks=self._dropped_blocks,
            error=self._write_error,
        )
        log.info(
            "Audio recording stopped: %d ms, %d dropped blocks",
            result.duration_ms,
            result.dropped_blocks,
        )
        return result


__all__ = [
    "AudioDevice",
    "AudioError",
    "AudioRecorder",
    "AudioResult",
    "default_input_device",
    "list_input_devices",
    "resolve_input_device",
]
