"""Helper process holding the Windows low-level mouse hook (``WH_MOUSE_LL``).

Run as ``python -m video_story.platform.windows_mouse_hook``. A low-level hook is called
synchronously for every mouse event of the whole session: in the Video Story window
process its callback waits for the GIL while frames are encoded, and the pointer of
the whole system then lags by up to 170 ms (measured in M0). Here the callback only
stamps the event with ``time.perf_counter_ns()`` (QueryPerformanceCounter, shared by all
processes), writes one short line to stdout and returns.

Protocol (ASCII lines on stdout):

* ``READY <pid>`` once the hook is installed, ``ERROR <text>`` when it cannot be;
* ``<ns> <msg> <x> <y> <data> <flags> <mods>`` per event: ``msg`` the ``WM_*`` message,
  ``x``/``y`` physical pixels of the virtual desktop, ``data`` the signed wheel delta or
  the X button number, ``flags`` the ``LLMHF_*`` flags (1 = injected), ``mods`` a bit set
  of Ctrl (1), Shift (2), Alt (4) and the Windows key (8).

Moves are thinned here already: while a button is down at most one per
:data:`DRAG_MOVE_MS`, otherwise one per :data:`IDLE_MOVE_MS` (they keep the parent's
watchdog fed). The process ends when stdin is closed (the parent stops it or dies) or
stdout cannot be written. Nothing of this module but :func:`parse_line` and the constants
is used by the parent, so it imports on every OS.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEHWHEEL = 0x020E
WM_QUIT = 0x0012
WH_MOUSE_LL = 14
LLMHF_INJECTED = 0x1

MOD_CTRL, MOD_SHIFT, MOD_ALT, MOD_META = 1, 2, 4, 8
DRAG_MOVE_MS = 8
IDLE_MOVE_MS = 50

DOWN = {WM_LBUTTONDOWN: "left", WM_RBUTTONDOWN: "right", WM_MBUTTONDOWN: "middle"}
UP = {WM_LBUTTONUP: "left", WM_RBUTTONUP: "right", WM_MBUTTONUP: "middle"}


@dataclass(frozen=True)
class HookLine:
    """One event line of the helper."""

    ns: int
    kind: str  # down, up, move, wheel
    x: int
    y: int
    button: str | None = None  # left, right, middle, x1, x2
    axis: str | None = None  # v, h
    delta: int = 0
    injected: bool = False
    mods: tuple[str, ...] = ()


def _mods(bits: int) -> tuple[str, ...]:
    names = ((MOD_CTRL, "ctrl"), (MOD_SHIFT, "shift"), (MOD_ALT, "alt"), (MOD_META, "meta"))
    return tuple(name for bit, name in names if bits & bit)


def parse_line(line: str) -> HookLine | None:
    """An event line of the helper; ``None`` for anything else (READY, noise)."""
    parts = line.split()
    if len(parts) != 7:
        return None
    try:
        ns, msg, x, y, data, flags, mods = (int(p) for p in parts)
    except ValueError:
        return None
    common = {
        "ns": ns,
        "x": x,
        "y": y,
        "injected": bool(flags & LLMHF_INJECTED),
        "mods": _mods(mods),
    }
    if msg == WM_MOUSEMOVE:
        return HookLine(kind="move", **common)
    if msg in DOWN:
        return HookLine(kind="down", button=DOWN[msg], **common)
    if msg in UP:
        return HookLine(kind="up", button=UP[msg], **common)
    if msg in (WM_XBUTTONDOWN, WM_XBUTTONUP):
        button = {1: "x1", 2: "x2"}.get(data)
        if button is None:
            return None
        return HookLine(kind="down" if msg == WM_XBUTTONDOWN else "up", button=button, **common)
    if msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
        return HookLine(
            kind="wheel", axis="v" if msg == WM_MOUSEWHEEL else "h", delta=data, **common
        )
    return None


# The helper process ----------------------------------------------------------------


def run() -> int:  # pragma: no cover - exercised by the Windows integration test
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    lresult = ctypes.c_ssize_t

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    hookproc = ctypes.WINFUNCTYPE(lresult, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, hookproc, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [
        wintypes.HHOOK,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.CallNextHookEx.restype = lresult
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    out = sys.stdout.fileno()
    thread_id = kernel32.GetCurrentThreadId()
    state = {"buttons": 0, "last_move": 0, "broken": False}
    button_bits = {
        WM_LBUTTONDOWN: 1,
        WM_LBUTTONUP: 1,
        WM_RBUTTONDOWN: 2,
        WM_RBUTTONUP: 2,
        WM_MBUTTONDOWN: 4,
        WM_MBUTTONUP: 4,
    }
    pressed = user32.GetAsyncKeyState

    def emit(text: str) -> bool:
        try:
            os.write(out, text.encode("ascii"))
            return True
        except OSError:  # the parent is gone
            state["broken"] = True
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            return False

    def modifiers() -> int:
        bits = 0
        if pressed(0x11) & 0x8000:
            bits |= MOD_CTRL
        if pressed(0x10) & 0x8000:
            bits |= MOD_SHIFT
        if pressed(0x12) & 0x8000:
            bits |= MOD_ALT
        if (pressed(0x5B) | pressed(0x5C)) & 0x8000:
            bits |= MOD_META
        return bits

    def callback(code: int, wparam: int, lparam: int) -> int:
        if code == 0 and not state["broken"]:
            now = time.perf_counter_ns()
            info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            msg = int(wparam)
            send = True
            data = 0
            mods = 0
            if msg == WM_MOUSEMOVE:
                gap = DRAG_MOVE_MS if state["buttons"] else IDLE_MOVE_MS
                send = now - state["last_move"] >= gap * 1_000_000
                if send:
                    state["last_move"] = now
            else:
                high = ctypes.c_int16(info.mouseData >> 16).value
                if msg in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                    data = high
                elif msg in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                    data = high
                    bit = 8 if high == 1 else 16
                    if msg == WM_XBUTTONDOWN:
                        state["buttons"] |= bit
                    else:
                        state["buttons"] &= ~bit
                elif msg in button_bits:
                    if msg in DOWN:
                        state["buttons"] |= button_bits[msg]
                    else:
                        state["buttons"] &= ~button_bits[msg]
                mods = modifiers()
            if send:
                emit(f"{now} {msg} {info.pt.x} {info.pt.y} {data} {info.flags} {mods}\n")
        return user32.CallNextHookEx(None, code, wparam, lparam)

    proc = hookproc(callback)
    hook = user32.SetWindowsHookExW(WH_MOUSE_LL, proc, kernel32.GetModuleHandleW(None), 0)
    if not hook:
        emit(f"ERROR SetWindowsHookExW failed (error {ctypes.get_last_error()})\n")
        return 2

    def watch_stdin() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)

    threading.Thread(target=watch_stdin, name="stdin-watch", daemon=True).start()
    emit(f"READY {os.getpid()}\n")
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        pass
    user32.UnhookWindowsHookEx(hook)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
