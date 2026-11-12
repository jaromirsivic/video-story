"""The small always-on-top control window: Record/Stop, Settings, Draw.

While recording, **Settings** gives its place to **Keypoint**: a click marks the
current moment in the recording (``manifest.json`` ``keypoints``; the editor shows them
as green lines that the crop handles snap to) and the button counts down 3, 2, 1 before
the next keypoint can be inserted. The text clipboard is tracked meanwhile
(:mod:`video_story.ui.clipboard`): every new text copied during the recording lands in
the frame sidecars of the next tick.

Keyboard shortcuts while recording (shown in brackets on the buttons and in their
tooltips; system-wide on Windows, see :mod:`video_story.ui.hotkeys`): Ctrl+Alt+S Stop,
Ctrl+Alt+K Keypoint, Ctrl+Space or Ctrl+F1 Draw. Leaving drawing mode - Esc, the Draw
shortcut or button again, the toolbar's cross - also clears what was drawn, so the next
drawing starts on an empty screen.

Two small icon buttons follow Draw: the light / dark theme of the window (the editor's
sun and moon icons; remembered in the recorder's settings) and **Hide to Tray**, which
hides the window behind a tray icon (the application's camera icon) and says in a
balloon how to stop; a left or right click on the icon brings the window back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from video_story.capture.recorder import low_disk_message
from video_story.core.timeline import format_timestamp
from video_story.editor.theme import apply_theme, system_prefers_dark
from video_story.host.controller import SessionController
from video_story.host.editor_launcher import launch_editor

from .clipboard import ClipboardTracker
from .hotkeys import Hotkey, RecordingHotkeys
from .icons import (
    app_icon,
    draw_icon,
    hide_to_tray_icon,
    keypoint_icon,
    moon_icon,
    record_icon,
    settings_icon,
    stop_icon,
    sun_icon,
)
from .settings_dialog import SettingsDialog
from .stop_dialog import StopChoice, StopDialog, confirm_discard

log = logging.getLogger(__name__)

UI_TICK_MS = 200
STOP_SHORTCUT = "Ctrl+Alt+S"
KEYPOINT_SHORTCUT = "Ctrl+Alt+K"
DRAW_SHORTCUT = "Ctrl+Space"
DRAW_ALT_SHORTCUT = "Ctrl+F1"  # also toggles drawing (Ctrl+Space may be taken)
STOP_LABEL = "Stop"
KEYPOINT_LABEL = "Keypoint"
DRAW_LABEL = "Draw"
THEME_SETTING = "view/dark"
TRAY_BALLOON_MS = 8000
KEYPOINT_COOLDOWN_S = 3  # the button counts 3, 2, 1 before the next keypoint
SAVED_NOTE = "What was recorded so far is saved and opens in the Video Story Editor."


class ProcessingWorker(QObject):
    """Saves the recording (stop, manifest, final folder) in a QThread."""

    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, controller: SessionController) -> None:
        super().__init__()
        self._controller = controller

    def run(self) -> None:
        try:
            final_dir = self._controller.process()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(str(final_dir))


class ControlWindow(QWidget):
    """Main window. ``on_finished`` is called with the final status when the session ends.

    However a recording ends - Stop and **Video Editor**, the auto stop, low disk space,
    a capture failure - it is saved and opened in the Video Story Editor; nothing is
    handed to the agent from here (only the editor's **Process** button does that).

    ``preflight`` returns what the transcription would miss (the Whisper model, FFmpeg);
    it is asked when Record is pressed so that the user hears about it at once.
    """

    def __init__(
        self,
        controller: SessionController,
        on_finished: Callable[[str], None],
        overlay_factory: Callable[[], QObject] | None = None,
        config_file: Path | None = None,
        preflight: Callable[[], list[str]] | None = None,
        system_wide_hotkeys: bool | None = None,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings  # remembers the theme (None: not remembered)
        self._tray: QSystemTrayIcon | None = None
        self._dark = system_prefers_dark()
        if settings is not None and settings.contains(THEME_SETTING):
            self._dark = bool(settings.value(THEME_SETTING, self._dark, type=bool))
            apply_theme(self._dark)
        self.controller = controller
        self._on_finished = on_finished
        self._overlay_factory = overlay_factory
        self._config_file = config_file
        self._preflight = preflight
        self._overlay: QObject | None = None
        self._processing = False
        self._halting = False  # stopping by itself: a notice is on screen, no closing
        self._done = False
        self._thread: QThread | None = None
        self._worker: ProcessingWorker | None = None
        self._stop_dialog: StopDialog | None = None

        self.setWindowTitle("Video Story")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        row = QHBoxLayout()
        row.setSpacing(8)

        self.record_button = QPushButton(record_icon(), "Record")
        self.record_button.setToolTip("Start recording audio, screens and cursor")
        self.record_button.clicked.connect(self._on_record_or_stop)
        row.addWidget(self.record_button)

        self.settings_button = QPushButton(settings_icon(), "Settings")
        self.settings_button.setToolTip(
            "Recording folder, capture interval, image format, audio input"
        )
        self.settings_button.clicked.connect(self._open_settings)
        row.addWidget(self.settings_button)

        # Takes the place of Settings while recording (see the module docstring).
        self.keypoint_button = QPushButton(
            keypoint_icon(), _with_shortcut(KEYPOINT_LABEL, KEYPOINT_SHORTCUT)
        )
        self.keypoint_button.setToolTip(
            f"Mark this moment of the recording as a keypoint ({KEYPOINT_SHORTCUT}); the "
            "editor shows it as a green line. The next one can follow after "
            f"{KEYPOINT_COOLDOWN_S} s"
        )
        self.keypoint_button.clicked.connect(self.insert_keypoint)
        self.keypoint_button.hide()
        row.addWidget(self.keypoint_button)
        self._keypoint_countdown: int | None = None
        self._keypoint_timer = QTimer(self)
        self._keypoint_timer.setInterval(1000)
        self._keypoint_timer.timeout.connect(self._on_keypoint_tick)
        self._clipboard = ClipboardTracker(parent=self)

        self.draw_button = QPushButton(draw_icon(), _with_shortcut(DRAW_LABEL, DRAW_SHORTCUT))
        self.draw_button.setToolTip(
            f"Draw annotations on the screen ({DRAW_SHORTCUT} or {DRAW_ALT_SHORTCUT}; "
            "available while recording).\nEsc, the same shortcut or this button again "
            "leaves drawing mode and removes everything drawn."
        )
        self.draw_button.setCheckable(True)
        self.draw_button.setEnabled(False)
        self.draw_button.toggled.connect(self._toggle_overlay)
        row.addWidget(self.draw_button)

        side = self.draw_button.sizeHint().height()
        self.theme_button = _icon_button("Theme", side, self.toggle_theme)
        row.addWidget(self.theme_button)
        self.tray_button = _icon_button("Hide to Tray", side, self.hide_to_tray)
        row.addWidget(self.tray_button)
        self._apply_icons()
        layout.addLayout(row)

        self.status_label = QLabel("Ready. Press Record to start.")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.status_label)

        self._timer = QTimer(self)
        self._timer.setInterval(UI_TICK_MS)
        self._timer.timeout.connect(self._tick)

        # Active only while recording (registered on Record, released when it ends).
        self.hotkeys = RecordingHotkeys(
            self,
            [
                Hotkey(STOP_SHORTCUT, self._stop_by_shortcut),
                Hotkey(KEYPOINT_SHORTCUT, self.insert_keypoint),
                Hotkey(DRAW_SHORTCUT, self._draw_by_shortcut),
                Hotkey(DRAW_ALT_SHORTCUT, self._draw_by_shortcut),
            ],
            system_wide=system_wide_hotkeys,
        )

        self.setFixedHeight(self.sizeHint().height())
        self._place_window()

    # Layout -----------------------------------------------------------------

    def _place_window(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.adjustSize()
        x = area.right() - self.width() - 24
        y = area.top() + 24
        self.move(x, y)

    # Record / Stop ----------------------------------------------------------

    def _on_record_or_stop(self) -> None:
        if self._processing or self._done:
            return
        if self.controller.recording:
            self._on_stop()
        else:
            self._on_record()

    def _confirm_missing_requirements(self) -> bool:
        """True when recording may start (nothing is missing, or the user accepts that
        the recording cannot be transcribed until the missing part is installed)."""
        if self._preflight is None:
            return True
        try:
            missing = self._preflight()
        except Exception:  # noqa: BLE001 - a broken check must not block recording
            log.exception("preflight check failed")
            return True
        if not missing:
            return True
        answer = QMessageBox.question(
            self,
            "Transcription is not ready",
            "Missing: " + ", ".join(missing) + ".\n\n"
            "You can record, but the recording cannot be transcribed until you run "
            "`video-story setup` (it downloads the missing parts).\n\nRecord anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_record(self) -> None:
        if not self._confirm_missing_requirements():
            return
        try:
            self.controller.start_recording()
        except Exception as exc:
            log.exception("cannot start recording")
            QMessageBox.critical(self, "Cannot start recording", f"{exc}")
            return
        right = self.geometry().right()  # before the longer labels widen the window
        self.record_button.setIcon(stop_icon())
        self.record_button.setText(_with_shortcut(STOP_LABEL, STOP_SHORTCUT))
        self.record_button.setToolTip(
            f"Stop and decide what to do with the recording ({STOP_SHORTCUT})"
        )
        self._show_recording_controls(True)
        self.draw_button.setEnabled(True)
        self.hotkeys.enable()
        self._start_clipboard_tracking()
        self._timer.start()
        self._tick()
        self._fit_width(right)

    def _show_recording_controls(self, recording: bool) -> None:
        """Settings while idle, Keypoint while recording (same slot of the row)."""
        self.settings_button.setEnabled(not recording)
        self.settings_button.setVisible(not recording)
        self._reset_keypoint_button()
        self.keypoint_button.setVisible(recording)

    def _fit_width(self, right: int) -> None:
        """Resize to the contents with the right edge at ``right`` (the window sits at
        the top right corner, and the labels differ in width between the states; Qt
        widens a window to the right as soon as a label grows)."""
        self.adjustSize()
        if self.isVisible():
            self.move(right - self.width() + 1, self.y())

    # Keypoints --------------------------------------------------------------

    @property
    def keypoint_countdown(self) -> int | None:
        """Seconds until the next keypoint may be inserted, ``None`` when ready."""
        return self._keypoint_countdown

    def insert_keypoint(self) -> int | None:
        """Mark the current moment; returns its time (ms) or ``None`` when refused."""
        recorder = self.controller.recorder
        add = getattr(recorder, "add_keypoint", None)
        if not self.controller.recording or add is None or self._keypoint_countdown is not None:
            return None
        time_ms = add()
        if time_ms is None:
            return None
        self._keypoint_countdown = KEYPOINT_COOLDOWN_S
        self.keypoint_button.setEnabled(False)
        self._show_keypoint_countdown()
        self._keypoint_timer.start()
        self._tick()
        return int(time_ms)

    def _show_keypoint_countdown(self) -> None:
        if self._keypoint_countdown is not None:
            self.keypoint_button.setText(f"{KEYPOINT_LABEL} ({self._keypoint_countdown})")

    def _on_keypoint_tick(self) -> None:
        if self._keypoint_countdown is None:
            self._keypoint_timer.stop()
            return
        self._keypoint_countdown -= 1
        if self._keypoint_countdown > 0:
            self._show_keypoint_countdown()
            return
        self._reset_keypoint_button()

    def _reset_keypoint_button(self) -> None:
        self._keypoint_timer.stop()
        self._keypoint_countdown = None
        self.keypoint_button.setText(_with_shortcut(KEYPOINT_LABEL, KEYPOINT_SHORTCUT))
        self.keypoint_button.setEnabled(True)

    # Clipboard --------------------------------------------------------------

    def _start_clipboard_tracking(self) -> None:
        recorder = self.controller.recorder
        if recorder is None:
            return
        try:
            self._clipboard.start()
        except Exception:  # noqa: BLE001 - the clipboard must never stop a recording
            log.exception("clipboard tracking unavailable")
            return
        recorder.clipboard_provider = self._clipboard.take

    def _stop_clipboard_tracking(self) -> None:
        self._clipboard.stop()
        recorder = self.controller.recorder
        if recorder is not None:
            recorder.clipboard_provider = None

    # Shortcuts --------------------------------------------------------------

    def _recording_controls_active(self) -> bool:
        return self.controller.recording and not (self._processing or self._halting or self._done)

    def _stop_by_shortcut(self) -> None:
        if not self._recording_controls_active():
            return
        if self._stop_dialog is not None:
            # Already asking: bring the question to the front again.
            self._stop_dialog.raise_()
            self._stop_dialog.activateWindow()
            return
        self._on_stop()

    def _draw_by_shortcut(self) -> None:
        if not self._recording_controls_active() or self._stop_dialog is not None:
            return
        if self.draw_button.isEnabled():
            self.draw_button.toggle()

    def _on_stop(self) -> None:
        while True:
            dialog = StopDialog(self)
            self._stop_dialog = dialog
            try:
                # Opened by the shortcut while another program is in front: the
                # question must come to the front and take the keyboard.
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
                dialog.exec()
            finally:
                dialog.hide()
                self._stop_dialog = None
            if self._processing or self._halting or not self.controller.recording:
                # The recording ended by itself while the dialog was open (auto stop,
                # low disk space): saving it has taken over.
                return
            choice = dialog.choice
            if choice is StopChoice.CONTINUE:
                return
            if choice is StopChoice.EDITOR:
                self._begin_processing()
                return
            if confirm_discard(self):
                self._discard()
                return
            # Not confirmed: back to the Stop dialog, recording still running.

    def _discard(self) -> None:
        self.hotkeys.disable()
        self._close_overlay()
        self._stop_clipboard_tracking()
        try:
            self.controller.discard()
        except Exception as exc:
            log.exception("discard failed")
            QMessageBox.critical(self, "Discard failed", f"{exc}")
        self._timer.stop()
        right = self.geometry().right()
        self.record_button.setIcon(record_icon())
        self.record_button.setText("Record")
        self.record_button.setToolTip("Start recording audio, screens and cursor")
        self._show_recording_controls(False)
        self.draw_button.setEnabled(False)
        self.status_label.setText("Recording discarded. Press Record to start again.")
        if self.in_tray:
            self.restore_from_tray()  # nothing is recording any more: show the window
        self._fit_width(right)

    # Processing -------------------------------------------------------------

    def _begin_processing(self) -> None:
        """Save the recording and open it in the editor (nothing is transcribed here:
        the editor does that in the background, so there is no dialog to wait in)."""
        if self._processing:
            return
        self._processing = True
        self.hotkeys.disable()
        self._timer.stop()
        self._close_overlay()
        self._stop_clipboard_tracking()
        self.record_button.setEnabled(False)
        self.draw_button.setEnabled(False)
        self._reset_keypoint_button()
        self.keypoint_button.setEnabled(False)
        self._thread = QThread(self)
        self._worker = ProcessingWorker(self.controller)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self.status_label.setText("Opening the Video Story Editor...")
        self._worker.finished.connect(self._on_processed)
        self._worker.failed.connect(self._on_processing_failed)
        self._thread.start()

    def _halt_recording(self, title: str, text: str) -> None:
        """The recording cannot go on (low disk space, a write or capture failure):
        stop it first - the microphone must not run on behind a message box -, tell the
        user why, then save what there is and open the editor."""
        if self._processing or self._halting:
            return
        self._halting = True
        self.hotkeys.disable()
        self._timer.stop()
        self._close_overlay()
        self._stop_clipboard_tracking()
        try:
            self.controller.stop_recording()
        except Exception:  # noqa: BLE001 - saving below reports what is wrong
            log.exception("stopping the recording failed")
        self.status_label.setText(title)
        QMessageBox.warning(self, title, f"{text}\n\n{SAVED_NOTE}")
        self._halting = False
        self._begin_processing()

    def _stop_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
        self._worker = None

    def _on_processed(self, final_dir: str) -> None:
        self._stop_thread()
        self._done = True
        log.info("recording saved: %s", final_dir)
        session_id = self.controller.paths.session_id
        try:
            info = launch_editor(
                self.controller.config,
                session_id,
                self._config_file,
                recording_session=session_id,
            )
        except Exception as exc:  # noqa: BLE001 - the recording must not be lost
            log.exception("cannot open the editor")
            self.controller.editor_failed(str(exc))
            QMessageBox.warning(
                self,
                "Video Editor",
                f"The editor could not be opened: {exc}\n\n"
                "The recording was saved and nothing was handed to the agent. Open it "
                "later with Edit Video Story.",
            )
            self._finish("cancelled")
            return
        self.controller.editor_opened(int(info.get("pid") or 0))
        self._finish("editing")

    def _on_processing_failed(self, message: str) -> None:
        self._stop_thread()
        self._done = True
        QMessageBox.critical(
            self,
            "Saving the recording failed",
            f"{message}\n\nThe recorded files were kept; the next start of Video Story or "
            "of the editor finishes the recording and lists it in the editor.",
        )
        self._finish("failed")

    def _finish(self, status: str) -> None:
        self.hotkeys.disable()
        self._timer.stop()
        self._close_overlay()
        self._drop_tray()
        self.hide()
        self._on_finished(status)

    # Settings ---------------------------------------------------------------

    def _open_settings(self) -> None:
        if self.controller.recording:
            return
        # A new Recording Folder applies to this session at once: the session moves there
        # while the dialog saves (see SessionController.relocating), so the recording, the
        # editor and the waiting agent all use the folder the user chose.
        dialog = SettingsDialog(
            self.controller.config,
            self,
            current_session_id=self.controller.paths.session_id,
            config_file=self._config_file,
            save_guard=lambda config: self.controller.relocating(config.recording_folder),
        )
        if dialog.exec() and dialog.result_config is not None:
            self.controller.config = dialog.result_config
            self.status_label.setText("Settings saved. Press Record to start.")

    # Theme ------------------------------------------------------------------

    @property
    def dark(self) -> bool:
        return self._dark

    def set_theme(self, dark: bool) -> None:
        """Apply the dark or the light theme (the whole recorder: dialogs too)."""
        right = self.geometry().right()
        self._dark = dark
        apply_theme(dark)
        self._apply_icons()
        if self._settings is not None:
            self._settings.setValue(THEME_SETTING, dark)
            self._settings.sync()
        self._fit_width(right)

    def toggle_theme(self) -> None:
        self.set_theme(not self._dark)

    def _apply_icons(self) -> None:
        """(Re)draw the icons that follow the button text colour of the theme."""
        self.draw_button.setIcon(draw_icon())
        self.theme_button.setIcon(sun_icon() if self._dark else moon_icon())
        target = "light" if self._dark else "dark"
        self.theme_button.setToolTip(f"Switch this window to the {target} theme")
        self.tray_button.setIcon(hide_to_tray_icon())
        self.tray_button.setToolTip(
            "Hide to Tray: hide this window behind an icon in the system tray; click the "
            f"icon to bring it back (while recording, {STOP_SHORTCUT} still stops)"
        )

    # System tray ------------------------------------------------------------

    @property
    def in_tray(self) -> bool:
        """True while the window is hidden behind its tray icon."""
        return self._tray is not None and self._tray.isVisible()

    def hide_to_tray(self) -> bool:
        """Hide the window behind a tray icon; False when there is no system tray."""
        if self._done:
            return False
        if not QSystemTrayIcon.isSystemTrayAvailable():
            QMessageBox.information(
                self, "Hide to Tray", "This desktop has no system tray to hide into."
            )
            return False
        if self._tray is None:
            self._tray = QSystemTrayIcon(app_icon(), self)
            self._tray.setToolTip("Video Story - click to show the window")
            self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()
        self.hide()
        if self.controller.recording:
            message = (
                f"Recording continues. Stop it with {STOP_SHORTCUT}, or click this icon "
                "to show the Video Story window again."
            )
        else:
            message = "Click this icon to show the Video Story window again."
        self._tray.showMessage(
            "Video Story", message, QSystemTrayIcon.MessageIcon.Information, TRAY_BALLOON_MS
        )
        return True

    def restore_from_tray(self) -> None:
        """Show the window again (a click on the tray icon, the recording ended)."""
        if self._tray is not None:
            self._tray.hide()
        if self._done:
            return
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Left (Trigger), right (Context), double or middle click: show the window.
        if reason != QSystemTrayIcon.ActivationReason.Unknown:
            self.restore_from_tray()

    def _drop_tray(self) -> None:
        if self._tray is not None:
            self._tray.hide()

    # Drawing overlay --------------------------------------------------------

    def _toggle_overlay(self, checked: bool) -> None:
        if checked:
            if not self.controller.recording:
                self.draw_button.setChecked(False)
                return
            if self._overlay is None and self._overlay_factory is not None:
                try:
                    self._overlay = self._overlay_factory()
                    closed = getattr(self._overlay, "closed", None)
                    if closed is not None:
                        closed.connect(self._overlay_closed)
                    keep_above = getattr(self._overlay, "keep_above", None)
                    if keep_above is not None:
                        # Stay movable and clickable above the drawing surface.
                        keep_above(self)
                    self._connect_overlay_to_recorder()
                except Exception as exc:
                    log.exception("cannot create overlay")
                    QMessageBox.warning(self, DRAW_LABEL, f"Drawing is not available: {exc}")
                    self.draw_button.setChecked(False)
                    return
            if self._overlay is not None:
                # The overlay takes the keyboard focus (B / R / A pick a tool); the overlay
                # keeps this window above it (keep_above).
                self._overlay.show()  # type: ignore[attr-defined]
                self._share_drawing_windows()
                self.raise_()
        elif self._overlay is not None:
            self._overlay.hide()  # type: ignore[attr-defined]
            self._clear_overlay()

    def _connect_overlay_to_recorder(self) -> None:
        """Let the recorder store the drawings in every frame sidecar."""
        recorder = self.controller.recorder
        if recorder is None or self._overlay is None:
            return
        set_screens = getattr(self._overlay, "set_screens", None)
        set_clock = getattr(self._overlay, "set_clock", None)
        drawings = getattr(self._overlay, "drawings", None)
        drawing_now = getattr(self._overlay, "drawing_now", None)
        zoom = getattr(self._overlay, "zoom", None)
        background = getattr(self._overlay, "background", None)
        if set_screens is not None:
            set_screens(list(getattr(recorder, "screens", [])))
        clock = getattr(recorder, "clock", None)
        if set_clock is not None and clock is not None:
            # started_ms / finished_ms of the shapes share the timeline of the frames.
            set_clock(clock.elapsed_ms)
        if drawings is not None:
            recorder.drawings_provider = drawings
        if drawing_now is not None:
            recorder.drawing_now_provider = drawing_now
        if zoom is not None:
            recorder.zoom_provider = zoom
        if background is not None:
            recorder.background_provider = background

    def _share_drawing_windows(self) -> None:
        """Tell the recorder which windows are the drawing layer (a click there is drawing,
        not a click into another application). Handles are read here, on the GUI thread."""
        recorder = self.controller.recorder
        windows = getattr(self._overlay, "windows", None)
        if recorder is None or windows is None:
            return
        try:
            handles = frozenset(int(w.winId()) for w in windows)
        except Exception:  # noqa: BLE001 - only a label in the mouse file depends on it
            log.debug("drawing windows unknown", exc_info=True)
            return
        recorder.drawing_windows = handles

    def _overlay_closed(self) -> None:
        """The user left drawing mode on the overlay (Esc, the toolbar's cross)."""
        self._clear_overlay()
        self.draw_button.blockSignals(True)
        self.draw_button.setChecked(False)
        self.draw_button.blockSignals(False)

    def _clear_overlay(self) -> None:
        """Leaving drawing mode removes the shapes: the next drawing starts empty."""
        clear = getattr(self._overlay, "clear", None)
        if clear is None:
            return
        try:
            clear()
        except Exception:  # pragma: no cover - drawing must never stop a recording
            log.exception("clearing the drawings failed")

    def _close_overlay(self) -> None:
        recorder = self.controller.recorder
        if recorder is not None:
            recorder.drawings_provider = None
            recorder.drawing_now_provider = None
            recorder.zoom_provider = None
            recorder.drawing_windows = frozenset()
        if self._overlay is not None:
            try:
                self._overlay.close()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                log.exception("overlay close failed")
            self._overlay = None
        self.draw_button.blockSignals(True)
        self.draw_button.setChecked(False)
        self.draw_button.blockSignals(False)

    # Periodic UI update -----------------------------------------------------

    def _tick(self) -> None:
        recorder = self.controller.recorder
        if recorder is None or self._processing or self._halting:
            return
        if recorder.auto_stopped:
            self.status_label.setText("Auto stop reached. Opening the editor...")
            self._begin_processing()
            return
        if getattr(recorder, "low_disk", False):
            self._halt_recording(
                "Recording stopped: disk almost full",
                low_disk_message(self.controller.paths.recording_folder),
            )
            return
        if not recorder.running and self.controller.summary is None:
            # Capture thread died: surface the error and stop cleanly.
            self._halt_recording(
                "Recording stopped",
                "Screen capture stopped unexpectedly. See recording.log.",
            )
            return
        error = getattr(recorder, "error", None)
        if error:
            # Frames or audio can no longer be written (the folder went away, ...).
            self._halt_recording(
                "Recording stopped: files cannot be written",
                f"The recording could not be written: {error}",
            )
            return
        self._clipboard.poll()
        elapsed = format_timestamp(recorder.elapsed_ms)
        remaining = max(0, self.controller.config.auto_stop_ms - recorder.elapsed_ms)
        keypoints = len(getattr(recorder, "keypoints", None) or ())
        suffix = f"  |  {keypoints} keypoint{'s' if keypoints != 1 else ''}" if keypoints else ""
        live_tick = getattr(self.controller, "live_tick", None)
        lag = live_tick() if live_tick is not None else None
        if isinstance(lag, int):
            # How far the transcription while recording is behind the recording.
            suffix += f"  |  text −{format_timestamp(lag)[3:-4]}"
        self.status_label.setText(
            f"Recording {elapsed[:-4]}  |  auto stop in {format_timestamp(remaining)[:-4]}  |  "
            f"{len(recorder.screens)} screen(s), {recorder.frame_count} frames" + suffix
        )

    # Close ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._done:
            self.hotkeys.disable()
            self._drop_tray()
            event.accept()
            return
        if self._processing or self._halting:
            event.ignore()
            return
        if self.controller.recording:
            event.ignore()
            self._on_stop()
            return
        try:
            self.controller.cancel()
        except Exception:  # pragma: no cover
            log.exception("cancel failed")
        self._done = True
        self._drop_tray()
        event.accept()
        self._on_finished("cancelled")


def _with_shortcut(label: str, shortcut: str) -> str:
    """Button text naming its keyboard shortcut in brackets."""
    return f"{label} ({shortcut})"


def _icon_button(name: str, side: int, slot: Callable[[], object]) -> QToolButton:
    """A small square icon-only button as high as the text buttons of the row;
    ``name`` names it (tests, accessibility) without showing."""
    button = QToolButton()
    button.setText(name)
    button.setAccessibleName(name)
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    button.setFixedSize(QSize(side, side))
    button.setIconSize(QSize(max(12, side - 10), max(12, side - 10)))
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.clicked.connect(lambda _checked=False: slot())
    return button


__all__ = [
    "DRAW_ALT_SHORTCUT",
    "DRAW_LABEL",
    "DRAW_SHORTCUT",
    "KEYPOINT_COOLDOWN_S",
    "KEYPOINT_LABEL",
    "KEYPOINT_SHORTCUT",
    "STOP_LABEL",
    "STOP_SHORTCUT",
    "ControlWindow",
    "ProcessingWorker",
]
