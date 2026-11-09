"""The **Instructions** dialog: master instructions for the AI about the whole recording.

Under the explanation, three template buttons fill the text box with a ready-made
English instruction: **Draft Intent** (a formal brief, then ask how to continue),
**Create Plan** (an implementation or execution plan) and **Implement** (carry out
everything autonomously). When the box already holds other text, a confirmation asks
before it is replaced.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from video_story.core.instructions import MAX_INSTRUCTIONS_CHARS, normalize_instructions

TITLE = "Instructions for the AI"
HEADING = "Master instructions for the AI about the whole recording"
EXPLANATION = (
    "These instructions frame everything you said in the recording: the AI reads them first "
    "and applies them to the cropped transcript and the screenshots."
)
PLACEHOLDER = "e.g. Create an implementation plan from what I describe in this recording."
REPLACE_TITLE = "Replace the instructions?"
REPLACE_TEXT = "The box already contains text. Replace it with the “{name}” template?"


@dataclass(frozen=True)
class Template:
    name: str
    tooltip: str
    text: str


DRAFT_INTENT = Template(
    name="Draft Intent",
    tooltip="Turn the recording into a formal brief, then ask how to continue",
    text="""\
# Goal: a formal brief only

Process the recording (only its cropped range) and turn what I say and show into a
formal brief. Write it in the chat as Markdown:

- A short summary of the intent: what I want and why.
- A numbered list of everything that should be done. Make each item specific and
  checkable, and note the time stamp and screen it comes from.
- Open questions and ambiguities, listed separately.

Do not write an implementation plan and do not change any code or files.
At the end, ask me whether to continue with the implementation or with an
implementation plan.
""",
)

CREATE_PLAN = Template(
    name="Create Plan",
    tooltip="Turn the recording into an implementation or execution plan",
    text="""\
# Goal: an implementation plan

Process the recording (only its cropped range) and create an implementation plan
(or an execution plan, when the task is not about code) for everything I describe
and show:

- Start with a short summary of the goal.
- Then ordered steps: what changes, where, and how to verify it.
- Note risks, assumptions and open questions.

Do not implement anything yet; wait for my approval of the plan.
""",
)

IMPLEMENT = Template(
    name="Implement",
    tooltip="Carry out everything in the recording without asking",
    text="""\
# Goal: implement everything

Implement (or carry out) everything I describe and show in the recording (only its
cropped range):

- Work autonomously and do not ask me questions unless you are truly blocked.
- Make the necessary decisions yourself, choosing the best option you can.
- Verify the result (build, tests, run it) before you finish.

End with a short summary of what was done and of the decisions you made.
""",
)

TEMPLATES: tuple[Template, ...] = (DRAFT_INTENT, CREATE_PLAN, IMPLEMENT)


class InstructionsDialog(QDialog):
    """Edit the instructions; ``instructions()`` is the normalised text after OK."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.setModal(True)
        layout = QVBoxLayout(self)
        heading = QLabel(HEADING)
        heading_font = QFont(heading.font())
        heading_font.setBold(True)
        heading_font.setPointSizeF(heading_font.pointSizeF() + 1)
        heading.setFont(heading_font)
        layout.addWidget(heading)
        explanation = QLabel(EXPLANATION)
        explanation.setWordWrap(True)
        explanation.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(explanation)
        templates = QHBoxLayout()
        templates.setSpacing(8)
        self.template_buttons: dict[str, QPushButton] = {}
        for template in TEMPLATES:
            button = QPushButton(template.name)
            button.setToolTip(template.tooltip)
            button.setAutoDefault(False)
            button.clicked.connect(lambda _=False, t=template: self.apply_template(t))
            templates.addWidget(button)
            self.template_buttons[template.name] = button
        templates.addStretch(1)
        layout.addLayout(templates)
        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setPlaceholderText(PLACEHOLDER)
        self.text_edit.setPlainText(text)
        self.text_edit.setTabChangesFocus(True)
        self.text_edit.setMinimumSize(520, 260)
        layout.addWidget(self.text_edit, 1)
        self.counter = QLabel("")
        self.counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.counter)
        self.text_edit.textChanged.connect(self._update_counter)
        self._update_counter()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.text_edit.setFocus()

    def _update_counter(self) -> None:
        count = len(self.text_edit.toPlainText())
        self.counter.setText(f"{count} / {MAX_INSTRUCTIONS_CHARS} characters")

    def confirm_replace(self, template: Template) -> bool:
        """Ask before the text in the box is replaced (tests override this)."""
        answer = QMessageBox.question(
            self,
            REPLACE_TITLE,
            REPLACE_TEXT.format(name=template.name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def apply_template(self, template: Template) -> bool:
        """Put ``template`` into the box; asks first when other text would be lost."""
        current = self.text_edit.toPlainText()
        if current.strip() == template.text.strip():
            return True
        if current.strip() and not self.confirm_replace(template):
            return False
        self.text_edit.setPlainText(template.text)
        self.text_edit.setFocus()
        return True

    def instructions(self) -> str:
        return normalize_instructions(self.text_edit.toPlainText())


__all__ = [
    "CREATE_PLAN",
    "DRAFT_INTENT",
    "EXPLANATION",
    "HEADING",
    "IMPLEMENT",
    "InstructionsDialog",
    "TEMPLATES",
    "TITLE",
    "Template",
]
