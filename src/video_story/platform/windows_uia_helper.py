"""Helper process answering "what is under this point?" with Windows UI Automation.

Run as ``python -m video_story.platform.windows_uia_helper``. It lives in a process of its
own so that COM (its own multithreaded apartment), slow or crashing accessibility
providers of other applications and the GIL never touch the Video Story window.

Protocol: one JSON object per line.

* in (stdin): ``{"id": 7, "x": 812, "y": 440, "want": "value_and_text", "deadline": <ns>}``
  - ``want`` is ``value_and_text``, ``selection`` or ``none``; ``deadline`` a
  ``time.perf_counter_ns()`` value after which the request is skipped (the parent has
  stopped waiting);
* out (stdout): ``{"ready": true, "pid": ...}`` once, then per request
  ``{"id": 7, "element": {...}, "ms": 12}`` or ``{"id": 7, "error": "..."}``.

Content follows ``implementation-plan-mouse.md`` 6.1: a field's value; otherwise (or
when the value is long, e.g. a whole document) the paragraph at the point
(``TextPattern.RangeFromPoint``; ``GetVisibleRanges`` where that fails, as in Firefox);
after a drag or a multi click the selection. At most :data:`TEXT_REQUEST_CHARS`
characters are read, so the parent can tell a text longer than 4096 characters. A
password field is never read.
"""

from __future__ import annotations

import json
import os
import sys
import time

MAX_TEXT_CHARS = 4096
TEXT_REQUEST_CHARS = MAX_TEXT_CHARS + 1  # one more: tells "longer than the limit"
LONG_VALUE_CHARS = 1000  # a value this long is a document: the paragraph says more
UIA_TIMEOUT_MS = 400
MAX_ANCESTOR_STEPS = 6
MAX_ANCESTORS = 3
TEXT_OWNER_STEPS = 3


def main() -> int:  # pragma: no cover - exercised by the Windows integration test
    import ctypes

    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    sys.coinit_flags = 0  # COINIT_MULTITHREADED, read by comtypes on import
    out = sys.stdout

    def reply(data: dict) -> None:
        out.write(json.dumps(data, ensure_ascii=True) + "\n")
        out.flush()

    try:
        uia = _Uia()
    except Exception as exc:  # noqa: BLE001 - report and end
        reply({"ready": False, "error": f"UI Automation unavailable: {exc}"})
        return 2
    reply({"ready": True, "pid": os.getpid()})
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        rid = request.get("id")
        deadline = request.get("deadline")
        if isinstance(deadline, int) and time.perf_counter_ns() > deadline:
            reply({"id": rid, "error": "stale"})
            continue
        started = time.perf_counter()
        try:
            element = uia.element_at(int(request["x"]), int(request["y"]), str(request.get("want")))
        except Exception as exc:  # noqa: BLE001 - one bad provider must not end the helper
            reply({"id": rid, "error": _error_text(exc)})
            continue
        reply({"id": rid, "element": element, "ms": round((time.perf_counter() - started) * 1000)})
    return 0


def _error_text(exc: BaseException) -> str:
    code = getattr(exc, "hresult", None)
    if isinstance(code, int):
        code &= 0xFFFFFFFF
        if code == 0x80131505:  # UIA_E_TIMEOUT
            return "timeout"
        if code == 0x80040201:  # UIA_E_ELEMENTNOTAVAILABLE
            return "unavailable"
        return f"com error {code:#010x}"
    return f"{type(exc).__name__}: {exc}"[:200]


