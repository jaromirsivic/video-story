"""Settings dialog: items in the order required by the specification, followed by the
retention setting and the recording folder actions (Open Folder, Delete All)."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)

from video_story.core.config import (
    AUTO_STOP_MAX_MINUTES,
    AUTO_STOP_MIN_MINUTES,
    CAPTURE_INTERVAL_MAX_MS,
    CAPTURE_INTERVAL_MIN_MS,
    JPEG_QUALITY_MAX,
    JPEG_QUALITY_MIN,
    RETENTION_MAX_DAYS,
    RETENTION_MIN_DAYS,
    Config,
    ConfigError,
    config_from_dict,
    save_config,
)

log = logging.getLogger(__name__)

SYSTEM_DEFAULT_LABEL = "System default input"


class SettingsDialog(QDialog):
    """Edit ``config.json``. ``result_config`` holds the saved configuration on accept.

    ``current_session_id`` is the session of the open control window; its work folder
    is never touched by **Delete All Recordings**.
    """

    def __init__(
        self,
        config: Config,
        parent: QWidget | None = None,
        current_session_id: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Video Story Settings")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(True)
        self._config = config
        self._current_session_id = current_session_id
        self.result_config: Config | None = None

        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        # 1. Recording Folder
        self.folder_edit = QLineEdit(str(config.recording_folder))
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        form.addRow("Recording Folder", folder_row)

        # 2. Capture Interval
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(CAPTURE_INTERVAL_MIN_MS, CAPTURE_INTERVAL_MAX_MS)
        self.interval_spin.setSuffix(" ms")
        self.interval_spin.setValue(config.capture_interval_ms)
        self.interval_spin.setToolTip(
            f"Screenshot interval, {CAPTURE_INTERVAL_MIN_MS} to {CAPTURE_INTERVAL_MAX_MS} ms"
        )
        form.addRow("Capture Interval", self.interval_spin)

        # 3. Image Format (+ JPEG Quality)
        self.format_combo = QComboBox()
        self.format_combo.addItem("PNG", "png")
        self.format_combo.addItem("JPEG", "jpeg")
        self.format_combo.setCurrentIndex(0 if config.image_format == "png" else 1)
        form.addRow("Image Format", self.format_combo)

        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(JPEG_QUALITY_MIN, JPEG_QUALITY_MAX)
        self.quality_spin.setSuffix(" %")
        self.quality_spin.setValue(config.jpeg_quality)
        form.addRow("JPEG Quality", self.quality_spin)
        self._quality_row = form.rowCount() - 1
        self.format_combo.currentIndexChanged.connect(self._update_quality_visibility)

        # 4. Audio Input
        self.audio_combo = QComboBox()
        self.audio_combo.addItem(SYSTEM_DEFAULT_LABEL, None)
        self._populate_audio_devices(config.audio_input)
        form.addRow("Audio Input", self.audio_combo)

        # 5. Auto Stop Recording
        self.auto_stop_spin = QSpinBox()
        self.auto_stop_spin.setRange(AUTO_STOP_MIN_MINUTES, AUTO_STOP_MAX_MINUTES)
        self.auto_stop_spin.setSuffix(" min")
        self.auto_stop_spin.setValue(config.auto_stop_minutes)
        self.auto_stop_spin.setToolTip(
            "Recording stops and is processed automatically after this time"
        )
        form.addRow("Auto Stop Recording", self.auto_stop_spin)

        # 6. Keep Recordings (retention)
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(RETENTION_MIN_DAYS, RETENTION_MAX_DAYS)
        self.retention_spin.setSuffix(" days")
        self.retention_spin.setValue(config.retention_days)
        self.retention_spin.setToolTip(
            "Recordings older than this are deleted automatically when Video Story starts"
        )
        form.addRow("Keep Recordings", self.retention_spin)

        # 7. Recordings: open the folder, delete everything
        self.open_folder_button = QPushButton("Open Folder")
        self.open_folder_button.setToolTip("Open the recording folder in the file manager")
        self.open_folder_button.clicked.connect(self._open_folder)
        self.delete_all_button = QPushButton("Delete All Recordings...")
        self.delete_all_button.setToolTip("Delete every stored recording (asks first)")
        self.delete_all_button.clicked.connect(self._delete_all)
        actions_row = QHBoxLayout()
        actions_row.addWidget(self.open_folder_button)
        actions_row.addWidget(self.delete_all_button)
        actions_row.addStretch(1)
        form.addRow("Recordings", actions_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        self._form = form
        self._update_quality_visibility()
        self.setMinimumWidth(460)

    # Helpers ----------------------------------------------------------------

    def _populate_audio_devices(self, selected: str | None) -> None:
        try:
            from video_story.capture.audio_recorder import list_input_devices

            devices = list_input_devices()
        except Exception as exc:  # no PortAudio, no devices
            log.warning("cannot list audio devices: %s", exc)
            devices = []
        names = [d.name for d in devices]
        for dev in devices:
            label = f"{dev.name} (default)" if dev.is_default else dev.name
            self.audio_combo.addItem(label, dev.name)
        if selected and selected not in names:
            self.audio_combo.addItem(f"{selected} (not connected)", selected)
        index = 0
        if selected:
            for i in range(self.audio_combo.count()):
                if self.audio_combo.itemData(i) == selected:
                    index = i
                    break
        self.audio_combo.setCurrentIndex(index)

    def _update_quality_visibility(self) -> None:
        is_jpeg = self.format_combo.currentData() == "jpeg"
        self._form.setRowVisible(self._quality_row, is_jpeg)
        self.adjustSize()

    def _browse_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose Recording Folder", self.folder_edit.text()
        )
        if chosen:
            self.folder_edit.setText(chosen)

    # Recording folder actions ------------------------------------------------

    def _open_folder(self) -> None:
        """Open the configured (saved) recording folder in the system file manager."""
        folder = self._config.recording_folder
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot open folder", f"{folder}\n\n{exc}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(self, "Cannot open folder", f"No file manager opened {folder}.")

    def _delete_all(self) -> None:
        """Delete every recording in the configured folder after confirmation."""
        from video_story.host.retention import delete_all_recordings, summarize_recordings

        folder = self._config.recording_folder
        summary = summarize_recordings(folder, exclude_session_id=self._current_session_id)
        if summary.count == 0:
            QMessageBox.information(self, "Delete All Recordings", f"No recordings in {folder}.")
            return
        answer = QMessageBox.question(
            self,
            "Delete All Recordings",
            f"Delete all {summary.count} recording(s) ({summary.total_text}) in\n{folder}?\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        deleted = delete_all_recordings(folder, exclude_session_id=self._current_session_id)
        QMessageBox.information(
            self, "Delete All Recordings", f"Deleted {len(deleted)} recording(s)."
        )

    # Values -------------------------------------------------------------------

    def values(self) -> dict[str, object]:
        data = self._config.to_json_dict()
        data.update(
            {
                "recording_folder": self.folder_edit.text().strip(),
                "capture_interval_ms": self.interval_spin.value(),
                "image_format": self.format_combo.currentData(),
                "jpeg_quality": self.quality_spin.value(),
                "audio_input": self.audio_combo.currentData(),
                "auto_stop_minutes": self.auto_stop_spin.value(),
                "retention_days": self.retention_spin.value(),
            }
        )
        return data

    def _save(self) -> None:
        try:
            config = config_from_dict(self.values())
            config.recording_folder.mkdir(parents=True, exist_ok=True)
            path = save_config(config)
        except ConfigError as exc:
            QMessageBox.warning(self, "Invalid setting", str(exc))
            return
        except OSError as exc:
            QMessageBox.warning(self, "Cannot save settings", f"{exc}")
            return
        log.info("settings saved to %s", path)
        self.result_config = config
        self.accept()


__all__ = ["SettingsDialog"]
