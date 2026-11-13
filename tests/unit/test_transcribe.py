"""Unit tests for the transcription layer: no network, no model, no audio devices."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
import urllib.error
import wave
import zipfile
from pathlib import Path

import numpy as np
import pytest

from video_story.core.config import Config
from video_story.core.session import SessionPaths
from video_story.core.transcript import Segment, Transcript, read_transcript
from video_story.host import downloads
from video_story.transcribe import ffmpeg as ff
from video_story.transcribe import pipeline
from video_story.transcribe import whisper_backend as wb

# Helpers -------------------------------------------------------------------------


def write_silence(path: Path, seconds: float = 1.0, rate: int = 48000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))


class FakeResponse:
    """Minimal stand-in for the object returned by ``urllib.request.urlopen``."""

    def __init__(self, payload: bytes, content_length: bool = True) -> None:
        self._buf = io.BytesIO(payload)
        self.headers = {"Content-Length": str(len(payload))} if content_length else {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


# downloads.py ---------------------------------------------------------------------


def test_download_file_verifies_checksums_and_reports_progress(tmp_path: Path, monkeypatch):
    payload = b"hello world" * 1000
    monkeypatch.setattr(
        downloads.urllib.request, "urlopen", lambda req, timeout: FakeResponse(payload)
    )
    seen: list[tuple[int, int | None]] = []
    import hashlib

    dest = tmp_path / "file.bin"
    result = downloads.download_file(
        "https://example.invalid/file.bin",
        dest,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_sha1="sha1:" + hashlib.sha1(payload).hexdigest().upper(),
        expected_md5=hashlib.md5(payload).hexdigest(),
        progress=lambda done, total: seen.append((done, total)),
    )
    assert dest.read_bytes() == payload
    assert result.size == len(payload) and result.attempts == 1
    assert seen and seen[-1] == (len(payload), len(payload))
    assert not dest.with_name("file.bin.part").exists()


def test_download_file_rejects_bad_sha256(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        downloads.urllib.request, "urlopen", lambda req, timeout: FakeResponse(b"data")
    )
    dest = tmp_path / "file.bin"
    with pytest.raises(downloads.ChecksumMismatch):
        downloads.download_file("https://example.invalid/x", dest, expected_sha256="00" * 32)
    assert not dest.exists() and not dest.with_name("file.bin.part").exists()


def test_download_file_retries_transient_errors(tmp_path: Path, monkeypatch):
    calls = {"n": 0}

    def flaky(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("boom")
        return FakeResponse(b"ok", content_length=False)

    monkeypatch.setattr(downloads.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(downloads.time, "sleep", lambda s: None)
    result = downloads.download_file("https://example.invalid/x", tmp_path / "f", attempts=3)
    assert result.attempts == 3 and (tmp_path / "f").read_bytes() == b"ok"


def test_download_file_gives_up_after_attempts(tmp_path: Path, monkeypatch):
    def always_fail(req, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(downloads.urllib.request, "urlopen", always_fail)
    monkeypatch.setattr(downloads.time, "sleep", lambda s: None)
    with pytest.raises(downloads.DownloadError):
        downloads.download_file("https://example.invalid/x", tmp_path / "f", attempts=2)


def test_file_digest_sha1(tmp_path: Path):
    f = tmp_path / "m.bin"
    f.write_bytes(b"abc")
    assert downloads.file_digest(f, "sha1") == "a9993e364706816aba3e25717850c26c9cd0d89d"


# ffmpeg.py: discovery ---------------------------------------------------------------


def test_platform_tag_normalises():
    assert ff.platform_tag("win32", "AMD64") == ("windows", "x86_64")
    assert ff.platform_tag("linux", "aarch64") == ("linux", "arm64")
    assert ff.platform_tag("darwin", "arm64") == ("macos", "arm64")


def test_find_ffmpeg_precedence(tmp_path: Path, monkeypatch):
    exe = ff.executable_name("ffmpeg")
    configured = tmp_path / "cfg" / exe
    configured.parent.mkdir()
    configured.write_bytes(b"")
    on_path = tmp_path / "path" / exe
    on_path.parent.mkdir()
    on_path.write_bytes(b"")
    state, code = tmp_path / "state", tmp_path / "code"
    monkeypatch.setenv("VIDEO_STORY_HOME", str(state))
    monkeypatch.setenv("VIDEO_STORY_ROOT", str(code))
    downloaded = ff.bundled_ffmpeg_path(state)
    bundled = ff.bundled_ffmpeg_path(code)
    for copy in (downloaded, bundled):
        copy.parent.mkdir(parents=True)
        copy.write_bytes(b"")
    assert ff.bundled_ffmpeg_path() == downloaded, "the state root is the default"

    monkeypatch.setattr(ff.shutil, "which", lambda name: str(on_path))
    assert ff.find_ffmpeg(Config(ffmpeg_path=configured)) == configured
    assert ff.find_ffmpeg(Config(ffmpeg_path=configured.parent)) == configured
    # Missing configured path falls through to PATH.
    assert ff.find_ffmpeg(Config(ffmpeg_path=tmp_path / "nope")) == on_path
    assert ff.find_ffmpeg(Config()) == on_path
    assert ff.ffmpeg_origin(Config(), on_path) == "PATH"
    assert ff.ffmpeg_origin(Config(ffmpeg_path=configured.parent), configured) == "ffmpeg_path"

    # Without PATH: the state root first, then a copy bundled with the code.
    monkeypatch.setattr(ff.shutil, "which", lambda name: None)
    assert ff.find_ffmpeg(Config()) == downloaded
    assert ff.ffmpeg_origin(Config(), downloaded) == "state folder"
    downloaded.unlink()
    assert ff.find_ffmpeg(Config()) == bundled
    assert ff.ffmpeg_origin(Config(), bundled) == "code folder"
    bundled.unlink()
    assert ff.find_ffmpeg(Config()) is None


def test_ensure_ffmpeg_raises_without_download(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VIDEO_STORY_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("VIDEO_STORY_ROOT", str(tmp_path / "code"))
    monkeypatch.setattr(ff.shutil, "which", lambda name: None)
    with pytest.raises(ff.FfmpegNotFound, match="setup") as raised:
        ff.ensure_ffmpeg(Config())
    # The message names both folders that were searched.
    assert str(tmp_path / "state") in str(raised.value)
    assert str(tmp_path / "code") in str(raised.value)


def test_ensure_ffmpeg_downloads_into_bundled_dir(tmp_path: Path, monkeypatch):
    """Downloads go to the state root, never next to the code."""
    monkeypatch.setenv("VIDEO_STORY_HOME", str(tmp_path))
    monkeypatch.setenv("VIDEO_STORY_ROOT", str(tmp_path / "code"))
    monkeypatch.setattr(ff.shutil, "which", lambda name: None)
    target: list[Path] = []

    def fake_download(target_dir: Path, progress=None) -> Path:
        target.append(target_dir)
        path = target_dir / ff.executable_name("ffmpeg")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
        return path

    monkeypatch.setattr(ff, "download_ffmpeg", fake_download)
    path = ff.ensure_ffmpeg(Config(), download=True)
    assert path == ff.bundled_ffmpeg_path(tmp_path) and target == [ff.bundled_ffmpeg_dir(tmp_path)]


# ffmpeg.py: download sources and extraction -------------------------------------------


def _btbn_release() -> dict:
    def asset(name: str, digest: str = "sha256:" + "ab" * 32) -> dict:
        return {
            "name": name,
            "browser_download_url": f"https://github.com/x/{name}",
            "size": 10,
            "digest": digest,
        }

    return {
        "assets": [
            asset("ffmpeg-master-latest-win64-gpl.zip"),
            asset("ffmpeg-n7.1-latest-win64-gpl-7.1.zip"),
            asset("ffmpeg-n8.1-latest-win64-gpl-shared-8.1.zip"),
            asset("ffmpeg-n8.1-latest-win64-gpl-8.1.zip", "sha256:" + "cd" * 32),
            asset("ffmpeg-n8.1-latest-win64-lgpl-8.1.zip"),
            asset("ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz"),
        ]
    }


def test_resolve_btbn_source_picks_newest_stable_gpl_asset(monkeypatch):
    monkeypatch.setattr(ff, "fetch_json", lambda url: _btbn_release())
    src = ff.resolve_btbn_source("windows", "x86_64")
    assert src.url.endswith("ffmpeg-n8.1-latest-win64-gpl-8.1.zip")
    assert src.sha256 == "cd" * 32 and src.kind == "zip" and src.size == 10
    linux = ff.resolve_btbn_source("linux", "x86_64")
    assert linux.kind == "tar" and linux.url.endswith("linux64-gpl-8.1.tar.xz")
    with pytest.raises(ff.FfmpegNotFound):
        ff.resolve_btbn_source("macos", "arm64")


def test_resolve_jvs_source_reads_md5(monkeypatch):
    monkeypatch.setattr(
        ff, "fetch_text", lambda url: f"7fa72b652e19bf84c9461e332ea1cdf3  {Path(url).name[:-4]}\n"
    )
    src = ff.resolve_jvs_source("linux", "x86_64")
    assert src.url == ff.JVS_BASE_URL + "ffmpeg-release-amd64-static.tar.xz"
    assert src.md5 == "7fa72b652e19bf84c9461e332ea1cdf3" and src.kind == "tar"
    monkeypatch.setattr(ff, "fetch_text", lambda url: "<html>oops</html>")
    with pytest.raises(downloads.DownloadError):
        ff.resolve_jvs_source("linux", "x86_64")


def _make_zip(path: Path, exe: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"ffmpeg-n8.1-latest-win64-gpl-8.1/bin/{exe}", b"FFMPEG")
        zf.writestr("ffmpeg-n8.1-latest-win64-gpl-8.1/bin/ffprobe.exe", b"FFPROBE")
        zf.writestr("ffmpeg-n8.1-latest-win64-gpl-8.1/doc/readme.txt", b"doc")
        zf.writestr("../evil/ffplay.exe", b"nope")


def _make_tar(path: Path) -> None:
    with tarfile.open(path, "w:xz") as tf:
        for name, data in (
            ("ffmpeg-7.0.2-amd64-static/ffmpeg", b"FFMPEG"),
            ("ffmpeg-7.0.2-amd64-static/ffprobe", b"FFPROBE"),
            ("ffmpeg-7.0.2-amd64-static/readme.txt", b"doc"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_extract_binaries_zip_and_tar(tmp_path: Path):
    archive = tmp_path / "a.zip"
    _make_zip(archive, "ffmpeg.exe")
    out = tmp_path / "out-zip"
    written = ff.extract_binaries(archive, "zip", out, ["ffmpeg.exe", "ffprobe.exe"])
    assert sorted(p.name for p in written) == ["ffmpeg.exe", "ffprobe.exe"]
    assert (out / "ffmpeg.exe").read_bytes() == b"FFMPEG"
    assert sorted(p.name for p in out.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]

    tar = tmp_path / "a.tar.xz"
    _make_tar(tar)
    out2 = tmp_path / "out-tar"
    written = ff.extract_binaries(tar, "tar", out2, ["ffmpeg", "ffprobe"])
    assert (out2 / "ffmpeg").read_bytes() == b"FFMPEG" and len(written) == 2


def test_download_ffmpeg_writes_provenance(tmp_path: Path, monkeypatch):
    os_name, arch = ff.platform_tag()
    exe = ff.executable_name("ffmpeg", os_name)
    kind = "zip" if os_name == "windows" else "tar"
    source = ff.FfmpegSource("test-source", f"https://example.invalid/ffmpeg.{kind}", kind)
    monkeypatch.setattr(ff, "candidate_sources", lambda o, a: [lambda: source])

    def fake_download(url, dest, **kwargs):
        if kind == "zip":
            _make_zip(dest, exe)
        else:
            _make_tar(dest)
        return downloads.DownloadResult(url, dest, 123, "s256", "s1", "m5", 1)

    monkeypatch.setattr(ff, "download_file", fake_download)
    monkeypatch.setattr(ff, "ffmpeg_version", lambda path: "8.1-test")
    target = tmp_path / "bundle"
    path = ff.download_ffmpeg(target)
    assert path == target / exe and path.read_bytes() == b"FFMPEG"
    prov = json.loads((target / ff.SOURCE_FILE_NAME).read_text(encoding="utf-8"))
    assert prov["url"] == source.url and prov["archive_sha256"] == "s256"
    assert prov["version"] == "8.1-test" and prov["archive_size"] == 123
    assert exe in prov["files"] and "downloaded_at" in prov
    # The downloaded archive is removed again.
    assert not any(p.is_dir() for p in target.iterdir())


def test_download_ffmpeg_falls_back_to_next_source(tmp_path: Path, monkeypatch):
    os_name, _ = ff.platform_tag()
    exe = ff.executable_name("ffmpeg", os_name)
    kind = "zip" if os_name == "windows" else "tar"
    good = ff.FfmpegSource("good", f"https://example.invalid/good.{kind}", kind)

    def broken():
        raise downloads.DownloadError("api down")

    monkeypatch.setattr(ff, "candidate_sources", lambda o, a: [broken, lambda: good])

    def fake_download(url, dest, **kwargs):
        (_make_zip if kind == "zip" else lambda d, e: _make_tar(d))(dest, exe)
        return downloads.DownloadResult(url, dest, 1, "x", "y", "z", 1)

    monkeypatch.setattr(ff, "download_file", fake_download)
    monkeypatch.setattr(ff, "ffmpeg_version", lambda path: "1")
    assert ff.download_ffmpeg(tmp_path / "b") == tmp_path / "b" / exe


def test_download_ffmpeg_reports_all_failures(tmp_path: Path, monkeypatch):
    def broken():
        raise downloads.DownloadError("api down")

    monkeypatch.setattr(ff, "candidate_sources", lambda o, a: [broken])
    with pytest.raises(ff.FfmpegNotFound, match="api down"):
        ff.download_ffmpeg(tmp_path / "b")
    monkeypatch.setattr(ff, "candidate_sources", lambda o, a: [])
    with pytest.raises(ff.FfmpegNotFound, match="not available"):
        ff.download_ffmpeg(tmp_path / "b")


# ffmpeg.py: conversion ---------------------------------------------------------------


def test_convert_to_16k_mono_command_and_errors(tmp_path: Path, monkeypatch):
    src = tmp_path / "audio.wav"
    dst = tmp_path / "audio16k.wav"
    src.write_bytes(b"RIFF")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        write_silence(dst, 0.1, 16000)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ff.subprocess, "run", fake_run)
    ff.convert_to_16k_mono(Path("ffmpeg"), src, dst)
    cmd = calls[0]
    assert cmd[:5] == ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    assert cmd[cmd.index("-i") + 1] == str(src) and cmd[-1] == str(dst)
    for flag, value in (("-ac", "1"), ("-ar", "16000"), ("-c:a", "pcm_s16le")):
        assert cmd[cmd.index(flag) + 1] == value
    assert "-protocol_whitelist" in cmd and "-vn" in cmd and "-y" in cmd

    def failing_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "line1\nInvalid data found\n")

    monkeypatch.setattr(ff.subprocess, "run", failing_run)
    with pytest.raises(ff.FfmpegError, match="Invalid data found"):
        ff.convert_to_16k_mono(Path("ffmpeg"), src, dst)

    def empty_run(cmd, **kwargs):
        dst.write_bytes(b"\x00" * 44)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ff.subprocess, "run", empty_run)
    with pytest.raises(ff.FfmpegError, match="no audio"):
        ff.convert_to_16k_mono(Path("ffmpeg"), src, dst)


def test_ffmpeg_version_parses_first_line(monkeypatch):
    monkeypatch.setattr(
        ff.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, "ffmpeg version 7.1-full_build Copyright\nbuilt with gcc\n", ""
        ),
    )
    assert ff.ffmpeg_version(Path("ffmpeg")) == "7.1-full_build"

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(ff.subprocess, "run", missing)
    assert ff.ffmpeg_version(Path("nope")) is None


# whisper_backend.py ------------------------------------------------------------------

CLI_JSON = {
    "params": {"model": "models/ggml-small.bin", "language": "auto", "translate": False},
    "result": {"language": "cs"},
    "transcription": [
        {
            "timestamps": {"from": "00:00:00,000", "to": "00:00:08,000"},
            "offsets": {"from": 0, "to": 8000},
            "text": " Dobrý den. Toto je zkouška.",
        },
        {
            "timestamps": {"from": "00:00:08,000", "to": "00:00:13,000"},
            "offsets": {"from": 8000, "to": 13000},
            "text": " Připravte prosím seznam úkolů.",
        },
    ],
}


def test_parse_whisper_cli_json():
    t = wb.parse_whisper_cli_json(CLI_JSON)
    assert t.language == "cs" and t.backend == "whisper-cli" and t.model == "ggml-small"
    assert t.segments == [
        Segment(0, 8000, "Dobrý den. Toto je zkouška."),
        Segment(8000, 13000, "Připravte prosím seznam úkolů."),
    ]
    empty = wb.parse_whisper_cli_json({"result": {"language": "auto"}, "transcription": []})
    assert empty.segments == [] and empty.language is None
    with pytest.raises(wb.TranscriptionError):
        wb.parse_whisper_cli_json({"transcription": [{"text": "x"}]})


def test_whisper_cli_backend_runs_and_parses(tmp_path: Path, monkeypatch):
    cli = tmp_path / "whisper-cli.exe"
    cli.write_bytes(b"")
    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"")
    wav = tmp_path / "audio16k.wav"
    write_silence(wav, 0.5, 16000)
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        Path(cmd[cmd.index("-of") + 1] + ".json").write_text(
            json.dumps(CLI_JSON, ensure_ascii=False), encoding="utf-8"
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(wb.subprocess, "run", fake_run)
    progress: list[float] = []
    t = wb.WhisperCliBackend(cli, model).transcribe(
        wav, language=None, threads=3, progress=progress.append
    )
    cmd = seen[0]
    assert cmd[0] == str(cli) and cmd[cmd.index("-m") + 1] == str(model)
    assert cmd[cmd.index("-l") + 1] == "auto" and cmd[cmd.index("-t") + 1] == "3"
    assert "-ng" in cmd and "-oj" in cmd and cmd[cmd.index("-f") + 1] == str(wav)
    assert t.language == "cs" and len(t.segments) == 2 and progress == [0.0, 1.0]

    monkeypatch.setattr(
        wb.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, "", "bad")
    )
    with pytest.raises(wb.TranscriptionError, match="exit code 2"):
        wb.WhisperCliBackend(cli, model).transcribe(wav, language="cs", threads=1)


class FakeSegment:
    def __init__(self, t0: int, t1: int, text: str) -> None:
        self.t0, self.t1, self.text = t0, t1, text


class FakeModel:
    instances: list[FakeModel] = []

    def __init__(self, model: str, **params) -> None:
        self.model = model
        self.params = params
        self.calls: list[dict] = []
        self._ctx = object()
        FakeModel.instances.append(self)

    def transcribe(self, audio, new_segment_callback=None, **params):
        self.calls.append({"samples": len(audio), **params})
        segs = [FakeSegment(0, 150, " Hello"), FakeSegment(150, 300, " world ")]
        if new_segment_callback is not None:
            for s in segs:
                new_segment_callback(s)
        return segs


def test_pywhispercpp_backend_lazy_load_and_units(tmp_path: Path, monkeypatch):
    model_path = tmp_path / "ggml-small.bin"
    model_path.write_bytes(b"")
    wav = tmp_path / "audio16k.wav"
    write_silence(wav, 3.0, 16000)
    FakeModel.instances.clear()
    monkeypatch.setattr(wb, "_import_model_class", lambda: FakeModel)
    monkeypatch.setattr(wb, "_detected_language", lambda model: "cs")

    backend = wb.PyWhisperCppBackend(model_path)
    assert FakeModel.instances == [], "model is loaded lazily"
    progress: list[float] = []
    streamed: list[Segment] = []
    t = backend.transcribe(
        wav, language="auto", threads=2, progress=progress.append, on_segment=streamed.append
    )
    assert streamed == [Segment(0, 1500, "Hello"), Segment(1500, 3000, "world")]
    t2 = backend.transcribe(wav, language="en", threads=2)
    assert len(FakeModel.instances) == 1, "model loaded once per backend"
    model = FakeModel.instances[0]
    assert model.params["n_threads"] == 2 and model.params["print_progress"] is False
    assert model.params["context_params"] == {"use_gpu": False}
    assert model.calls[0]["language"] == "auto" and model.calls[1]["language"] == "en"
    assert model.calls[0]["samples"] == 3 * 16000
    # centiseconds -> milliseconds, text stripped, detected language kept
    assert t.segments == [Segment(0, 1500, "Hello"), Segment(1500, 3000, "world")]
    assert t.language == "cs" and t.backend == "pywhispercpp" and t.model == "ggml-small"
    assert t2.language == "en"
    assert progress[0] == pytest.approx(0.5) and progress[-1] == 1.0


def test_pywhispercpp_backend_pads_short_audio(tmp_path: Path, monkeypatch):
    model_path = tmp_path / "m.bin"
    model_path.write_bytes(b"")
    wav = tmp_path / "short.wav"
    write_silence(wav, 0.2, 16000)
    FakeModel.instances.clear()
    monkeypatch.setattr(wb, "_import_model_class", lambda: FakeModel)
    monkeypatch.setattr(wb, "_detected_language", lambda model: None)
    wb.PyWhisperCppBackend(model_path).transcribe(wav, language=None, threads=1)
    samples = FakeModel.instances[0].calls[0]["samples"]
    assert samples == wb.MIN_AUDIO_MS * 16


def test_load_wav16k_rejects_wrong_rate(tmp_path: Path):
    wav = tmp_path / "x.wav"
    write_silence(wav, 0.1, 44100)
    with pytest.raises(wb.TranscriptionError, match="16000 Hz"):
        wb.load_wav16k(wav)
    good = tmp_path / "y.wav"
    write_silence(good, 0.1, 16000)
    data = wb.load_wav16k(good)
    assert data.dtype == np.float32 and len(data) == 1600


def test_create_backend_selection(tmp_path: Path):
    cfg = Config(whisper_model_path=tmp_path / "m.bin")
    assert isinstance(wb.create_backend(cfg), wb.PyWhisperCppBackend)
    with pytest.raises(wb.TranscriptionError, match="whisper_cli_path"):
        wb.create_backend(cfg.with_values(whisper_backend="whisper-cli"))
    cli_cfg = cfg.with_values(whisper_backend="whisper-cli", whisper_cli_path=str(tmp_path / "w"))
    backend = wb.create_backend(cli_cfg)
    assert isinstance(backend, wb.WhisperCliBackend) and backend.model_path == tmp_path / "m.bin"


# pipeline.py -------------------------------------------------------------------------


class FakeBackend:
    name = "fake"

    def __init__(self, segments: list[Segment] | None = None, language: str | None = "cs"):
        self.segments = segments if segments is not None else []
        self.language = language
        self.calls: list[dict] = []

    def transcribe(self, wav16k, *, language, threads, progress=None, on_segment=None):
        self.calls.append({"wav": wav16k, "language": language, "threads": threads})
        assert wav16k.exists()
        if progress:
            progress(0.5)
            if on_segment:
                on_segment(Segment(61_500, 63_000, "  hello   there "))
            progress(0.75)
            progress(1.0)
        return Transcript(language=self.language, segments=list(self.segments))


@pytest.fixture
def session(tmp_path: Path, monkeypatch) -> tuple[SessionPaths, Config]:
    cfg = Config(
        recording_folder=tmp_path / "rec",
        whisper_model_path=tmp_path / "ggml-small.bin",
        whisper_threads=2,
    )
    paths = SessionPaths(cfg.recording_folder, "20260101-120000")
    paths.create_work_dirs()
    write_silence(paths.audio_path(paths.work_dir), 1.0)
    monkeypatch.setattr(pipeline, "ensure_ffmpeg", lambda config, download=False: Path("ffmpeg"))
    monkeypatch.setattr(
        pipeline, "convert_to_16k_mono", lambda ffmpeg, src, dst: write_silence(dst, 1.0, 16000)
    )
    return paths, cfg


def test_transcribe_recording_writes_files_and_progress(session):
    paths, cfg = session
    backend = FakeBackend(
        [Segment(1500, 1000, " second "), Segment(0, 1200, "first"), Segment(1300, 1400, "  ")]
    )
    events: list[tuple[float, str]] = []
    result = pipeline.transcribe_recording(
        paths, cfg, progress=lambda f, m: events.append((f, m)), backend=backend
    )
    assert backend.calls == [
        {"wav": paths.audio_16k_path(paths.work_dir), "language": "auto", "threads": 2}
    ]
    assert not paths.audio_16k_path(paths.work_dir).exists(), "audio16k.wav is removed"
    # The end of a segment never lies past the end of the audio (1 s here).
    assert result.transcript.segments == [Segment(0, 1000, "first"), Segment(1500, 1500, "second")]
    assert result.transcript.language == "cs" and result.audio_duration_ms == 1000
    assert result.info.language == "cs" and result.info.segments == 2
    assert result.info.model == "ggml-small" and result.info.backend == "fake"
    assert result.info.duration_s is not None and result.info.duration_s >= 0
    txt = paths.transcript_txt_path(paths.work_dir).read_text(encoding="utf-8")
    assert txt.splitlines() == [
        "[00:00:00.000 --> 00:00:01.000] first",
        "[00:00:01.500 --> 00:00:01.500] second",
    ]
    stored = read_transcript(paths.transcript_json_path(paths.work_dir))
    assert stored.language == "cs" and stored.segments == result.transcript.segments
    assert stored.model == "ggml-small" and stored.backend == "fake"
    fractions = [f for f, _ in events]
    assert fractions == sorted(fractions) and fractions[0] == 0.0 and fractions[-1] == 1.0
    messages = [m for _, m in events]
    t = pipeline.TRANSCRIBING_MESSAGE
    assert messages[:3] == ["Preparing audio", t, t]
    # After whisper produced a segment: its start time and text on a second line.
    assert pipeline.TRANSCRIBING_MESSAGE + "\n00:01:01  hello there" in messages
    assert "Writing transcript" in set(messages)
    mid = [f for f, m in events if m.startswith(pipeline.TRANSCRIBING_MESSAGE)]
    assert len(mid) == 4 and all(0.05 <= f <= 0.98 for f in mid)


def test_transcribe_recording_uses_base_dir(session, tmp_path: Path):
    paths, cfg = session
    final = tmp_path / "final"
    paths.audio_dir(final).mkdir(parents=True)
    shutil.copy(paths.audio_path(paths.work_dir), paths.audio_path(final))
    result = pipeline.transcribe_recording(paths, cfg, base_dir=final, backend=FakeBackend())
    assert paths.transcript_json_path(final).exists()
    assert not paths.transcript_json_path(paths.work_dir).exists()
    assert result.transcript.segments == [] and result.info.segments == 0
    assert paths.transcript_txt_path(final).read_text(encoding="utf-8") == ""


def test_transcribe_recording_empty_audio(session):
    paths, cfg = session
    audio = paths.audio_path(paths.work_dir)
    write_silence(audio, 0.0)
    backend = FakeBackend()
    result = pipeline.transcribe_recording(paths, cfg, backend=backend)
    assert backend.calls == [], "backend is skipped for an empty recording"
    assert result.transcript.segments == [] and result.audio_duration_ms == 0
    assert result.transcript.language is None
    assert (
        json.loads(paths.transcript_json_path(paths.work_dir).read_text("utf-8"))["segments"] == []
    )


def test_transcribe_recording_missing_ffmpeg(session, monkeypatch):
    paths, cfg = session

    def no_ffmpeg(config, download=False):
        raise ff.FfmpegNotFound("missing")

    monkeypatch.setattr(pipeline, "ensure_ffmpeg", no_ffmpeg)
    with pytest.raises(ff.FfmpegNotFound):
        pipeline.transcribe_recording(paths, cfg, backend=FakeBackend())
    assert not paths.transcript_json_path(paths.work_dir).exists()


def test_transcribe_recording_cleans_up_on_backend_error(session):
    paths, cfg = session

    class Boom(FakeBackend):
        def transcribe(self, wav16k, **kwargs):
            raise wb.TranscriptionError("boom")

    with pytest.raises(wb.TranscriptionError):
        pipeline.transcribe_recording(paths, cfg, backend=Boom())
    assert not paths.audio_16k_path(paths.work_dir).exists()
