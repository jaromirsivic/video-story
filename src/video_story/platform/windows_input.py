"""Windows mouse source: the low-level hook in a helper process, read through a pipe.

See :mod:`video_story.platform.windows_mouse_hook` for why the hook runs in its own
process. This side starts that process with the interpreter Video Story itself runs
on (no fixed paths), reads its lines on a thread, converts the time stamps to the
recording clock, finds the top-level window under each press and wheel event and
hands :class:`RawMouseEvent` values to the recorder.

A watchdog restarts the helper (at most :data:`MAX_RESTARTS` times) when it ended, or
when the pointer keeps moving but no event arrived for :data:`STALL_S` seconds: Windows
removes a low-level hook that answers too slowly without telling anyone. The helper is
put into a job object that kills it with Video Story, and it also quits on its own when
its stdin closes.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

from video_story.core.gestures import ContactFrame, TouchpadGeometry
from video_story.core.input_events import (
    MouseButton,
    MouseCapabilities,
    MouseSystemParams,
    RawMouseEvent,
)
from video_story.core.timeline import RecordingClock

from .base import MouseSource, PlatformError, TouchpadSource
from .process import console_python
from .windows_mouse_hook import parse_line
from .windows_touchpad import PadLine, TouchLine, parse_touch_line

log = logging.getLogger(__name__)

READY_TIMEOUT_S = 15.0  # a cold Python start on a slow disk with antivirus scanning
STALL_S = 5.0
MAX_RESTARTS = 3
WATCH_INTERVAL_S = 0.5
STOP_TIMEOUT_S = 3.0

ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
CREATE_NO_WINDOW = 0x08000000
GA_ROOT = 2
SM_CXDOUBLECLK, SM_CYDOUBLECLK, SM_CXDRAG, SM_CYDRAG = 36, 37, 68, 69
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


def system_params() -> MouseSystemParams:
    """Double-click time and rectangle and the drag threshold of this user's settings."""
    user32 = ctypes.windll.user32
    try:
        return MouseSystemParams(
            double_click_ms=int(user32.GetDoubleClickTime()) or 500,
            double_click_px=(
                int(user32.GetSystemMetrics(SM_CXDOUBLECLK)) or 4,
                int(user32.GetSystemMetrics(SM_CYDOUBLECLK)) or 4,
            ),
            drag_px=(
                int(user32.GetSystemMetrics(SM_CXDRAG)) or 4,
                int(user32.GetSystemMetrics(SM_CYDRAG)) or 4,
            ),
        )
    except (AttributeError, OSError):
        return MouseSystemParams()


def hook_command() -> list[str]:
    """The helper's command line: the same Python and package as this process."""
    return [console_python(), "-X", "utf8", "-m", "video_story.platform.windows_mouse_hook"]


class _Job:
    """A job object that kills the helper when Video Story ends (also when it crashes)."""

    def __init__(self) -> None:
        self.handle = None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return

            class IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

            class Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class Extended(ctypes.Structure):
                _fields_ = [
                    ("Basic", Basic),
                    ("Io", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = Extended()
            info.Basic.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if kernel32.SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                self.handle = handle
            else:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            log.debug("no job object for the mouse hook helper", exc_info=True)

    def assign(self, proc: subprocess.Popen) -> None:
        if self.handle is None:
            return
        try:
            kernel32 = ctypes.windll.kernel32
            if not kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(self.handle),
                wintypes.HANDLE(int(proc._handle)),  # type: ignore[attr-defined]
            ):
                log.debug("mouse hook helper not put into the job object")
        except (AttributeError, OSError):
            log.debug("mouse hook helper not put into the job object", exc_info=True)

    def close(self) -> None:
        if self.handle is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(self.handle))
            except OSError:  # pragma: no cover
                pass
            self.handle = None


def window_at(gx: int, gy: int) -> tuple[int | None, int | None]:
    """Top-level window under a point and its process id."""
    user32 = ctypes.windll.user32
    try:
        hwnd = user32.WindowFromPoint(wintypes.POINT(gx, gy))
        if not hwnd:
            return None, None
        root = user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT) or hwnd
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(root), ctypes.byref(pid))
        return int(root), int(pid.value) or None
    except (AttributeError, OSError):
        return None, None


