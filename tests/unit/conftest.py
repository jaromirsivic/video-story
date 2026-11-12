"""Unit-test fixtures shared by every module of this folder."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_automatic_archiving(monkeypatch):
    """Test recordings are dated long ago: without this, every editor a test opens
    would queue them for archiving and start real archive workers. Tests of the
    automatic archiving patch ``recordings_due_for_archive`` back themselves."""
    try:
        import video_story.editor.window as window
    except ImportError:  # pragma: no cover - Qt missing
        return
    monkeypatch.setattr(window, "recordings_due_for_archive", lambda *_a, **_k: [])


@pytest.fixture(autouse=True)
def _no_live_transcription(monkeypatch):
    """Recordings that tests start must not run a real whisper worker while recording
    (the developer's machine has the model). Tests of the live transcription patch
    ``live_transcription_available`` back themselves."""
    import video_story.host.controller as controller

    monkeypatch.setattr(controller, "live_transcription_available", lambda _c: "disabled in tests")


@pytest.fixture(autouse=True)
def _no_modal_process_question(monkeypatch):
    """Process on a recording that is not the green one asks in a modal box, which would
    hang an offscreen test: fail loudly instead. Tests that expect the question patch
    ``_confirm_process_not_latest`` on their window."""
    try:
        import video_story.editor.window as window
    except ImportError:  # pragma: no cover - Qt missing
        return

    def unexpected(_self) -> bool:
        raise AssertionError("unexpected 'process a recording that is not the latest' question")

    monkeypatch.setattr(window.EditorWindow, "_confirm_process_not_latest", unexpected)
