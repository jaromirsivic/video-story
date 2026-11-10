"""GUI process entry point (``video-story ui --session ID``)."""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from video_story.core.config import load_config
from video_story.core.session import SessionPaths, new_session_id
from video_story.platform import get_platform

log = logging.getLogger(__name__)


def _housekeeping_in_background(config, session_id: str) -> None:
    """Finish recordings a dead window left behind, without delaying the window. Old
    recordings are not touched here: the editor archives them when it starts."""
    from video_story.host.recovery import recover_orphaned_recordings

    def work() -> None:
        try:
            recovered = recover_orphaned_recordings(config.recording_folder)
        except Exception:  # pragma: no cover - housekeeping must never break the window
            log.exception("recovery of left-behind recordings failed")
        else:
            if recovered:
                log.info("recovered recordings: %s", ", ".join(recovered))

    threading.Thread(target=work, name="housekeeping", daemon=True).start()


def run_ui(session_id: str | None = None, config_file: Path | None = None) -> int:
    platform = get_platform()
    platform.prepare_process()  # DPI awareness must be set before Qt starts

    from PySide6.QtWidgets import QApplication, QMessageBox

    from video_story.host.controller import SessionController
    from video_story.ui.control_window import ControlWindow
    from video_story.ui.icons import install_app_icon

    config = load_config(config_file)
    status = platform.support_status()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Video Story")
    install_app_icon(app, "VideoStory.Recorder")
    app.setQuitOnLastWindowClosed(False)

    config.recording_folder.mkdir(parents=True, exist_ok=True)
    session_id = session_id or new_session_id(config.recording_folder)
    paths = SessionPaths(config.recording_folder, session_id)
    controller = SessionController(config, platform, paths)
    if not status.supported:
        controller.fail(status.reason, code="UNSUPPORTED_PLATFORM")
        QMessageBox.critical(None, "Video Story", status.reason)
        return 3

    _housekeeping_in_background(config, session_id)

    final_status = {"value": "cancelled"}

    def finished(status_name: str) -> None:
        final_status["value"] = status_name
        controller.release_lock()
        app.quit()

    def overlay_factory():
        from video_story.ui.overlay import DrawingOverlay

        return DrawingOverlay()

    def preflight() -> list[str]:
        from video_story.transcribe.pipeline import missing_requirements

        return missing_requirements(controller.config)

    window = ControlWindow(
        controller,
        on_finished=finished,
        overlay_factory=overlay_factory,
        config_file=config_file,
        preflight=preflight,
    )
    window.show()
    window.raise_()
    window.activateWindow()
    log.info("control window shown for session %s", session_id)
    code = app.exec()
    log.info("session %s finished with %s", session_id, final_status["value"])
    return 0 if final_status["value"] in {"processed", "cancelled", "editing"} else code or 1


__all__ = ["run_ui"]