class _Uia:  # pragma: no cover - needs Windows and UI Automation
    def __init__(self) -> None:
        import comtypes
        import comtypes.client as cc

        cc.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uia_mod

        self.comtypes = comtypes
        self.U = uia_mod
        try:
            self.uia = cc.CreateObject(uia_mod.CUIAutomation8, interface=uia_mod.IUIAutomation2)
            self.uia.ConnectionTimeout = UIA_TIMEOUT_MS
            self.uia.TransactionTimeout = UIA_TIMEOUT_MS
        except (OSError, comtypes.COMError, AttributeError):  # Windows 7: no timeouts
            self.uia = cc.CreateObject(uia_mod.CUIAutomation, interface=uia_mod.IUIAutomation)
        self.errors = (comtypes.COMError, ValueError, OSError)
        self.cache = self.uia.CreateCacheRequest()
        for prop in (
            uia_mod.UIA_ControlTypePropertyId,
            uia_mod.UIA_NamePropertyId,
            uia_mod.UIA_AutomationIdPropertyId,
            uia_mod.UIA_ClassNamePropertyId,
            uia_mod.UIA_FrameworkIdPropertyId,
            uia_mod.UIA_BoundingRectanglePropertyId,
            uia_mod.UIA_IsPasswordPropertyId,
            uia_mod.UIA_ProcessIdPropertyId,
            uia_mod.UIA_IsValuePatternAvailablePropertyId,
            uia_mod.UIA_IsTextPatternAvailablePropertyId,
        ):
            self.cache.AddProperty(prop)
        self.parent_cache = self.uia.CreateCacheRequest()
        self.parent_cache.AddProperty(uia_mod.UIA_ControlTypePropertyId)
        self.parent_cache.AddProperty(uia_mod.UIA_NamePropertyId)
        self.walker = self.uia.ControlViewWalker
        self.root = self.uia.GetRootElement()
        self.types = {
            getattr(uia_mod, n): n[4:-13]
            for n in dir(uia_mod)
            if n.startswith("UIA_") and n.endswith("ControlTypeId")
        }
        self.process_names: dict[int, str] = {}

    # Element ---------------------------------------------------------------------

    def element_at(self, x: int, y: int, want: str) -> dict | None:
        U = self.U
        point = U.tagPOINT(x, y)
        el = self.uia.ElementFromPointBuildCache(point, self.cache)
        if not el:
            return None
        rect = el.CachedBoundingRectangle
        pid = int(el.CachedProcessId or 0)
        password = bool(el.CachedIsPassword)
        info: dict = {
            "role_raw": self.types.get(el.CachedControlType, str(el.CachedControlType)),
            "name": (el.CachedName or "")[:500],
            "automation_id": (el.CachedAutomationId or "")[:500],
            "class_name": (el.CachedClassName or "")[:500],
            "framework": el.CachedFrameworkId or "",
            "bounds": [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top],
            "is_password": password,
            "pid": pid or None,
            "process": self._process_name(pid),
            "ancestors": self._ancestors(el),
        }
        if not password and want in ("value_and_text", "selection"):
            content = self._selection(el) if want == "selection" else self._value_or_text(el, point)
            if content is not None:
                info["content"] = content
        return info

    def _ancestors(self, el) -> list[list[str]]:
        found: list[list[str]] = []
        cur = el
        for _ in range(MAX_ANCESTOR_STEPS):
            try:
                cur = self.walker.GetParentElementBuildCache(cur, self.parent_cache)
            except self.errors:
                break
            if not cur or self.uia.CompareElements(cur, self.root):
                break
            name = cur.CachedName or ""
            if name:
                role = self.types.get(cur.CachedControlType, str(cur.CachedControlType))
                found.append([role, name[:500]])
                if len(found) >= MAX_ANCESTORS:
                    break
        return found

    # Content ---------------------------------------------------------------------

    @staticmethod
    def _content(kind: str, text: str | None, chars: int | None = None) -> dict | None:
        if not text:
            return None
        return {
            "kind": kind,
            "text": text[:TEXT_REQUEST_CHARS],
            "chars": min(chars if chars is not None else len(text), 10**9),
        }

    def _value(self, el) -> str | None:
        if not el.GetCachedPropertyValue(self.U.UIA_IsValuePatternAvailablePropertyId):
            return None
        try:
            pattern = el.GetCurrentPattern(self.U.UIA_ValuePatternId)
            if not pattern:
                return None
            value = pattern.QueryInterface(self.U.IUIAutomationValuePattern).CurrentValue
            return value or None
        except self.errors:
            return None

    def _text_pattern(self, el):
        """The element's TextPattern, or that of its nearest ancestor that has one."""
        U = self.U
        cur = el
        for _ in range(TEXT_OWNER_STEPS + 1):
            if not cur:
                return None
            try:
                pattern = cur.GetCurrentPattern(U.UIA_TextPatternId)
                if pattern:
                    return pattern.QueryInterface(U.IUIAutomationTextPattern)
            except self.errors:
                return None
            try:
                cur = self.walker.GetParentElement(cur)
            except self.errors:
                return None
        return None

    def _paragraph(self, el, point) -> str | None:
        pattern = self._text_pattern(el)
        if pattern is None:
            return None
        try:
            rng = pattern.RangeFromPoint(point)
            rng.ExpandToEnclosingUnit(self.U.TextUnit_Paragraph)
            return rng.GetText(TEXT_REQUEST_CHARS) or None
        except self.errors:
            pass
        try:  # Firefox: no RangeFromPoint; the visible text instead
            ranges = pattern.GetVisibleRanges()
            parts: list[str] = []
            total = 0
            for i in range(ranges.Length if ranges else 0):
                text = ranges.GetElement(i).GetText(TEXT_REQUEST_CHARS - total) or ""
                parts.append(text)
                total += len(text)
                if total >= TEXT_REQUEST_CHARS:
                    break
            return "".join(parts) or None
        except self.errors:
            return None

    def _value_or_text(self, el, point) -> dict | None:
        value = self._value(el)
        if value is not None and len(value) <= LONG_VALUE_CHARS:
            return self._content("value", value)
        paragraph = self._paragraph(el, point)
        if paragraph:
            return self._content("text", paragraph)
        if value is not None:
            return self._content("value", value[:TEXT_REQUEST_CHARS], len(value))
        return None

    def _selection(self, el) -> dict | None:
        pattern = self._text_pattern(el)
        if pattern is None:
            return None
        try:
            ranges = pattern.GetSelection()
            parts: list[str] = []
            total = 0
            for i in range(ranges.Length if ranges else 0):
                text = ranges.GetElement(i).GetText(TEXT_REQUEST_CHARS - total) or ""
                parts.append(text)
                total += len(text)
                if total >= TEXT_REQUEST_CHARS:
                    break
            return self._content("selection", "".join(parts))
        except self.errors:
            return None

    # Processes -------------------------------------------------------------------

    def _process_name(self, pid: int) -> str:
        if not pid:
            return ""
        if pid not in self.process_names:
            self.process_names[pid] = process_name(pid)
        return self.process_names[pid]


def process_name(pid: int) -> str:
    """Executable name of a process (``notepad.exe``), ``""`` when it cannot be read."""
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value.rsplit("\\", 1)[-1]
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return ""


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
