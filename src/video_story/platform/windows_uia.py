"""Windows element inspector: UI Automation in a helper process, window titles here.

:meth:`WindowsElementInspector.element_at` sends one request to
:mod:`video_story.platform.windows_uia_helper` and waits for its answer at most
``timeout_ms``; a late answer is dropped. The helper is started with the interpreter
Video Story runs on (no fixed paths), sits in a kill-on-close job object and is started
again when it dies (at most :data:`MAX_RESTARTS` times).

:meth:`window_info` (title and process of a top-level window) needs no COM: it uses
``InternalGetWindowText``, which never sends a message to the other window and so cannot
hang on an application that does not respond.
"""

from __future__ import annotations

import ctypes
import json
import logging
import subprocess
import threading
import time
from ctypes import wintypes

from video_story.core.input_events import (
    Ancestor,
    ContentKind,
    ContentRequest,
    ElementContent,
    ElementInfo,
    WindowInfo,
    normalize_role,
)
from video_story.core.textcut import truncate_text

from .base import ElementInspector, PlatformError
from .process import console_python
from .windows_input import (
    ABOVE_NORMAL_PRIORITY_CLASS,
    CREATE_NO_WINDOW,
    _end,
    _Job,
    _read_first_line,
)
from .windows_uia_helper import MAX_TEXT_CHARS, process_name

log = logging.getLogger(__name__)

READY_TIMEOUT_S = 30.0  # first start generates the comtypes wrapper of UIAutomationCore
MAX_RESTARTS = 3
STALE_GRACE_MS = 50


def uia_command() -> list[str]:
    return [console_python(), "-X", "utf8", "-m", "video_story.platform.windows_uia_helper"]


def element_from_json(data: dict) -> ElementInfo:
    """The helper's element (JSON) as an :class:`ElementInfo` with the core limits."""
    content = None
    raw = data.get("content")
    if isinstance(raw, dict) and raw.get("text"):
        text = str(raw["text"])
        chars = int(raw.get("chars") or len(text))
        content = ElementContent(
            kind=ContentKind(raw.get("kind", "value")),
            text=truncate_text(text, MAX_TEXT_CHARS),
            chars=chars,
            truncated=chars > MAX_TEXT_CHARS,
        )
    bounds = data.get("bounds")
    role_raw = str(data.get("role_raw") or "")
    return ElementInfo(
        role=normalize_role(role_raw),
        role_raw=role_raw,
        name=str(data.get("name") or ""),
        automation_id=str(data.get("automation_id") or ""),
        class_name=str(data.get("class_name") or ""),
        framework=str(data.get("framework") or ""),
        bounds=tuple(int(v) for v in bounds)
        if isinstance(bounds, list) and len(bounds) == 4
        else None,  # type: ignore[arg-type]
        is_password=bool(data.get("is_password")),
        ancestors=tuple(
            Ancestor(normalize_role(str(a[0])), str(a[1]))
            for a in data.get("ancestors") or []
            if isinstance(a, list) and len(a) == 2
        ),
        process=str(data.get("process") or ""),
        pid=data.get("pid"),
        content=content,
    )


