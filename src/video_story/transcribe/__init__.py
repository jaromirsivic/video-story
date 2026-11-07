"""Offline transcription: FFmpeg conversion, whisper.cpp backends and the pipeline."""

from .ffmpeg import (
    FfmpegError,
    FfmpegNotFound,
    convert_to_16k_mono,
    download_ffmpeg,
    ensure_ffmpeg,
    ffmpeg_version,
    find_ffmpeg,
)
from .pipeline import TranscriptionResult, transcribe_recording
from .whisper_backend import (
    PyWhisperCppBackend,
    TranscriptionBackend,
    TranscriptionError,
    WhisperCliBackend,
    create_backend,
    parse_whisper_cli_json,
)

__all__ = [
    "FfmpegError",
    "FfmpegNotFound",
    "PyWhisperCppBackend",
    "TranscriptionBackend",
    "TranscriptionError",
    "TranscriptionResult",
    "WhisperCliBackend",
    "convert_to_16k_mono",
    "create_backend",
    "download_ffmpeg",
    "ensure_ffmpeg",
    "ffmpeg_version",
    "find_ffmpeg",
    "parse_whisper_cli_json",
    "transcribe_recording",
]
