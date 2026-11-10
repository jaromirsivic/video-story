"""Keyboard shortcuts of the control window while recording (Stop, Keypoint, Draw).

While recording the user works in other applications, so the control window rarely has
the keyboard focus. On Windows the shortcuts are therefore system-wide hotkeys
(``RegisterHotKey``): they work whichever application is in front, and the key press
does not reach that application. They are registered only while recording, so the
combinations stay free for other programs the rest of the time.

Elsewhere (Linux X11, macOS, the offscreen Qt platform of the tests), and for a
combination another program has already registered, the shortcut falls back to an
application-wide :class:`QShortcut`: it then works while a Video Story window (the
control window, the drawing overlay) has the focus.

Note: on keyboard layouts with AltGr, Windows reports AltGr as Ctrl+Alt, so AltGr plus
one of the letters triggers the shortcut too while recording.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QObject, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import QWidget

log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000  # holding the keys down fires once
HOTKEY_ID_BASE = 0x5653  # application hotkey ids must lie in 0x0000-0xBFFF


@dataclass
class Hotkey:
    """One shortcut: ``sequence`` such as ``"Ctrl+Alt+S"`` and what it triggers."""

    sequence: str
    callback: Callable[[], None]


def system_wide_available() -> bool:
    """True when hotkeys can be registered for the whole system (native Windows Qt)."""
    if not sys.platform.startswith("win"):
        return False
    app = QGuiApplication.instance()
    return app is not None and QGuiApplication.platformName() == "windows"


def windows_hotkey(sequence: str) -> tuple[int, int] | None:
    """``(modifiers, virtual key)`` for RegisterHotKey, ``None`` when not expressible.

    Only letters and digits are supported: their Qt key codes equal the Windows
    virtual-key codes.
    """
    keys = QKeySequence(sequence)
    if keys.count() != 1:
        return None
    combination = keys[0]
    key = int(combination.key().value)
    if not (ord("A") <= key <= ord("Z") or ord("0") <= key <= ord("9")):
        return None
    qt_modifiers = combination.keyboardModifiers()
    modifiers = MOD_NOREPEAT
    if qt_modifiers & Qt.KeyboardModifier.ControlModifier:
        modifiers |= MOD_CONTROL
    if qt_modifiers & Qt.KeyboardModifier.AltModifier:
        modifiers |= MOD_ALT
    if qt_modifiers & Qt.KeyboardModifier.ShiftModifier:
        modifiers |= MOD_SHIFT
    return modifiers, key


class _WindowsHotkeyFilter(QAbstractNativeEventFilter):
    """Passes WM_HOTKEY thread messages (hotkeys registered without a window) on."""

    def __init__(self, on_hotkey: Callable[[int], bool]) -> None:
        super().__init__()
        self._on_hotkey = on_hotkey

    def nativeEventFilter(self, event_type, message):  # noqa: N802 - Qt API
        # message is a shiboken VoidPtr without a size: its truth value raises.
        if bytes(event_type) != b"windows_generic_MSG" or message is None:
            return False, 0
        from ctypes import wintypes

        msg = wintypes.MSG.from_address(int(message))
        if msg.message != WM_HOTKEY:
            return False, 0
        return bool(self._on_hotkey(int(msg.wParam))), 0


class RecordingHotkeys(QObject):
    """The shortcuts of one window; inactive until :meth:`enable`.

    ``system_wide`` ``None`` picks system-wide hotkeys where available (see the module
    docstring); ``False`` always uses application-wide shortcuts.
    """

    def __init__(
        self,
        parent: QWidget,
        hotkeys: list[Hotkey],
        system_wide: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self._parent = parent
        self._hotkeys = list(hotkeys)
        self._system_wide = system_wide_available() if system_wide is None else system_wide
        self._registered: dict[int, Hotkey] = {}
        self._filter: _WindowsHotkeyFilter | None = None
        # Created on first use and kept (enabled only while active): deleting QShortcuts
        # while the window lives proved fragile.
        self._shortcuts: dict[str, QShortcut] = {}
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def system_wide(self) -> list[str]:
        """Sequences currently registered for the whole system."""
        return [hotkey.sequence for hotkey in self._registered.values()]

    @property
    def application_wide(self) -> list[str]:
        """Sequences currently active as application-wide shortcuts."""
        return [
            shortcut.key().toString()
            for shortcut in self._shortcuts.values()
            if shortcut.isEnabled()
        ]

    def enable(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        for index, hotkey in enumerate(self._hotkeys):
            if self._system_wide and self._register_windows(HOTKEY_ID_BASE + index, hotkey):
                continue
            self._add_shortcut(hotkey)

    def disable(self) -> None:
        if not self._enabled:
            return
        self._enabled = False
        if self._registered:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            for hotkey_id in self._registered:
                user32.UnregisterHotKey(None, hotkey_id)
            self._registered.clear()
        if self._filter is not None:
            app = QCoreApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._filter)
            self._filter = None
        for shortcut in self._shortcuts.values():
            shortcut.setEnabled(False)

    # Windows ----------------------------------------------------------------

    def _register_windows(self, hotkey_id: int, hotkey: Hotkey) -> bool:
        native = windows_hotkey(hotkey.sequence)
        if native is None:
            return False
        modifiers, key = native
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            ok = bool(user32.RegisterHotKey(None, hotkey_id, modifiers, key))
        except Exception:  # noqa: BLE001 - fall back to an application-wide shortcut
            log.exception("RegisterHotKey failed for %s", hotkey.sequence)
            return False
        if not ok:
            log.warning(
                "%s is already used by another program; it works only while a Video Story "
                "window has the focus",
                hotkey.sequence,
            )
            return False
        if self._filter is None:
            self._filter = _WindowsHotkeyFilter(self._on_windows_hotkey)
            app = QCoreApplication.instance()
            if app is not None:
                app.installNativeEventFilter(self._filter)
        self._registered[hotkey_id] = hotkey
        return True

    def _on_windows_hotkey(self, hotkey_id: int) -> bool:
        hotkey = self._registered.get(hotkey_id)
        if hotkey is None:
            return False
        self._run(hotkey)
        return True

    # Application-wide -------------------------------------------------------

    def _add_shortcut(self, hotkey: Hotkey) -> None:
        shortcut = self._shortcuts.get(hotkey.sequence)
        if shortcut is None:
            shortcut = QShortcut(QKeySequence(hotkey.sequence), self._parent)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(lambda h=hotkey: self._run(h))
            self._shortcuts[hotkey.sequence] = shortcut
        shortcut.setEnabled(True)

    def _run(self, hotkey: Hotkey) -> None:
        # Leave the native event filter / shortcut handler first: Stop opens a modal
        # dialog with an event loop of its own.
        QTimer.singleShot(0, self, lambda: self._run_now(hotkey))

    def _run_now(self, hotkey: Hotkey) -> None:
        if not self._enabled:
            return
        try:
            hotkey.callback()
        except Exception:  # noqa: BLE001 - a shortcut must never end the recording
            log.exception("shortcut %s failed", hotkey.sequence)


__all__ = [
    "Hotkey",
    "RecordingHotkeys",
    "system_wide_available",
    "windows_hotkey",
]