class WindowsMouseSource(MouseSource):
    name = "windows-ll-hook"
    capabilities = MouseCapabilities(
        buttons=(
            MouseButton.LEFT,
            MouseButton.MIDDLE,
            MouseButton.RIGHT,
            MouseButton.X1,
            MouseButton.X2,
        ),
        horizontal_wheel=True,
        high_resolution_wheel=True,
        touchpad_gestures=False,  # M5
        elements=True,  # with the UI Automation inspector (the recorder says if it ran)
        element_content=True,
    )

    def __init__(
        self,
        command: Callable[[], list[str]] = hook_command,
        stall_s: float = STALL_S,
        max_restarts: int = MAX_RESTARTS,
        cursor: Callable[[], tuple[int, int] | None] | None = None,
    ) -> None:
        self.params = system_params()
        self._command = command
        self._stall_s = stall_s
        self._max_restarts = max_restarts
        self._cursor = cursor or _cursor_position
        self._proc: subprocess.Popen | None = None
        self._job: _Job | None = None
        self._reader: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._sink: Callable[[RawMouseEvent], None] | None = None
        self._clock: RecordingClock | None = None
        self._on_status: Callable[[str, str], None] | None = None
        self._last_event = 0.0
        self._restarts = 0
        self._gave_up = False
        # Precision Touchpads the helper found, and where their frames go (see
        # WindowsTouchpadSource); frames arriving without a sink are dropped.
        self.touchpads: dict[int, TouchpadGeometry] = {}
        self.touch_sink: Callable[[ContactFrame], None] | None = None

    # MouseSource ---------------------------------------------------------------

    @property
    def restarts(self) -> int:
        return self._restarts

    def start(
        self,
        sink: Callable[[RawMouseEvent], None],
        clock: RecordingClock,
        on_status: Callable[[str, str], None] | None = None,
    ) -> None:
        if self._proc is not None:
            raise RuntimeError("mouse source already started")
        self._sink, self._clock, self._on_status = sink, clock, on_status
        self._stopping.clear()
        self._job = _Job()
        self._spawn()  # raises PlatformError
        self._watchdog = threading.Thread(target=self._watch, name="mouse-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            _end(proc)
        for thread in (self._reader, self._watchdog):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=STOP_TIMEOUT_S)
        with self._lock:
            self._proc = None
        if self._job is not None:
            self._job.close()
            self._job = None

    def healthy(self) -> bool:
        with self._lock:
            proc = self._proc
        return proc is not None and proc.poll() is None and not self._gave_up

    def probe(self) -> str | None:
        """Start the helper once and stop it again (doctor)."""
        try:
            proc, _ = self._launch()
        except PlatformError as exc:
            return str(exc)
        _end(proc)
        return None

    # Helper process ------------------------------------------------------------

    def _launch(self) -> tuple[subprocess.Popen, str]:
        try:
            proc = subprocess.Popen(
                self._command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW | ABOVE_NORMAL_PRIORITY_CLASS,
                bufsize=0,
            )
        except OSError as exc:
            raise PlatformError(f"mouse hook helper did not start: {exc}") from exc
        if self._job is not None:
            self._job.assign(proc)
        first = _read_first_line(proc, READY_TIMEOUT_S)
        if first is None or not first.startswith("READY"):
            _end(proc)
            reason = first.removeprefix("ERROR").strip() if first else "no answer"
            raise PlatformError(f"mouse hook not installed: {reason}")
        return proc, first

    def _spawn(self) -> None:
        proc, ready = self._launch()
        log.info("mouse hook helper running (%s)", ready)
        with self._lock:
            self._proc = proc
        self._last_event = time.monotonic()
        self._reader = threading.Thread(
            target=self._read, args=(proc,), name="mouse-reader", daemon=True
        )
        self._reader.start()

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        clock, sink = self._clock, self._sink
        assert clock is not None and sink is not None
        try:
            for raw in proc.stdout:
                self._handle_line(raw, clock, sink)
        except (OSError, ValueError):  # the pipe went away with the helper
            log.debug("mouse hook pipe closed", exc_info=True)

    def _handle_line(
        self, raw: bytes, clock: RecordingClock, sink: Callable[[RawMouseEvent], None]
    ) -> None:
        self._last_event = time.monotonic()
        text = raw.decode("ascii", errors="replace")
        if text[:1] in ("P", "T"):
            self._handle_touch(text, clock)
            return
        line = parse_line(text)
        if line is None:
            return
        t_ms = clock.ms_at(line.ns)
        if t_ms is None or t_ms < 0:
            return  # before Record
        # Also for moves: a rest point over Video Story's own window is not queried.
        window, pid = window_at(line.x, line.y)
        event = RawMouseEvent(
            kind=line.kind,  # type: ignore[arg-type]
            t_ms=t_ms,
            gx=line.x,
            gy=line.y,
            button=MouseButton(line.button) if line.button else None,
            axis=line.axis,  # type: ignore[arg-type]
            delta=line.delta,
            modifiers=line.mods,
            injected=line.injected,
            window=window,
            pid=pid,
        )
        try:
            sink(event)
        except Exception:  # noqa: BLE001 - the recorder must not kill the reader
            log.exception("mouse event not stored")

    def _handle_touch(self, text: str, clock: RecordingClock) -> None:
        line = parse_touch_line(text)
        if isinstance(line, PadLine):
            if line.width > 0 and line.height > 0:
                self.touchpads[line.device] = TouchpadGeometry(line.width, line.height)
            return
        if not isinstance(line, TouchLine):
            return
        sink = self.touch_sink
        t_ms = clock.ms_at(line.ns)
        if sink is None or t_ms is None or t_ms < 0:
            return
        try:
            sink(ContactFrame(t_ms, line.contacts, line.device))
        except Exception:  # noqa: BLE001 - the recorder must not kill the reader
            log.exception("touchpad frame not stored")

    def _watch(self) -> None:
        last_pos = self._cursor()
        moving_since: float | None = None
        while not self._stopping.wait(WATCH_INTERVAL_S):
            with self._lock:
                proc = self._proc
            if proc is None:
                return
            now = time.monotonic()
            pos = self._cursor()
            moved = pos is not None and pos != last_pos
            last_pos = pos
            if proc.poll() is not None:
                self._restart(f"the helper ended (exit code {proc.returncode})")
                moving_since = None
                continue
            if not moved or now - self._last_event < self._stall_s:
                if not moved:
                    moving_since = None
                continue
            moving_since = moving_since or now
            # The pointer moved in several checks in a row and the hook stayed silent.
            if now - moving_since >= 1.0:
                self._restart(f"no events for {self._stall_s:.0f} s while the pointer moved")
                moving_since = None

    def _restart(self, reason: str) -> None:
        if self._stopping.is_set() or self._gave_up:
            return
        if self._restarts >= self._max_restarts:
            self._gave_up = True
            log.warning("mouse hook: %s; not restarted again", reason)
            self._status("gave_up", reason)
            return
        self._restarts += 1
        log.warning("mouse hook: %s; restarting (%d)", reason, self._restarts)
        with self._lock:
            old = self._proc
        if old is not None:
            _end(old)
        try:
            self._spawn()
        except PlatformError as exc:
            self._gave_up = True
            self._status("failed", f"{reason}; restart failed: {exc}")
            return
        self._status("restarted", reason)

    def _status(self, state: str, reason: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(state, reason)
            except Exception:  # noqa: BLE001
                log.exception("mouse status not stored")


class WindowsTouchpadSource(TouchpadSource):
    """Touchpad frames read by the mouse hook helper (one process for both)."""

    def __init__(self, mouse: WindowsMouseSource) -> None:
        self.mouse = mouse

    @property
    def geometry(self) -> TouchpadGeometry:  # type: ignore[override]
        pads = list(self.mouse.touchpads.values())
        return pads[0] if pads else TouchpadGeometry(0, 0)

    def geometry_of(self, device: int) -> TouchpadGeometry | None:
        return self.mouse.touchpads.get(device) or (self.geometry if self.available else None)

    @property
    def available(self) -> bool:
        return bool(self.mouse.touchpads)

    def start(self, sink: Callable[[ContactFrame], None], clock: RecordingClock) -> None:
        self.mouse.touch_sink = sink

    def stop(self) -> None:
        self.mouse.touch_sink = None


def _cursor_position() -> tuple[int, int] | None:
    point = wintypes.POINT()
    try:
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return point.x, point.y
    except (AttributeError, OSError):
        pass
    return None


def _read_first_line(proc: subprocess.Popen, timeout: float) -> str | None:
    """The helper's first line, or ``None`` when it does not answer in time."""
    result: list[str] = []

    def read() -> None:
        assert proc.stdout is not None
        try:
            result.append(proc.stdout.readline().decode("ascii", errors="replace").strip())
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=read, name="mouse-ready", daemon=True)
    thread.start()
    thread.join(timeout)
    return result[0] if result and result[0] else None


def _end(proc: subprocess.Popen) -> None:
    """Close the helper's stdin (it quits by itself), kill it when it does not."""
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover
            log.warning("mouse hook helper %s did not end", proc.pid)


__all__ = [
    "WindowsMouseSource",
    "WindowsTouchpadSource",
    "hook_command",
    "system_params",
    "window_at",
]
