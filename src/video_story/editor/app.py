"""Editor process entry point (``video-story edit`` starts this detached).

Only one editor runs per recording folder: the running instance holds an OS lock on
``<recording_folder>/.work/editor.lock`` (see :mod:`video_story.host.editor_lock`);
a second start raises the existing window and fails with ``ALREADY_RUNNING``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from video_story.core.config import load_config
from video_story.host.editor_launcher import check_editor_session, editor_lock_path
from video_story.host.editor_lock import EditorLock, lock_holder_pid
from video_story.host.editor_session import write_editor_event
from video_story.host.runner import SessionError
from video_story.platform import get_platform

log = logging.getLogger(__name__)

WINDOW_TITLE = "Video Story Editor"


def bring_existing_to_front(title: str = WINDOW_TITLE) -> bool:
    """Raise the window of the already running editor (Windows only; best effort)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        handle = user32.FindWindowW(None, title)
        if not handle:
            return False
        SW_RESTORE = 9
        user32.ShowWindow(handle, SW_RESTORE)
        user32.SetForegroundWindow(handle)
        return True
    except (AttributeError, OSError):
        return False


def run_editor(
    session_id: str | None = None,
    config_file: Path | None = None,
    editor_session: Path | None = None,
    recording_session: str | None = None,
) -> int:
    """Run the editor window; raises :class:`SessionError` when it cannot start.

    ``editor_session`` is the event file the agent waits on (``wait --editor``);
    ``recording_session`` is a recording session in status ``editing`` that the editor
    finishes (Process) or cancels (close) - see :mod:`video_story.host.editor_session`.

    ``ALREADY_RUNNING`` (another editor holds the lock; its window is raised first),
    ``NO_RECORDINGS``, ``UNKNOWN_SESSION`` and ``INVALID_SESSION`` as for ``--detach``.
    """
    platform = get_platform()
    platform.prepare_process()  # DPI awareness must be set before Qt starts

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from video_story.editor.window import EditorWindow

    config = load_config(config_file)
    config.recording_folder.mkdir(parents=True, exist_ok=True)
    check_editor_session(config, session_id)
    lock_path = editor_lock_path(config)
    lock = EditorLock(lock_path)
    if not lock.acquire():
        holder = lock_holder_pid(lock_path)
        log.info("editor already running (pid %s); raising its window", holder)
        bring_existing_to_front()
        who = f" (pid {holder})" if holder else ""
        raise SessionError("ALREADY_RUNNING", f"the Video Story Editor is already open{who}")

    if editor_session is not None:
        write_editor_event(editor_session, "open", pid=os.getpid())

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Video Story Editor")
    app.setOrganizationName("VideoStory")
    settings = QSettings("VideoStory", "Editor")
    try:
        window = EditorWindow(
            config,
            session_id=session_id,
            settings=settings,
            editor_session=editor_session,
            recording_session=recording_session,
        )
        window.restore_settings()
        window.show()
        window.raise_()
        window.activateWindow()
        log.info("editor shown (session %s)", session_id or "newest")
        return app.exec()
    finally:
        lock.release()


__all__ = ["WINDOW_TITLE", "bring_existing_to_front", "run_editor"]
