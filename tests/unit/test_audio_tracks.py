"""The two audio tracks (microphone, computer audio): selection, mixing, transcription
and export inputs, the manifest and the computer audio recorder's clock alignment."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
from test_transcribe import FakeBackend

from video_story.capture import computer_audio as ca
from video_story.capture.recorder import Recorder
from video_story.core.audio_mix import (
    COMPUTER,
    MICROPHONE,
    MixReader,
    TrackSelection,
    existing_tracks,
    has_audio,
    open_mix,
    read_selection,
    selected_sources,
    write_selection,
)
from video_story.core.config import Config
from video_story.core.manifest import (
    COMPUTER_AUDIO_FILE,
    AudioInfo,
    Manifest,
    read_manifest,
    write_manifest,
)
from video_story.core.session import SessionPaths
from video_story.export import ffmpeg_export
from video_story.transcribe import ffmpeg as ff
from video_story.transcribe import pipeline


def write_wav(path: Path, samples, rate: int = 16000, channels: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.asarray(samples, dtype="<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(data.tobytes())
    return path


@pytest.fixture
def rec(tmp_path: Path) -> tuple[SessionPaths, Path]:
    paths = SessionPaths(tmp_path, "20260105-100000")
    base = paths.final_dir
    paths.audio_dir(base).mkdir(parents=True)
    return paths, base


# Selection ---------------------------------------------------------------------------


def test_selection_defaults_to_both_tracks_and_round_trips(rec):
    paths, base = rec
    assert read_selection(paths, base) == TrackSelection(True, True)
    off = TrackSelection().toggled(COMPUTER)
    assert off == TrackSelection(microphone=True, computer=False)
    write_selection(paths, base, off)
    assert paths.audio_tracks_path(base).name == "tracks.json"
    assert read_selection(paths, base) == off
    paths.audio_tracks_path(base).write_text("not json", encoding="utf-8")
    assert read_selection(paths, base) == TrackSelection()  # unreadable: both on
    with pytest.raises(ValueError):
        TrackSelection().toggled("speakers")


def test_sources_follow_the_selection_and_the_files_on_disk(rec):
    paths, base = rec
    assert not has_audio(paths, base)
    mic = write_wav(paths.audio_path(base), [1, 2, 3])
    assert existing_tracks(paths, base) == {MICROPHONE: mic}
    assert selected_sources(paths, base) == [mic]  # no computer track: just the microphone
    computer = write_wav(paths.computer_audio_path(base), [4, 5])
    assert paths.computer_audio_path(base).name == "computer.wav"
    assert selected_sources(paths, base) == [mic, computer]
    assert selected_sources(paths, base, TrackSelection(False, True)) == [computer]
    assert selected_sources(paths, base, TrackSelection(False, False)) == []
    assert has_audio(paths, base)


# Mixing -----------------------------------------------------------------------------


def test_mix_adds_the_tracks_clips_and_pads_the_shorter_one(rec):
    paths, base = rec
    write_wav(paths.audio_path(base), [100, -200, 30000, 5, 6, 7])
    write_wav(paths.computer_audio_path(base), [10, 20, 30000, -40])
    with open_mix(paths, base) as mix:
        assert mix.frames == 6 and mix.sample_rate == 16000 and not mix.silent
        assert mix.read(3).tolist() == [110, -180, 32767]  # clipped, never wrapped
        assert mix.read(10).tolist() == [-35, 6, 7]  # the computer track has ended
        assert mix.read(5).tolist() == []
        mix.setpos(1)
        assert mix.read(2).tolist() == [-180, 32767]
    with open_mix(paths, base, TrackSelection(microphone=False, computer=True)) as mix:
        assert mix.frames == 6  # as long as the longest track, even when it is off
        assert mix.read(6).tolist() == [10, 20, 30000, -40, 0, 0]


def test_mix_of_no_track_is_silence_of_the_full_length(rec):
    paths, base = rec
    write_wav(paths.audio_path(base), [100] * 8)
    write_wav(paths.computer_audio_path(base), [7] * 5)
    with open_mix(paths, base, TrackSelection(False, False)) as mix:
        assert mix.silent and mix.frames == 8
        assert mix.read(100).tolist() == [0] * 8


def test_mix_maps_another_sample_rate_and_downmixes_stereo(tmp_path: Path):
    mic = write_wav(tmp_path / "mic.wav", [0, 1, 2, 3, 4, 5, 6, 7], rate=8)
    # 4 Hz stereo: each frame (L, R) averages to 10, 20, 30, 40.
    computer = write_wav(tmp_path / "pc.wav", [5, 15, 15, 25, 25, 35, 35, 45], rate=4, channels=2)
    with MixReader([mic, computer], sample_rate=8) as mix:
        assert mix.frames == 8
        assert mix.read(8).tolist() == [10, 11, 22, 23, 34, 35, 46, 47]


# Transcription and export inputs -------------------------------------------------------


def test_convert_mixes_several_sources_with_amix(tmp_path: Path, monkeypatch):
    seen: list[list[str]] = []

    def run(cmd, **_kwargs):
        seen.append(cmd)
        write_wav(Path(cmd[-1]), [0] * 32000)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ff.subprocess, "run", run)
    a, b = tmp_path / "audio.wav", tmp_path / "computer.wav"
    ff.convert_to_16k_mono(Path("ffmpeg"), [a, b], tmp_path / "16k.wav")
    cmd = seen[-1]
    assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"] == [str(a), str(b)]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph == "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[mix]"
    assert cmd[cmd.index("-map") + 1] == "[mix]"
    ff.convert_to_16k_mono(Path("ffmpeg"), a, tmp_path / "16k.wav")
    assert "-filter_complex" not in seen[-1]  # one source: converted as before
    with pytest.raises(ValueError):
        ff.convert_to_16k_mono(Path("ffmpeg"), [], tmp_path / "16k.wav")


@pytest.fixture
def transcribable(tmp_path: Path, monkeypatch):
    cfg = Config(recording_folder=tmp_path / "rec", whisper_model_path=tmp_path / "m.bin")
    paths = SessionPaths(cfg.recording_folder, "20260105-110000")
    paths.create_work_dirs()
    base = paths.work_dir
    write_wav(paths.audio_path(base), [100] * 16000)
    write_wav(paths.computer_audio_path(base), [50] * 16000)
    sources: list[list[Path]] = []

    def convert(_ffmpeg, src, dst):
        sources.append(list(src))
        write_wav(dst, [0] * 16000)

    monkeypatch.setattr(pipeline, "ensure_ffmpeg", lambda config, download=False: Path("ffmpeg"))
    monkeypatch.setattr(pipeline, "convert_to_16k_mono", convert)
    return paths, cfg, base, sources


def test_transcription_uses_the_tracks_that_are_switched_on(transcribable):
    paths, cfg, base, sources = transcribable
    pipeline.transcribe_audio(paths, cfg, backend=FakeBackend())
    assert sources[-1] == [paths.audio_path(base), paths.computer_audio_path(base)]
    write_selection(paths, base, TrackSelection(microphone=True, computer=False))
    pipeline.transcribe_audio(paths, cfg, backend=FakeBackend())
    assert sources[-1] == [paths.audio_path(base)]


def test_no_track_switched_on_gives_an_empty_transcript_without_whisper(transcribable):
    paths, cfg, base, sources = transcribable
    write_selection(paths, base, TrackSelection(False, False))
    backend = FakeBackend()
    result = pipeline.transcribe_recording(paths, cfg, backend=backend)
    assert result.transcript.segments == [] and result.transcript.complete
    assert backend.calls == [] and sources == []


def test_export_mixes_the_selected_tracks_in_the_filter_graph(tmp_path: Path):
    graph = ffmpeg_export.filter_graph(None, audio_inputs=2)
    assert graph.endswith(";[1:a][2:a]amix=inputs=2:duration=longest:normalize=0,apad[aout]")
    assert "amix" not in ffmpeg_export.filter_graph(None, audio_inputs=1)
    with_cursor = ffmpeg_export.filter_graph(("1", "2"), audio_inputs=2)
    assert "[2:a][3:a]amix" in with_cursor  # the cursor PNG is input 1
    cfg = Config(recording_folder=tmp_path)
    tracks = [tmp_path / "audio.wav", tmp_path / "computer.wav"]
    cmd = ffmpeg_export.build_command(
        Path("ffmpeg"), cfg, tmp_path / "o.mp4", 1500, 250, tracks, False, graph
    )
    assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"][1:] == [str(t) for t in tracks]
    assert cmd.count("-ss") == 2 and "[aout]" in cmd and "-af" not in cmd
    single = ffmpeg_export.build_command(
        Path("ffmpeg"), cfg, tmp_path / "o.mp4", 1500, 250, tracks[:1], False, "g"
    )
    assert "1:a" in single and "-af" in single  # one track: mapped and padded as before


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs FFmpeg on PATH")
def test_real_ffmpeg_accepts_the_mixing_commands(tmp_path: Path):
    """The amix graphs are valid for the FFmpeg on this machine (16 kHz conversion and
    the MP4 export with two tracks)."""
    ffmpeg = Path(shutil.which("ffmpeg") or "ffmpeg")
    t = np.arange(16000) / 16000
    mic = write_wav(tmp_path / "audio.wav", (np.sin(2 * np.pi * 440 * t) * 8000).astype("<i2"))
    pc = write_wav(
        tmp_path / "computer.wav", (np.sin(2 * np.pi * 220 * t[:8000]) * 8000).astype("<i2")
    )
    out = tmp_path / "16k.wav"
    ff.convert_to_16k_mono(ffmpeg, [mic, pc], out)
    with wave.open(str(out), "rb") as wf:
        assert wf.getframerate() == 16000 and abs(wf.getnframes() - 16000) < 400
    frame = tmp_path / "f.png"
    from PIL import Image

    Image.new("RGB", (64, 48), (10, 20, 30)).save(frame)
    (tmp_path / ffmpeg_export.FRAMES_LIST_NAME).write_text(
        f"ffconcat version 1.0\nfile '{frame.as_posix()}'\nduration 1\nfile '{frame.as_posix()}'\n",
        encoding="utf-8",
    )
    cfg = Config(recording_folder=tmp_path, export_preset="ultrafast")
    graph = ffmpeg_export.filter_graph(None, audio_inputs=2)
    cmd = ffmpeg_export.build_command(
        ffmpeg, cfg, tmp_path / "o.mp4", 1000, 0, [mic, pc], False, graph
    )
    proc = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert (tmp_path / "o.mp4").stat().st_size > 0


# Manifest and recorder -------------------------------------------------------------


def test_manifest_keeps_the_computer_track(tmp_path: Path):
    manifest = Manifest(
        session_id="20260105-120000",
        started_at="",
        finished_at="",
        recorded_duration_ms=1000,
        capture_interval_ms=500,
        image_format="jpeg",
        jpeg_quality=70,
        screens=[],
        computer_audio=AudioInfo(file=COMPUTER_AUDIO_FILE, device="Speakers", sample_rate=48000),
    )
    path = tmp_path / "manifest.json"
    write_manifest(path, manifest)
    loaded = read_manifest(path)
    assert loaded.computer_audio is not None
    assert loaded.computer_audio.file == "audio/computer.wav"
    assert loaded.computer_audio.device == "Speakers"
    manifest.computer_audio = None
    write_manifest(path, manifest)
    assert read_manifest(path).computer_audio is None


class _FakeWav:
    def __init__(self) -> None:
        self.data = bytearray()

    def writeframes(self, raw: bytes) -> None:
        self.data += raw

    @property
    def samples(self) -> np.ndarray:
        return np.frombuffer(bytes(self.data), dtype="<i2")


def test_computer_audio_stays_on_the_recording_clock(tmp_path: Path):
    recorder = ca.ComputerAudioRecorder(tmp_path / "computer.wav", sample_rate=1000)
    wav = _FakeWav()
    recorder._origin = 0.0
    block = np.full((50, 2), 0.5, dtype=np.float32)  # stereo float, 50 ms
    recorder._write(wav, block, 0.05)
    assert recorder.frames_written == 50 and wav.samples[0] == 16383  # downmixed to mono
    # Nothing came for a second (silence: WASAPI delivers nothing): filled with zeros.
    recorder._write(wav, block, 1.1)
    assert recorder.frames_written == 1100
    assert not wav.samples[50:1050].any() and wav.samples[1050:].all()
    # Small jitter is left alone.
    recorder._write(wav, block[:30], 1.11)
    assert recorder.frames_written == 1130
    # The device ran ahead of the clock: the surplus is dropped.
    recorder._write(wav, np.zeros((400, 1), dtype=np.float32), 1.2)
    assert recorder.frames_written == 1200
    recorder._pad_to(wav, 1.5)
    assert recorder.frames_written == 1500  # stop: silence up to the end


class _NoComputerAudio:
    def __init__(self, path: Path, sample_rate: int) -> None:
        self.path = path

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"partial")
        raise ca.ComputerAudioError("no audio output device")


def test_a_failing_computer_track_never_stops_the_recording(tmp_path: Path, monkeypatch):
    from video_story.capture import recorder as recorder_mod

    monkeypatch.setattr(recorder_mod, "ComputerAudioRecorder", _NoComputerAudio)
    paths = SessionPaths(tmp_path, "20260105-130000")
    paths.create_work_dirs()

    class _Mic:
        sample_rate = 48000

    platform = None  # never used before the capture starts
    recorder = Recorder(Config(recording_folder=tmp_path), platform, paths)  # type: ignore[arg-type]
    recorder._audio = _Mic()  # type: ignore[assignment]
    recorder._start_computer_audio()
    assert recorder._computer is None
    assert not paths.computer_audio_path(paths.work_dir).exists()  # no half track left
    off = Config(recording_folder=tmp_path, computer_audio=False)
    recorder = Recorder(off, platform, paths)  # type: ignore[arg-type]
    recorder._audio = _Mic()  # type: ignore[assignment]
    recorder._start_computer_audio()  # switched off in the settings: not even tried
    assert recorder._computer is None


def test_computer_audio_is_reported_unsupported_off_windows(monkeypatch):
    monkeypatch.setattr(ca.sys, "platform", "linux")
    support = ca.computer_audio_support()
    assert not support.supported and "Windows" in support.reason
