"""Audio tracks of an archive: the AAC encode and the check of the written file.

The tests that run FFmpeg are skipped when it is not available.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from video_story.core.config import Config
from video_story.export import ExportError
from video_story.export.ffmpeg_audio import (
    ARCHIVE_AUDIO_BITRATE_K,
    AUDIO_VERIFY_TOLERANCE_MS,
    audio_probe_command,
    encode_audio,
    encode_command,
    verify_audio,
    wav_duration_ms,
)
from video_story.transcribe.ffmpeg import find_ffmpeg


def write_tone(path: Path, ms: int, rate: int = 16000, channels: int = 1) -> Path:
    frames = rate * ms // 1000
    sample = (
        struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * n / rate))) * channels
        for n in range(frames)
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(sample))
    return path


@pytest.fixture
def config() -> Config:
    if find_ffmpeg(Config()) is None:
        pytest.skip("ffmpeg not available")
    return Config()


def test_commands_use_the_built_in_aac_encoder_and_need_no_ffprobe(tmp_path: Path):
    encode = encode_command(Path("ffmpeg"), tmp_path / "audio.wav", tmp_path / "audio.part.m4a")
    assert encode[0] == "ffmpeg" and "-nostdin" in encode and "-progress" in encode
    assert encode[encode.index("-c:a") + 1] == "aac"
    assert encode[encode.index("-b:a") + 1] == f"{ARCHIVE_AUDIO_BITRATE_K}k" == "96k"
    assert encode[encode.index("-f") + 1] == "mp4"  # the partial name does not say
    assert "-ar" not in encode and "-ac" not in encode  # rate and channels are kept
    assert encode[-1] == str(tmp_path / "audio.part.m4a")
    probe = audio_probe_command(Path("ffmpeg"), tmp_path / "audio.m4a")
    assert probe[probe.index("-map") + 1] == "0:a:0"
    assert probe[probe.index("-c") + 1] == "copy" and probe[-3:] == ["-f", "null", "-"]


def test_wav_length_comes_from_the_header(tmp_path: Path):
    assert wav_duration_ms(write_tone(tmp_path / "a.wav", 1500)) == 1500
    assert wav_duration_ms(write_tone(tmp_path / "b.wav", 700, rate=48000, channels=2)) == 700
    broken = tmp_path / "c.wav"
    broken.write_bytes(b"not a wav file")
    with pytest.raises(ExportError) as error:
        wav_duration_ms(broken)
    assert error.value.code == "AUDIO_UNREADABLE"


@pytest.mark.parametrize(("rate", "channels"), [(16000, 1), (48000, 1), (44100, 2)])
def test_encoded_track_is_as_long_as_the_wav(tmp_path: Path, config, rate: int, channels: int):
    wav = write_tone(tmp_path / "audio.wav", 2300, rate, channels)
    m4a = tmp_path / "audio.part.m4a"
    reported: list[float] = []
    encode_audio(wav, m4a, config, lambda fraction, _text: reported.append(fraction))
    assert m4a.stat().st_size > 0 and reported and reported[-1] == 1.0
    measured = verify_audio(m4a, 2300, config)
    assert abs(measured - 2300) <= AUDIO_VERIFY_TOLERANCE_MS
    # The same file is too short for a longer WAV, beyond the tolerance only.
    assert verify_audio(m4a, measured + AUDIO_VERIFY_TOLERANCE_MS, config) == measured
    with pytest.raises(ExportError) as error:
        verify_audio(m4a, measured + AUDIO_VERIFY_TOLERANCE_MS + 1, config)
    assert error.value.code == "OUTPUT_TRUNCATED" and "cut short" in str(error.value)


def test_file_that_is_no_audio_is_not_verified(tmp_path: Path, config):
    garbage = tmp_path / "audio.part.m4a"
    garbage.write_bytes(b"aac of audio.wav")
    with pytest.raises(ExportError) as error:
        verify_audio(garbage, 1000, config)
    assert error.value.code == "OUTPUT_UNVERIFIED"
    # An MP4 cut in the middle has no index any more: it cannot be read back either.
    wav = write_tone(tmp_path / "audio.wav", 3000)
    whole = tmp_path / "whole.m4a"
    encode_audio(wav, whole, config)
    cut = tmp_path / "cut.m4a"
    cut.write_bytes(whole.read_bytes()[: whole.stat().st_size // 2])
    with pytest.raises(ExportError) as error:
        verify_audio(cut, 3000, config)
    assert error.value.code in ("OUTPUT_UNVERIFIED", "OUTPUT_TRUNCATED")


def test_encode_fails_for_a_wav_that_is_none(tmp_path: Path, config):
    broken = tmp_path / "computer.wav"
    broken.write_bytes(b"not a wav file")
    with pytest.raises(ExportError) as error:
        encode_audio(broken, tmp_path / "computer.part.m4a", config)
    assert error.value.code == "AUDIO_UNREADABLE"
    assert not (tmp_path / "computer.part.m4a").exists()


def test_encode_and_check_can_be_cancelled(tmp_path: Path, config):
    wav = write_tone(tmp_path / "audio.wav", 1000)
    with pytest.raises(ExportError) as error:
        encode_audio(wav, tmp_path / "audio.part.m4a", config, cancel=lambda: True)
    assert error.value.code == "CANCELLED"
    encode_audio(wav, tmp_path / "audio.part.m4a", config)
    with pytest.raises(ExportError) as error:
        verify_audio(tmp_path / "audio.part.m4a", 1000, config, cancel=lambda: True)
    assert error.value.code == "CANCELLED"
