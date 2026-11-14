"""Undo and Redo of subtitle changes in the editor - pure logic, no Qt.

Every change of the subtitles the user makes in the editor (a drag on the unlocked
narration, Add New Subtitle, Delete Subtitle, a save from the Edit window) is one
:class:`Step`: the segments ``old`` that were replaced by ``new`` (``old`` empty: a
subtitle was added; ``new`` empty: one was deleted). Undo applies ``new -> old``,
Redo ``old -> new`` again (``LoadedRecording.apply_segment_edit`` does both).

The history belongs to one recording: the window clears it when another recording
is loaded or the transcript changes outside these steps (Transcribe again, the
background transcription). A new step clears what could be redone.
"""

from __future__ import annotations

from dataclasses import dataclass

from video_story.core.transcript import Segment

UNDO_LIMIT = 50  # steps kept; the oldest go first


@dataclass(frozen=True)
class Step:
    """``old`` segments were replaced by ``new`` (both tuples; one may be empty)."""

    old: tuple[Segment, ...]
    new: tuple[Segment, ...]
    label: str = "subtitle changed"

    def inverse(self) -> Step:
        return Step(self.new, self.old, self.label)


class SubtitleHistory:
    """The undo and redo stacks of one recording's subtitles."""

    def __init__(self, limit: int = UNDO_LIMIT) -> None:
        self.limit = max(1, int(limit))
        self._undo: list[Step] = []
        self._redo: list[Step] = []
        self.owner: str | None = None  # the session id the steps belong to

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def __len__(self) -> int:
        return len(self._undo)

    def record(self, old, new, label: str = "subtitle changed") -> None:
        """A change was saved: it can be undone; nothing can be redone any more."""
        step = Step(tuple(old), tuple(new), label)
        if step.old == step.new:
            return
        self._undo.append(step)
        del self._undo[: max(0, len(self._undo) - self.limit)]
        self._redo.clear()

    def peek_undo(self) -> Step | None:
        return self._undo[-1] if self._undo else None

    def peek_redo(self) -> Step | None:
        return self._redo[-1] if self._redo else None

    def undone(self) -> None:
        """The last step was undone (its inverse was saved): it can be redone."""
        self._redo.append(self._undo.pop())

    def redone(self) -> None:
        """The last undone step was applied again: it can be undone again."""
        self._undo.append(self._redo.pop())

    def clear(self, owner: str | None = None) -> None:
        self._undo.clear()
        self._redo.clear()
        self.owner = owner


__all__ = ["UNDO_LIMIT", "Step", "SubtitleHistory"]
