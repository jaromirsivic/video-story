"""The audio header of the editor: microphone / computer audio switches and the pen that
transcribes again (offscreen Qt; the whisper worker is the scripted fake)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import wave  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
from test_editor import DURATION_MS, NEWEST, SAMPLE_RATE, make_recording  # noqa: E402
from test_editor_transcription import (  # noqa: E402
    DONE,
    _open,
    _spec,
    _starts,
    _transcript,
    _user_click,
    _wait_complete,
)

from video_story.core.audio_mix import COMPUTER, MICROPHONE, TrackSelection, read_selection  # noqa: E402
from video_story.core.session import SessionPaths  # noqa: E402
from video_story.editor.timeline.view import (  # noqa: E402
    AUDIO_ICON_TOOLTIPS,
    AUDIO_ICONS,
    HEADER_W,
    TRANSCRIBE,
)

SEGMENTS = [
    {"event": "segment", "start_ms": 0, "end_ms": 900, "text": "znovu"},
    {"event": "progress", "fraction": 1.0, "transcribed_ms": 900},
    DONE,
]


def add_computer_track(paths: SessionPaths, amplitude: int = 5000) -> Path:
    path = paths.computer_audio_path(paths.final_dir)
    t = np.arange(SAMPLE_RATE * 2) / SAMPLE_RATE
    samples = (np.sin(2 * np.pi * 220 * t) * amplitude).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes())
    return path


def test_header_has_the_three_icons_after_the_fit_toggle(qtbot, tmp_path: Path):
    paths = make_recording(tmp_path, NEWEST)
    add_computer_track(paths)
    window = _open(qtbot, tmp_path, _spec(tmp_path, SEGMENTS))
    timeline = window.timeline
    rects = timeline.audio_icon_rects()
    assert list(rects) == list(AUDIO_ICONS) == [MICROPHONE, COMPUTER, TRANSCRIBE]
    audio = timeline.audio_rect()
    left = audio.x() + 6 + 20  # behind the fit toggle
    for rect in rects.values():
        assert rect.left() > left and rect.right() < HEADER_W and audio.contains(rect)
        left = rect.right()
    assert timeline.audio_icon_state(MICROPHONE) is True
    assert timeline.audio_icon_state(COMPUTER) is True
    assert "click to leave it out" in timeline.audio_icon_tooltip(COMPUTER)
    assert timeline.audio_icon_tooltip(TRANSCRIBE) == AUDIO_ICON_TOOLTIPS[(TRANSCRIBE, True)]
    # Both tracks are on: the player and the waveform get the sum of both.
    assert window.recording.audio is not None and len(window.recording.audio.sources) == 2


def test_recording_without_computer_audio_shows_a_dim_speaker(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST)
    window = _open(qtbot, tmp_path, _spec(tmp_path, SEGMENTS))
    timeline = window.timeline
    assert timeline.audio_icon_state(COMPUTER) is None
    assert timeline.audio_icon_tooltip(COMPUTER) == AUDIO_ICON_TOOLTIPS[(COMPUTER, None)]
    assert "No computer audio" in timeline.audio_icon_tooltip(COMPUTER)
    asked: list[bool] = []
    window._confirm_transcribe_again = lambda _rec: asked.append(True) or False
    _user_click(timeline, timeline.audio_icon_rects()[COMPUTER].center())
    assert asked == [] and window.recording.selection == TrackSelection()  # nothing happened


def test_switching_a_track_changes_the_mix_and_asks_to_transcribe_again(qtbot, tmp_path: Path):
    paths = make_recording(tmp_path, NEWEST)
    computer = add_computer_track(paths)
    window = _open(qtbot, tmp_path, _spec(tmp_path, SEGMENTS))
    timeline = window.timeline
    asked: list[str] = []

    def decline(recording) -> bool:
        asked.append(window.transcribe_again_text(recording))
        return False

    window._confirm_transcribe_again = decline
    _user_click(timeline, timeline.audio_icon_rects()[MICROPHONE].center())
    assert read_selection(paths, paths.final_dir) == TrackSelection(microphone=False)
    assert window.recording.audio.sources == (computer,)
    assert window.player._audio is window.recording.audio  # the player hears the new mix
    assert timeline.audio_icon_state(MICROPHONE) is False
    assert "click to add it" in timeline.audio_icon_tooltip(MICROPHONE)
    assert len(asked) == 1
    assert "Microphone: not included" in asked[0] and "Computer audio: included" in asked[0]
    # Declined: the narration stays as it was.
    assert [s.text for s in _transcript(tmp_path).segments] == ["hello world", "second line"]
    assert window.recording.transcript_complete

    _user_click(timeline, timeline.audio_icon_rects()[COMPUTER].center())
    assert read_selection(paths, paths.final_dir) == TrackSelection(False, False)
    assert window.recording.audio.sources == ()  # silence of the full length
    assert window.recording.audio.frames == SAMPLE_RATE * DURATION_MS // 1000  # the longest
    assert "the narration will be empty" in asked[-1]


def test_pen_transcribes_again_from_the_start(qtbot, tmp_path: Path):
    paths = make_recording(tmp_path, NEWEST)
    add_computer_track(paths)
    spec = _spec(tmp_path, SEGMENTS)
    window = _open(qtbot, tmp_path, spec)
    assert window.process_button.isEnabled()
    window._confirm_transcribe_again = lambda _rec: True
    _user_click(window.timeline, window.timeline.audio_icon_rects()[TRANSCRIBE].center())
    # The old narration is gone at once and the buttons wait for the new one.
    assert not window.recording.transcript_complete
    assert window.recording.segments == []
    assert not window.process_button.isEnabled()
    _wait_complete(qtbot, window)
    assert [s.text for s in _transcript(tmp_path).segments] == ["znovu"]
    assert _starts(tmp_path) == [(NEWEST, 0)]
    qtbot.waitUntil(lambda: window.process_button.isEnabled(), timeout=5000)


def test_transcribe_again_restarts_a_running_transcription(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST, with_transcript=False)
    spec = _spec(
        tmp_path,
        [
            {"event": "segment", "start_ms": 0, "end_ms": 500, "text": "first run"},
            {"event": "progress", "fraction": 0.2, "transcribed_ms": 500},
            {"sleep": 30},
            DONE,
        ],
    )
    window = _open(qtbot, tmp_path, spec)
    qtbot.waitUntil(lambda: window.recording.segments != [], timeout=10_000)
    # The second run of the worker finishes at once.
    spec.write_text(
        spec.read_text(encoding="utf-8").replace('{"sleep": 30}, ', ""), encoding="utf-8"
    )
    window._confirm_transcribe_again = lambda _rec: True
    assert window.request_transcribe_again()
    assert window.recording.segments == []
    _wait_complete(qtbot, window)
    assert _starts(tmp_path) == [(NEWEST, 0), (NEWEST, 0)]  # started over, not resumed
    assert [s.text for s in _transcript(tmp_path).segments] == ["first run"]


def test_archived_recording_has_no_audio_icons(qtbot, tmp_path: Path):
    from video_story.core.archive_state import ArchiveState, ArchiveStatus, write_archive_state

    paths = make_recording(tmp_path, NEWEST)
    state = ArchiveState(status=ArchiveStatus.ARCHIVED, screens=["screen1"])
    write_archive_state(paths, state, paths.final_dir)
    window = _open(qtbot, tmp_path, _spec(tmp_path, SEGMENTS))
    assert window.recording.archived
    assert window.timeline.audio_icon_rects() == {}
    assert not window.request_transcribe_again()


def test_arrow_follows_whisper_through_silence(qtbot, tmp_path: Path):
    """No segment comes while nothing is said (e.g. only the microphone switched on and
    the sound was on the computer track): the arrow still moves with whisper's progress
    and never jumps back when a segment that ends earlier arrives."""
    make_recording(tmp_path, NEWEST, with_transcript=False)  # 2.5 s long
    spec = _spec(
        tmp_path,
        [
            {"event": "segment", "start_ms": 0, "end_ms": 200, "text": "ahoj"},
            {"event": "progress", "fraction": 0.1, "transcribed_ms": 200},
            {"event": "progress", "fraction": 0.8, "transcribed_ms": 200},
            {"event": "segment", "start_ms": 1000, "end_ms": 1500, "text": "konec"},
            {"sleep": 30},
            DONE,
        ],
    )
    window = _open(qtbot, tmp_path, spec)
    qtbot.waitUntil(lambda: len(window.recording.segments) == 2, timeout=10_000)
    # 80 % of 2.5 s = 2000 ms, although the latest segment ends at 1500 ms.
    assert window.timeline.transcription == ("transcribing", 2000)
    assert window.recording.transcribed_ms == 1500  # a resume still starts there
    window.transcription.shutdown()
