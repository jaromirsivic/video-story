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
