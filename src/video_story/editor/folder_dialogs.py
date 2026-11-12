"""Dialogs of the recordings' folders: Create new folder, Move Video(s) to Folder."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from video_story.core.folders import new_folder_name, validate_name

CREATE_TITLE = "Create new folder"
MOVE_ONE_TITLE = "Move Video to Folder"
MOVE_MANY_TITLE = "Move Videos to Folder"


def move_checkbox_label(count: int) -> str:
    if count == 1:
        return "Move selected video to the new folder"
    return f"Move selected videos ({count}) to the new folder"


class CreateFolderDialog(QDialog):
    """The name of a new folder (prefilled ``New folder YYYYMMDD-HHMMSS``, selected so
    that typing replaces it) and whether the selected videos move into it (ticked).

    ``movable`` is how many selected videos can move; with none the box is off.
    """

    def __init__(
        self,
        existing: list[str],
        movable: int,
        parent: QWidget | None = None,
        now: datetime | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(CREATE_TITLE)
        self.setModal(True)
        self._existing = list(existing)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Folder name:"))
        self.name_edit = QLineEdit(new_folder_name(self._existing, now))
        self.name_edit.selectAll()
        layout.addWidget(self.name_edit)
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #d32f2f;")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        self.move_checkbox = QCheckBox(move_checkbox_label(max(1, movable)))
        self.move_checkbox.setChecked(movable > 0)
        self.move_checkbox.setEnabled(movable > 0)
        if movable == 0:
            self.move_checkbox.setToolTip("No selected video can be moved")
        layout.addWidget(self.move_checkbox)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._accept_if_valid)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.name_edit.textChanged.connect(self._validate)
        self._validate()
        self.setMinimumWidth(380)

    @property
    def name(self) -> str:
        return self.name_edit.text()

    @property
    def move_selected(self) -> bool:
        return self.move_checkbox.isEnabled() and self.move_checkbox.isChecked()

    def problem(self) -> str | None:
        return validate_name(self.name, self._existing)

    def _validate(self) -> None:
        problem = self.problem()
        self.error_label.setText(problem or "")
        self.error_label.setVisible(problem is not None)
        save = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setEnabled(problem is None)

    def _accept_if_valid(self) -> None:
        if self.problem() is None:
            self.accept()


class MoveToFolderDialog(QDialog):
    """Choose the folder the selected videos move to (a combo box of the others)."""

    def __init__(self, folders: list[str], count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(MOVE_ONE_TITLE if count == 1 else MOVE_MANY_TITLE)
        self.setModal(True)
        layout = QVBoxLayout(self)
        what = "the video" if count == 1 else f"the {count} videos"
        layout.addWidget(QLabel(f"Move {what} to the folder:"))
        self.combo = QComboBox()
        self.combo.addItems(folders)
        layout.addWidget(self.combo)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(320)

    @property
    def folder(self) -> str:
        return self.combo.currentText()


__all__ = [
    "CREATE_TITLE",
    "CreateFolderDialog",
    "MoveToFolderDialog",
    "move_checkbox_label",
]