class WindowsElementInspector(ElementInspector):
    name = "windows-uia"

    def __init__(self, command=uia_command, max_restarts: int = MAX_RESTARTS) -> None:
        self._command = command
        self._max_restarts = max_restarts
        self._proc: subprocess.Popen | None = None
        self._job: _Job | None = None
        self._reader: threading.Thread | None = None
        self._replies: dict[int, dict] = {}
        self._cond = threading.Condition()
        self._next_id = 0
        self._restarts = 0
        self._failed: str | None = None
        self._closed = False
        self._titles = _WindowText()

    @property
    def restarts(self) -> int:
        return self._restarts

    # Lifecycle ------------------------------------------------------------------

    def prepare(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if self._job is None:
            self._job = _Job()
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
            raise PlatformError(f"UI Automation helper did not start: {exc}") from exc
        self._job.assign(proc)
        first = _read_first_line(proc, READY_TIMEOUT_S)
        try:
            ready = json.loads(first) if first else {}
        except ValueError:
            ready = {}
        if not ready.get("ready"):
            _end(proc)
            raise PlatformError(ready.get("error") or "UI Automation helper did not answer")
        self._proc = proc
        self._reader = threading.Thread(
            target=self._read, args=(proc,), name="uia-reader", daemon=True
        )
        self._reader.start()
        log.info("UI Automation helper running (pid %s)", ready.get("pid"))

    def close(self) -> None:
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is not None:
            _end(proc)
        if self._reader is not None:
            self._reader.join(timeout=3)
        if self._job is not None:
            self._job.close()
            self._job = None
        with self._cond:
            self._cond.notify_all()

    # Queries --------------------------------------------------------------------

    def element_at(
        self, gx: int, gy: int, want: ContentRequest, timeout_ms: int
    ) -> ElementInfo | None:
        if self._closed:
            return None
        if self._failed:
            raise PlatformError(self._failed)
        self._ensure_running()
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        with self._cond:
            self._next_id += 1
            rid = self._next_id
        deadline_ns = time.perf_counter_ns() + (timeout_ms - STALE_GRACE_MS) * 1_000_000
        request = {"id": rid, "x": gx, "y": gy, "want": str(want), "deadline": deadline_ns}
        try:
            proc.stdin.write((json.dumps(request) + "\n").encode("ascii"))
            proc.stdin.flush()
        except OSError as exc:
            raise PlatformError(f"UI Automation helper gone: {exc}") from exc
        end = time.monotonic() + timeout_ms / 1000
        with self._cond:
            while rid not in self._replies:
                left = end - time.monotonic()
                if left <= 0 or self._closed:
                    raise TimeoutError(f"no answer in {timeout_ms} ms")
                self._cond.wait(left)
            reply = self._replies.pop(rid)
        if "error" in reply:
            if reply["error"] in ("timeout", "stale"):
                raise TimeoutError(reply["error"])
            return None
        element = reply.get("element")
        if not isinstance(element, dict):
            return None
        info = element_from_json(element)
        ms = reply.get("ms")
        return ElementInfo(
            **{**info.__dict__, "query_ms": int(ms) if isinstance(ms, int) else None}
        )

    def window_info(self, window: int) -> WindowInfo | None:
        return self._titles.info(window)

    # Helper process -------------------------------------------------------------

    def _ensure_running(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return
        if self._restarts >= self._max_restarts:
            self._failed = "UI Automation helper ended too often"
            raise PlatformError(self._failed)
        self._restarts += 1
        log.warning("UI Automation helper ended; starting it again (%d)", self._restarts)
        self.prepare()

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                try:
                    reply = json.loads(raw)
                except ValueError:
                    continue
                rid = reply.get("id")
                if not isinstance(rid, int):
                    continue
                with self._cond:
                    if rid >= self._next_id - 16:  # drop answers nobody waits for any more
                        self._replies[rid] = reply
                        for old in [k for k in self._replies if k < self._next_id - 16]:
                            del self._replies[old]
                    self._cond.notify_all()
        except (OSError, ValueError):
            log.debug("UI Automation pipe closed", exc_info=True)


class _WindowText:
    """Title and process of top-level windows, without sending them messages."""

    def __init__(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.InternalGetWindowText.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._processes: dict[int, str] = {}

    def info(self, window: int) -> WindowInfo | None:
        try:
            buf = ctypes.create_unicode_buffer(512)
            self._user32.InternalGetWindowText(wintypes.HWND(window), buf, len(buf))
            pid = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(wintypes.HWND(window), ctypes.byref(pid))
        except (AttributeError, OSError):
            return None
        if not pid.value:
            return None
        if pid.value not in self._processes:
            self._processes[pid.value] = process_name(pid.value)
        return WindowInfo(title=buf.value, process=self._processes[pid.value], pid=pid.value)


__all__ = ["WindowsElementInspector", "element_from_json", "uia_command"]
