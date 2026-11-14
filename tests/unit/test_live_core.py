"""Live transcription: growing WAV files and the window logic (no whisper)."""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from video_story.core.transcript import Segment
from video_story.transcribe.growing_wav import GrowingMix, GrowingWav, read_format
from video_story.transcribe.live import (
    FRAME_MS,
    Cut,
    LiveCore,
    LiveParams,
    NoiseFloor,
    Window,
    build_prompt,
    commit_window,
    find_pauses,
    frame_db,
    has_speech,
    live_threads,
    plan_window,
)

RATE = 16000


def write_wav(path: Path, samples: np.ndarray, rate: int = RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.asarray(samples, dtype=np.int16).tobytes())


def speech(ms: int, rate: int = RATE, level: int = 8000, seed: int = 1) -> np.ndarray:
    """Noise in syllable-like bursts: 200 ms loud, 80 ms almost quiet."""
    rng = np.random.default_rng(seed)
    n = rate * ms // 1000
    envelope = np.where((np.arange(n) * 1000 // rate) % 280 < 200, level, 30)
    return (rng.standard_normal(n) * envelope).clip(-32768, 32767).astype(np.int16)


def silence(ms: int, rate: int = RATE, level: int = 30) -> np.ndarray:
    rng = np.random.default_rng(7)
    return (rng.standard_normal(rate * ms // 1000) * level).astype(np.int16)


# Growing WAV --------------------------------------------------------------------


def test_growing_wav_reads_a_file_while_it_is_written(tmp_path: Path) -> None:
    path = tmp_path / "audio.wav"
    data = speech(2000)
    writer = wave.open(str(path), "wb")
    writer.setnchannels(1)
    writer.setsampwidth(2)
    writer.setframerate(RATE)
    writer.writeframes(data[:8000].tobytes())
    writer._file.flush()  # what the wave module does when it patches the header
    try:
        growing = GrowingWav(path)
        assert growing.sample_rate == RATE
        assert growing.available_frames() == 8000
        writer.writeframes(data[8000:20000].tobytes())
        writer._file.flush()
        assert growing.available_frames() == 20000
        block = growing.read(7990, 20)
        assert np.array_equal(block, data[7990:8010].astype(np.int32))
        assert len(growing.read(19990, 100)) == 10  # only what is there
    finally:
        writer.close()


def test_growing_wav_ignores_a_stale_header_and_a_partial_frame(tmp_path: Path) -> None:
    path = tmp_path / "audio.wav"
    write_wav(path, speech(100))  # 1600 frames, header says so
    with open(path, "ab") as fh:
        fh.write(b"\x01\x00" * 400 + b"\x05")  # 400 more frames and half of one
    assert GrowingWav(path).available_frames() == 2000


def test_missing_or_incomplete_files_have_no_frames(tmp_path: Path) -> None:
    assert GrowingWav(tmp_path / "missing.wav").available_frames() == 0
    (tmp_path / "half.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVEfm")
    assert read_format(tmp_path / "half.wav") is None
    assert GrowingWav(tmp_path / "half.wav").available_frames() == 0


def test_growing_wav_follows_a_writer_thread(tmp_path: Path) -> None:
    path = tmp_path / "audio.wav"
    data = speech(1000)
    done = threading.Event()

    def write() -> None:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(RATE)
            for i in range(0, len(data), 1024):
                wf.writeframes(data[i : i + 1024].tobytes())
                time.sleep(0.002)
        done.set()

    thread = threading.Thread(target=write)
    thread.start()
    growing = GrowingWav(path)
    seen = []
    while not done.is_set():
        frames = growing.available_frames()
        if frames:
            assert np.array_equal(growing.read(0, frames)[:frames], data[:frames])
            seen.append(frames)
    thread.join()
    assert growing.available_frames() == len(data)
    assert seen == sorted(seen)


def test_mix_sums_tracks_and_waits_for_the_slower_one(tmp_path: Path) -> None:
    mic = np.full(RATE, 1000, dtype=np.int16)
    computer = np.full(RATE // 2, 500, dtype=np.int16)
    write_wav(tmp_path / "audio.wav", mic)
    write_wav(tmp_path / "computer.wav", computer)
    mix = GrowingMix(tmp_path / "audio.wav", tmp_path / "computer.wav")
    assert mix.available_frames() == RATE // 2
    assert mix.available_ms(safety_ms=100) == 400
    mixed, microphone = mix.read(RATE // 2 - 2, 4)
    assert mixed.tolist() == [1500, 1500, 1000, 1000]
    assert microphone.tolist() == [1000] * 4


def test_mix_ignores_a_computer_track_that_stopped(tmp_path: Path) -> None:
    write_wav(tmp_path / "audio.wav", np.zeros(RATE * 7, dtype=np.int16))
    write_wav(tmp_path / "computer.wav", np.zeros(RATE, dtype=np.int16))
    mix = GrowingMix(tmp_path / "audio.wav", tmp_path / "computer.wav")
    assert mix.available_frames() == RATE * 7


def test_mix_clips_and_maps_another_rate(tmp_path: Path) -> None:
    write_wav(tmp_path / "audio.wav", np.full(RATE, 30000, dtype=np.int16))
    write_wav(tmp_path / "computer.wav", np.full(RATE * 2, 10000, dtype=np.int16), rate=RATE * 2)
    mix = GrowingMix(tmp_path / "audio.wav", tmp_path / "computer.wav")
    assert mix.available_frames() == RATE
    mixed, _ = mix.read(0, 3)
    assert mixed.tolist() == [32767] * 3


def test_mix_without_a_computer_track(tmp_path: Path) -> None:
    write_wav(tmp_path / "audio.wav", np.full(100, 7, dtype=np.int16))
    mix = GrowingMix(tmp_path / "audio.wav", tmp_path / "computer.wav")
    assert mix.available_frames() == 100
    assert mix.read(0, 2)[0].tolist() == [7, 7]


# Energy, pauses -------------------------------------------------------------------


def test_frame_db_and_pauses() -> None:
    audio = np.concatenate([speech(1000), silence(600), speech(400)])
    db = frame_db(audio, RATE)
    assert len(db) == 2000 // FRAME_MS
    floor = NoiseFloor(LiveParams())
    floor.add(db)
    threshold = floor.threshold()
    assert db[:50].max() > threshold > db[50:80].max()
    assert find_pauses(db, threshold, 20) == [(50, 80)]
    assert find_pauses(db, threshold, 31) == []
    assert has_speech(db, threshold, LiveParams())
    assert not has_speech(frame_db(silence(2000), RATE), threshold, LiveParams())


def test_digital_silence_is_never_speech() -> None:
    db = frame_db(np.zeros(RATE, dtype=np.int16), RATE)
    floor = NoiseFloor(LiveParams())
    assert not has_speech(db, floor.threshold(db), LiveParams())


def db_with(pauses: list[tuple[int, int]], total_ms: int = 31_000) -> np.ndarray:
    """Frame energies: -20 dB speech with quiet (-80 dB) ``pauses`` (ms ranges)."""
    db = np.full(total_ms // FRAME_MS, -20.0)
    for a, b in pauses:
        db[a // FRAME_MS : b // FRAME_MS] = -80.0
    return db


def test_plan_prefers_the_longest_late_pause() -> None:
    params = LiveParams()
    db = db_with([(14_000, 15_500), (22_000, 22_600), (27_000, 27_800)])
    window = plan_window(5_000, 40_000, db, db, -50, -50, params)
    assert window == Window(5_000, 5_000 + 27_400, Cut.PAUSE)


def test_plan_falls_back_to_an_early_pause_then_the_microphone_then_a_hard_cut() -> None:
    params = LiveParams()
    early = db_with([(13_000, 14_000)])
    assert plan_window(0, 30_000, early, early, -50, -50, params) == Window(0, 13_500, Cut.PAUSE)
    loud = db_with([])
    assert plan_window(0, 30_000, loud, early, -50, -50, params) == Window(0, 13_500, Cut.MIC_PAUSE)
    assert plan_window(0, 30_000, loud, loud, -50, -50, params) == Window(0, 30_000, Cut.HARD)


def test_plan_waits_for_a_full_window_unless_early_cuts_are_on() -> None:
    db = db_with([(15_000, 16_000)], total_ms=20_000)
    assert plan_window(0, 20_000, db, db, -50, -50, LiveParams()) is None
    early = LiveParams(early_cut=True)
    assert plan_window(0, 20_000, db, db, -50, -50, early) == Window(0, 15_500, Cut.PAUSE)
    assert plan_window(0, 11_000, db, db, -50, -50, early) is None


def test_plan_takes_the_rest_at_the_end() -> None:
    db = db_with([], total_ms=8_000)
    assert plan_window(0, 8_000, db, db, -50, -50, LiveParams(), final=True) == Window(
        0, 8_000, Cut.END
    )
    assert plan_window(8_000, 8_000, db, db, -50, -50, LiveParams(), final=True) is None


# Commit -------------------------------------------------------------------------


def test_commit_in_a_pause_keeps_everything() -> None:
    window = Window(10_000, 38_000, Cut.PAUSE)
    segments = [
        Segment(9_000, 14_000, " Ahoj "),
        Segment(15_000, 39_000, "světe"),
        Segment(1, 2, ""),
    ]
    commit = commit_window(window, segments, 10_000, LiveParams())
    assert commit.segments == [Segment(10_000, 14_000, "Ahoj"), Segment(15_000, 38_000, "světe")]
    assert commit.next_ms == 38_000


def test_commit_after_a_hard_cut_redoes_the_cut_segment() -> None:
    window = Window(0, 30_000, Cut.HARD)
    segments = [Segment(0, 10_000, "a"), Segment(10_000, 20_000, "b"), Segment(20_100, 30_000, "c")]
    commit = commit_window(window, segments, 0, LiveParams())
    assert [s.text for s in commit.segments] == ["a", "b"]
    assert commit.next_ms == 20_000  # never before the last kept segment ends


def test_commit_after_a_hard_cut_with_one_segment_keeps_it() -> None:
    window = Window(0, 30_000, Cut.HARD)
    commit = commit_window(window, [Segment(0, 30_000, "dlouhá věta")], 0, LiveParams())
    assert len(commit.segments) == 1 and commit.next_ms == 30_000


def test_commit_drops_what_was_stored_before() -> None:
    window = Window(20_000, 45_000, Cut.PAUSE)
    segments = [Segment(19_000, 20_400, "už mám"), Segment(20_500, 30_000, "nové")]
    commit = commit_window(window, segments, 20_500, LiveParams())
    assert [s.text for s in commit.segments] == ["nové"]


# Prompt, threads ------------------------------------------------------------------


def test_prompt_is_the_end_of_the_text_from_a_sentence_start() -> None:
    texts = ["Tohle je první věta, která je docela dlouhá a nic moc neříká."] * 2 + [
        "Druhá věta pokračuje. Třetí věta je poslední a končí tady."
    ]
    prompt = build_prompt(texts, max_chars=80)
    assert prompt == "Druhá věta pokračuje. Třetí věta je poslední a končí tady."
    assert build_prompt([]) is None
    assert build_prompt(["  "]) is None


def test_prompt_is_left_out_after_a_repetition_loop() -> None:
    assert build_prompt(["Děkuji.", "děkuji.", "Děkuji."]) is None
    assert build_prompt(["a", "b", "a"]) == "a b a"


@pytest.mark.parametrize(
    ("mode", "cpus", "threads"),
    [("light", 32, 8), ("light", 8, 2), ("light", 2, 2), ("faster", 32, 16), ("faster", 8, 4)],
)
def test_live_threads(mode: str, cpus: int, threads: int) -> None:
    assert live_threads(mode, cpus) == threads


# LiveCore with a fake backend ------------------------------------------------------


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe_samples(self, samples, *, language, threads, initial_prompt=None):
        self.calls.append(
            {"ms": len(samples) * 1000 // RATE, "language": language, "prompt": initial_prompt}
        )
        n = len(self.calls)
        return [Segment(0, len(samples) * 1000 // RATE, f"věta číslo {n} je dost dlouhá")], "cs"


def test_core_skips_silence_locks_the_language_and_can_carry_a_prompt(tmp_path: Path) -> None:
    audio = np.concatenate(
        [speech(20_000), silence(1_000), speech(9_000), silence(45_000), speech(5_000)]
    )
    write_wav(tmp_path / "audio.wav", audio)
    backend = FakeBackend()
    core = LiveCore(
        GrowingMix(tmp_path / "audio.wav"),
        backend,
        lambda s, rate: s,
        threads=3,
        params=LiveParams(use_prompt=True),
    )
    total = core.mix.available_ms()
    assert core.next_window(25_000) is None  # waits for a full window
    results = []
    while core.done_ms < total:
        window = core.next_window(total, final=True)
        assert window is not None
        results.append(core.transcribe(window))
    assert [r.window.cut for r in results] == [Cut.PAUSE, Cut.PAUSE, Cut.PAUSE, Cut.END]
    assert [r.window.end_ms for r in results] == [20_500, 41_500, 62_500, 80_000]
    assert [r.speech for r in results] == [True, True, False, True]
    assert len(backend.calls) == 3
    assert backend.calls[0]["language"] == "auto" and backend.calls[0]["prompt"] is None
    assert backend.calls[1]["language"] == "cs"
    assert backend.calls[1]["prompt"] == "věta číslo 1 je dost dlouhá"
    assert core.language == "cs" and core.done_ms == total


# Resuming in the editor ------------------------------------------------------------


def test_resume_context_keeps_the_language_and_prompts_with_the_text_before(
    tmp_path: Path,
) -> None:
    from video_story.core.session import SessionPaths
    from video_story.core.transcript import Transcript, write_transcript
    from video_story.transcribe.pipeline import resume_context

    paths = SessionPaths(tmp_path, "20261111-120000")
    base = tmp_path / "20261111-120000"
    base.mkdir()
    assert resume_context(paths, base, 5_000) == (None, None)
    transcript = Transcript(
        language="cs",
        segments=[Segment(0, 4_000, "Dobrý den."), Segment(4_000, 9_000, "Tohle je test.")],
        complete=False,
        transcribed_ms=9_000,
    )
    write_transcript(paths.transcript_txt_path(base), paths.transcript_json_path(base), transcript)
    assert resume_context(paths, base, 9_000) == ("cs", None)
    assert resume_context(paths, base, 0) == (None, None)
    transcript.complete = True
    write_transcript(paths.transcript_txt_path(base), paths.transcript_json_path(base), transcript)
    assert resume_context(paths, base, 9_000) == (None, None)


def test_transcribe_audio_passes_language_and_prompt_to_the_backend(tmp_path: Path) -> None:
    from video_story.core.config import Config
    from video_story.core.session import SessionPaths
    from video_story.core.transcript import Transcript
    from video_story.transcribe import pipeline
    from video_story.transcribe.ffmpeg import find_ffmpeg

    if find_ffmpeg(Config()) is None:
        pytest.skip("FFmpeg is not installed")
    paths = SessionPaths(tmp_path, "20261111-120000")
    base = tmp_path / "20261111-120000"
    write_wav(paths.audio_path(base), speech(3_000))
    seen: dict = {}

    class Backend:
        name = "fake"

        def transcribe(self, wav, *, language, threads, progress=None, on_segment=None, **kw):
            seen.update(language=language, **kw)
            return Transcript(language=None, segments=[Segment(0, 500, "dál")])

    result = pipeline.transcribe_audio(
        paths,
        Config(),
        base_dir=base,
        start_ms=1_000,
        backend=Backend(),
        language="cs",
        initial_prompt="Předtím.",
    )
    assert seen == {"language": "cs", "initial_prompt": "Předtím."}
    assert result.transcript.language == "cs"
    assert result.transcript.segments == [Segment(1_000, 1_500, "dál")]


def test_core_gives_no_prompt_by_default(tmp_path: Path) -> None:
    write_wav(tmp_path / "audio.wav", np.concatenate([speech(20_000), silence(1_000)] * 3))
    backend = FakeBackend()
    core = LiveCore(GrowingMix(tmp_path / "audio.wav"), backend, lambda s, rate: s, threads=3)
    total = core.mix.available_ms()
    while core.done_ms < total:
        core.transcribe(core.next_window(total, final=True))
    assert len(backend.calls) >= 2
    assert all(call["prompt"] is None for call in backend.calls)
    assert backend.calls[1]["language"] == "cs"
