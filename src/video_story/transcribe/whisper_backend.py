"""Transcription backends: in-process ``pywhispercpp`` and the external ``whisper-cli``.

Both return a :class:`~video_story.core.transcript.Transcript` with segment times in
milliseconds since the start of the audio and the detected language code.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from video_story.core.config import Config
from video_story.core.transcript import Segment, Transcript

log = logging.getLogger(__name__)

WHISPER_SAMPLE_RATE = 16_000
MIN_AUDIO_MS = 1100  # whisper.cpp rejects input shorter than 1000 ms; we pad with silence

ProgressCallback = Callable[[float], None]


class TranscriptionError(RuntimeError):
    """The backend failed to produce a transcript."""


class TranscriptionBackend(Protocol):
    """Common interface of the whisper backends."""

    name: str

    def transcribe(
        self,
        wav16k: Path,
        *,
        language: str | None,
        threads: int,
        progress: ProgressCallback | None = None,
    ) -> Transcript:
        """Transcribe a 16 kHz mono 16-bit WAV. ``language`` None/"auto" = detect."""
        ...


def _wants_auto(language: str | None) -> bool:
    return language is None or language.strip().lower() in ("", "auto")


def wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate() or WHISPER_SAMPLE_RATE
        return int(wf.getnframes() * 1000 / rate)


def load_wav16k(path: Path) -> np.ndarray:
    """Read a 16 kHz 16-bit WAV as float32 samples in ``[-1, 1]`` (stereo is averaged)."""
    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != WHISPER_SAMPLE_RATE or wf.getsampwidth() != 2:
            raise TranscriptionError(
                f"{path.name} must be {WHISPER_SAMPLE_RATE} Hz 16-bit PCM "
                f"(got {wf.getframerate()} Hz, {wf.getsampwidth() * 8}-bit)"
            )
        channels = wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples


# pywhispercpp -------------------------------------------------------------------


def _import_model_class() -> Any:
    from pywhispercpp.model import Model

    return Model


def _detected_language(model: Any) -> str | None:
    """Language whisper.cpp settled on during the last ``whisper_full`` call."""
    try:
        import _pywhispercpp as pw

        lang_id = pw.whisper_full_lang_id(model._ctx)
        if lang_id < 0:
            return None
        return pw.whisper_lang_str(lang_id) or None
    except Exception as exc:  # binding differences between versions
        log.debug("cannot read detected language: %s", exc)
        return None


class PyWhisperCppBackend:
    """In-process whisper.cpp through ``pywhispercpp`` (CPU only).

    The model is loaded on first use and kept for the lifetime of the instance
    (loading ``ggml-small`` takes about half a second and ~500 MB of RAM).
    """

    name = "pywhispercpp"

    def __init__(self, model_path: Path) -> None:
        self.model_path = Path(model_path)
        self._model: Any = None

    def _load(self, threads: int) -> Any:
        if self._model is None:
            if not self.model_path.is_file():
                raise TranscriptionError(f"whisper model not found: {self.model_path}")
            model_cls = _import_model_class()
            log.info("Loading whisper model %s", self.model_path)
            self._model = model_cls(
                str(self.model_path),
                redirect_whispercpp_logs_to=None,
                print_progress=False,
                print_realtime=False,
                print_timestamps=False,
                n_threads=threads,
                context_params={"use_gpu": False},
            )
        return self._model

    def transcribe(
        self,
        wav16k: Path,
        *,
        language: str | None,
        threads: int,
        progress: ProgressCallback | None = None,
    ) -> Transcript:
        model = self._load(threads)
        audio = load_wav16k(wav16k)
        total_ms = int(len(audio) * 1000 / WHISPER_SAMPLE_RATE)
        if total_ms < MIN_AUDIO_MS:
            pad = int((MIN_AUDIO_MS - total_ms) * WHISPER_SAMPLE_RATE / 1000)
            audio = np.concatenate([audio, np.zeros(pad, dtype=np.float32)])
        lang = "auto" if _wants_auto(language) else language
        assert lang is not None

        def on_segment(segment: Any) -> None:
            if progress is not None and total_ms > 0:
                progress(min(1.0, max(0.0, segment.t1 * 10 / total_ms)))

        raw_segments = model.transcribe(
            audio,
            new_segment_callback=on_segment if progress is not None else None,
            n_threads=threads,
            language=lang,
            translate=False,
        )
        segments = [
            Segment(int(s.t0) * 10, int(s.t1) * 10, str(s.text).strip()) for s in raw_segments
        ]
        detected = _detected_language(model) if _wants_auto(language) else language
        if progress is not None:
            progress(1.0)
        return Transcript(
            language=detected,
            segments=segments,
            model=self.model_path.stem,
            backend=self.name,
        )


# whisper-cli --------------------------------------------------------------------


def parse_whisper_cli_json(data: dict[str, Any]) -> Transcript:
    """Convert the ``-oj`` output of whisper-cli (``offsets`` in ms) to a Transcript."""
    segments: list[Segment] = []
    for item in data.get("transcription", []) or []:
        offsets = item.get("offsets") or {}
        try:
            start = int(offsets["from"])
            end = int(offsets["to"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TranscriptionError(f"whisper-cli segment without offsets: {item!r}") from exc
        segments.append(Segment(start, end, str(item.get("text", "")).strip()))
    result = data.get("result") or {}
    language = result.get("language") or (data.get("params") or {}).get("language")
    if isinstance(language, str) and language.lower() == "auto":
        language = None
    model_name = (data.get("params") or {}).get("model")
    return Transcript(
        language=language,
        segments=segments,
        model=Path(model_name).stem if model_name else None,
        backend="whisper-cli",
    )


class WhisperCliBackend:
    """External ``whisper-cli`` executable from whisper.cpp (JSON output)."""

    name = "whisper-cli"

    def __init__(self, cli_path: Path, model_path: Path) -> None:
        self.cli_path = Path(cli_path)
        self.model_path = Path(model_path)

    def transcribe(
        self,
        wav16k: Path,
        *,
        language: str | None,
        threads: int,
        progress: ProgressCallback | None = None,
    ) -> Transcript:
        if not self.cli_path.is_file():
            raise TranscriptionError(f"whisper-cli not found: {self.cli_path}")
        if not self.model_path.is_file():
            raise TranscriptionError(f"whisper model not found: {self.model_path}")
        lang = "auto" if _wants_auto(language) else language
        assert lang is not None
        if progress is not None:
            progress(0.0)
        with tempfile.TemporaryDirectory(prefix="whisper-cli-") as tmp:
            out_prefix = Path(tmp) / "out"
            cmd = [
                str(self.cli_path),
                "-m",
                str(self.model_path),
                "-f",
                str(wav16k),
                "-l",
                lang,
                "-t",
                str(threads),
                "-ng",
                "-oj",
                "-of",
                str(out_prefix),
            ]
            log.debug("Running %s", " ".join(cmd))
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    creationflags=_creation_flags(),
                )
            except OSError as exc:
                raise TranscriptionError(f"cannot run whisper-cli: {exc}") from exc
            json_path = out_prefix.with_suffix(".json")
            if proc.returncode != 0 or not json_path.is_file():
                tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-10:])
                raise TranscriptionError(
                    f"whisper-cli failed with exit code {proc.returncode}: {tail}"
                )
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TranscriptionError(f"cannot read whisper-cli output: {exc}") from exc
        transcript = parse_whisper_cli_json(data)
        transcript.model = self.model_path.stem
        if not _wants_auto(language):
            transcript.language = language
        if progress is not None:
            progress(1.0)
        return transcript


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0


def create_backend(config: Config) -> TranscriptionBackend:
    """Backend selected by ``config.whisper_backend``."""
    model_path = config.resolved_model_path()
    if config.whisper_backend == "whisper-cli":
        if config.whisper_cli_path is None:
            raise TranscriptionError(
                "whisper_backend is 'whisper-cli' but whisper_cli_path is not configured"
            )
        return WhisperCliBackend(config.whisper_cli_path, model_path)
    return PyWhisperCppBackend(model_path)


__all__ = [
    "MIN_AUDIO_MS",
    "WHISPER_SAMPLE_RATE",
    "ProgressCallback",
    "PyWhisperCppBackend",
    "TranscriptionBackend",
    "TranscriptionError",
    "WhisperCliBackend",
    "create_backend",
    "load_wav16k",
    "parse_whisper_cli_json",
    "wav_duration_ms",
]
