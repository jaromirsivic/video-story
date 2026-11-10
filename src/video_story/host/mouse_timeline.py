"""Mouse actions of a recording as timeline lines, and their full detail.

:func:`mouse_events` turns ``input/mouse.jsonl`` into compact lines for
:func:`video_story.host.events.build_timeline` (``CLICK``, ``DOUBLE-CLICK``, ``DRAG``,
``SCROLL``, ``ZOOM-SCROLL``, ``GESTURE``) and tells the speech lines what the pointer
rested over. :func:`mouse_detail` returns everything the lines leave out (paths, bounds,
ancestors, the whole content up to 4096 characters) for the ``mouse`` command and the
``video_story_mouse`` MCP tool.

Rules against flooding the context (``implementation-plan-mouse.md`` ch. 11): one line per
action; an element name of at most 60 characters with one ancestor; the window only when
it changed since the previous action; content cut after 200 characters (the selection
after 1000) with a pointer to ``mouse --at``; content seen before is ``same as <ms>``.
Actions in Video Story's own windows and on the drawing layer are left out (KEYPOINT,
DRAW and ZOOM cover them). Over :data:`SUMMARY_AFTER_LINES` lines, scrolls and gestures
are merged per :data:`SUMMARY_BUCKET_MS`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from video_story.core.crop import Crop
from video_story.core.input_events import ElementRecord, MouseLog, MouseSystemParams, read_mouse_log
from video_story.core.mouse_actions import MouseAction, Point, derive_actions
from video_story.core.screens import Screen
from video_story.core.session import SessionPaths

MouseMode = Literal["full", "summary", "off", "auto"]
MOUSE_MODES = ("auto", "full", "summary", "off")

NAME_CHARS = 60
CONTENT_EXCERPT_CHARS = 200
SELECTION_CHARS = 1000
SUMMARY_AFTER_LINES = 400
SUMMARY_BUCKET_MS = 10_000
HOVER_BEFORE_SPEECH_MS = 3000  # a rest point this long before a sentence still counts
AT_TOLERANCE_MS = 1000

BUTTON_NAMES = {"x1": "x1 (back)", "x2": "x2 (forward)"}
LEGEND = (
    "CLICK / DOUBLE-CLICK / TRIPLE-CLICK / MULTI-CLICK n <button>, DRAG <button> a -> b, "
    "SCROLL <direction> n notches, ZOOM-SCROLL (Ctrl+wheel), GESTURE (two-finger "
    "touchpad pinch or scroll): what the user did with the mouse in other applications, "
    "at screen x,y (pixels of that screen's image). 'on <role> \"name\"' is the element "
    "under the mouse, 'in \"title\" [process]' the window when it changed; value / text / "
    "selected is its content (value of a field, paragraph at the click, selected text), "
    "cut after 200 characters - `mouse --at <ms>` has all of it; 'same as <ms>' repeats an "
    "earlier content. 'over ...' in a SAY line is the element the pointer rested on."
)


@dataclass
class MouseLine:
    time_ms: int
    text: str


@dataclass
class MouseTimeline:
    recorded: bool  # the recording has a mouse file
    mode: str = "off"
    lines: list[MouseLine] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    note: str | None = None
    over: Callable[[int, int], str | None] = lambda _s, _e: None


# Loading -------------------------------------------------------------------------


@dataclass
class _Loaded:
    log: MouseLog
    params: MouseSystemParams
    screens: list[Screen]
    duration_ms: int


def _load(paths: SessionPaths, manifest: dict[str, Any]) -> _Loaded | None:
    base = paths.current_dir
    path = paths.mouse_log_path(base)
    if not path.is_file():
        return None
    mouse = manifest.get("mouse") if isinstance(manifest.get("mouse"), dict) else {}
    screens = []
    for raw in manifest.get("screens") or []:
        try:
            screens.append(Screen.from_manifest(raw))
        except (KeyError, TypeError, ValueError):
            continue
    return _Loaded(
        log=read_mouse_log(path),
        params=MouseSystemParams.from_dict(mouse.get("params")),
        screens=screens,
        duration_ms=int(manifest.get("recorded_duration_ms") or 0),
    )


def _actions(loaded: _Loaded, window: Crop) -> list[MouseAction]:
    actions = derive_actions(loaded.log, loaded.params, loaded.duration_ms or None)
    return [a for a in actions if a.target == "app" and window.start_ms <= a.t <= window.end_ms]


# Formatting ----------------------------------------------------------------------


def _quote(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return json.dumps(flat, ensure_ascii=False)
    return json.dumps(flat[:limit] + "...", ensure_ascii=False)


def _where(p: Point) -> str:
    return f"{p.screen or '?'} {p.x},{p.y}"


def _element_text(el: ElementRecord | None) -> str:
    if el is None or el.error:
        return ""
    name = el.name or ""
    text = f" on {el.role}" + (f" {_quote(name, NAME_CHARS)}" if name else "")
    ancestor = next(
        (
            a
            for a in el.ancestors
            if isinstance(a, dict)
            and a.get("name")
            and a.get("name") != name
            and a.get("role") != "window"  # the window is named by the window part
        ),
        None,
    )
    if ancestor is not None:
        text += f" in {ancestor.get('role')} {_quote(str(ancestor.get('name')), NAME_CHARS)}"
    return text


class _Formatter:
    def __init__(self, loaded: _Loaded, frames: Callable[[int, int], str]) -> None:
        self.log = loaded.log
        self.frames = frames
        self.last_win: int | None = None
        self.seen: dict[str, int] = {}  # content hash -> time of the first line showing it

    def window(self, win: int | None) -> str:
        if win is None or win == self.last_win:
            return ""
        self.last_win = win
        record = self.log.windows.get(win)
        if record is None:
            return ""
        title = f" {_quote(record.title or '', 80)}" if record.title else ""
        return f" in{title} [{record.process or '?'}]"

    def content(self, el: ElementRecord | None, time_ms: int) -> str:
        if el is None or not el.content_hash:
            return ""
        kind = el.content_kind or "value"
        label = "selected" if kind == "selection" else kind
        digest = el.content_hash
        if digest in self.seen:
            return f" {label} same as {self.seen[digest]}"
        text = self.log.contents.get(digest)
        if not text:
            return ""
        self.seen[digest] = time_ms
        limit = SELECTION_CHARS if kind == "selection" else CONTENT_EXCERPT_CHARS
        flat = " ".join(text.split())
        excerpt = _quote(text, limit)
        chars = int((el.content or {}).get("chars") or len(text))
        if len(flat) > limit or (el.content or {}).get("truncated"):
            more = f"{chars}+" if (el.content or {}).get("truncated") else str(chars)
            excerpt += f" ({more} chars, `mouse --at {time_ms}`)"
        return f" {label} {excerpt}"

    def frames_part(self, a: MouseAction) -> str:
        frames = self.frames(a.t, a.until)
        return f" ({frames})" if frames else ""

    def flags(self, a: MouseAction) -> str:
        notes = []
        if a.held_ms:
            notes.append(f"held {a.held_ms / 1000:.1f} s")
        if a.mods:
            notes.append("+".join(m.capitalize() for m in a.mods))
        if a.injected:
            notes.append("injected")
        if a.unfinished:
            notes.append("still pressed at the end")
        return f" ({', '.join(notes)})" if notes else ""

    def line(self, a: MouseAction) -> str:
        el = self.log.element_for(a.ref) if a.ref is not None else None
        end_el = self.log.element_for(a.end_ref) if a.end_ref is not None else None
        win = self.window(a.win if a.win is not None else (el.win if el else None))
        if a.kind in ("click", "double_click", "multi_click"):
            name = {1: "CLICK", 2: "DOUBLE-CLICK", 3: "TRIPLE-CLICK"}.get(
                a.clicks, f"MULTI-CLICK {a.clicks}"
            )
            button = BUTTON_NAMES.get(a.button or "", a.button or "?")
            times = f"{a.t}" if a.clicks == 1 else f"{a.t}-{a.until}"
            text = f"{times} {name} {button} {_where(a.at)}{_element_text(el)}{win}"
            text += self.content(el, a.t)
            if a.clicks > 1 and end_el is not None and end_el.content_kind == "selection":
                text += self.content(end_el, a.t)
            return text + self.flags(a) + self.frames_part(a)
        if a.kind == "drag":
            end = a.end or a.at
            button = BUTTON_NAMES.get(a.button or "", a.button or "?")
            text = (
                f"{a.t}-{a.until} DRAG {button} {_where(a.at)} -> {_where(end)} "
                f"({a.duration_ms} ms, {a.length_px} px){_element_text(el)}{win}"
            )
            if end_el is not None and end_el.name != (el.name if el else None):
                text += f" to{_element_text(end_el).removeprefix(' on')}"
            if end_el is not None and end_el.content_kind == "selection":
                text += self.content(end_el, a.t)
            return text + self.flags(a) + self.frames_part(a)
        if a.kind in ("scroll", "zoom"):
            name = "ZOOM-SCROLL" if a.kind == "zoom" else "SCROLL"
            how = " (Ctrl+wheel)" if a.kind == "zoom" else ""
            over = _element_text(el).replace(" on ", " over ", 1)
            return (
                f"{a.t}-{a.until} {name} {a.direction} {a.notches:g} notches{how} "
                f"{_where(a.at)}{over}{win}{self.flags(a)}"
            )
        # gesture
        detail = f"x{a.scale:g}" if a.gesture == "pinch" and a.scale else ""
        return (
            f"{a.t}-{a.until} GESTURE {a.gesture} {a.direction or ''} 2 fingers {detail} "
            f"{_where(a.at)}{win} (touchpad)"
        ).replace("  ", " ")


def action_lines(log: MouseLog, actions: list[MouseAction]) -> list[str]:
    """Timeline lines of ``actions`` (in this order) without the frame times, e.g. for
    the editor's mouse window; the window and repeated content are named as in the
    timeline (only when they changed, ``same as <ms>``)."""
    loaded = _Loaded(log=log, params=MouseSystemParams(), screens=[], duration_ms=0)
    formatter = _Formatter(loaded, lambda _t, _u: "")
    return [formatter.line(a) for a in actions]


def _summarize(actions: list[MouseAction], formatter: _Formatter) -> list[MouseLine]:
    """Clicks and drags stay; scrolls and gestures merge per SUMMARY_BUCKET_MS."""
    lines: list[MouseLine] = []
    group: list[MouseAction] = []

    def flush() -> None:
        if not group:
            return
        if len(group) == 1:
            lines.append(MouseLine(group[0].t, formatter.line(group[0])))
        else:
            parts: dict[str, float] = {}
            for a in group:
                zoom = "zoom " if a.kind == "zoom" else ""
                gesture = f"{a.gesture} " if a.gesture else ""
                key = f"{zoom}{gesture}{a.direction}"
                parts[key] = parts.get(key, 0) + (a.notches or 1)
            what = ", ".join(f"{k} {v:g}" for k, v in parts.items())
            first = group[0]
            lines.append(
                MouseLine(
                    first.t,
                    f"{first.t}-{group[-1].until} SCROLLS {len(group)}x ({what}) "
                    f"{_where(first.at)}{formatter.window(first.win)}",
                )
            )
        group.clear()

    for a in actions:
        if a.kind in ("scroll", "zoom", "gesture"):
            if group and a.t - group[0].t > SUMMARY_BUCKET_MS:
                flush()
            group.append(a)
            continue
        flush()
        lines.append(MouseLine(a.t, formatter.line(a)))
    flush()
    return lines


def mouse_events(
    paths: SessionPaths,
    manifest: dict[str, Any],
    window: Crop,
    frames: Callable[[int, int], str],
    mode: str = "auto",
) -> MouseTimeline:
    """Timeline lines of the mouse in ``window``; ``frames(t, until)`` names the frames
    before and after an action."""
    if mode not in MOUSE_MODES:
        raise ValueError(f"unknown mouse mode {mode!r}; use {', '.join(MOUSE_MODES)}")
    loaded = _load(paths, manifest)
    if loaded is None:
        return MouseTimeline(recorded=False)
    actions = _actions(loaded, window)
    counts = {
        "clicks": sum(1 for a in actions if a.kind in ("click", "double_click", "multi_click")),
        "drags": sum(1 for a in actions if a.kind == "drag"),
        "scrolls": sum(1 for a in actions if a.kind in ("scroll", "zoom")),
        "gestures": sum(1 for a in actions if a.kind == "gesture"),
    }
    result = MouseTimeline(recorded=True, counts=counts, over=_over(loaded, window))
    if mode == "off":
        result.mode = "off"
        return result
    formatter = _Formatter(loaded, frames)
    note = None
    if mode == "auto":
        mode = "summary" if len(actions) > SUMMARY_AFTER_LINES else "full"
        if mode == "summary":
            note = (
                f"{len(actions)} mouse actions in the range: scrolls and gestures are merged per "
                f"{SUMMARY_BUCKET_MS // 1000} s; use `timeline --mouse full` or `mouse` for each."
            )
    if mode == "summary":
        result.lines = _summarize(actions, formatter)
    else:
        result.lines = [MouseLine(a.t, formatter.line(a)) for a in actions]
    result.mode = mode
    result.note = note
    return result


def _over(loaded: _Loaded, window: Crop) -> Callable[[int, int], str | None]:
    """What the pointer rested on around a sentence: the latest rest point up to
    HOVER_BEFORE_SPEECH_MS before it starts, else the first one inside it."""
    log = loaded.log
    rests = []
    for hover in log.hovers:
        el = log.element_for(hover.seq)
        if el is None or el.error or not (el.name or el.role not in ("pane", "other")):
            continue
        process = log.windows[el.win].process if el.win in log.windows else None
        name = f" {_quote(el.name, NAME_CHARS)}" if el.name else ""
        rests.append((hover.t, f"over {el.role}{name}" + (f" ({process})" if process else "")))

    def over(start: int, end: int) -> str | None:
        before = [text for t, text in rests if start - HOVER_BEFORE_SPEECH_MS <= t <= start]
        if before:
            return before[-1]
        inside = [text for t, text in rests if start < t <= end]
        return inside[0] if inside else None

    return over


# Detail ---------------------------------------------------------------------------


def _local(loaded: _Loaded, gx: int, gy: int) -> dict[str, Any] | None:
    for screen in loaded.screens:
        if screen.contains(gx, gy):
            x, y = screen.to_local(gx, gy)
            return {"screen": screen.id, "x": x, "y": y}
    return None


def _element_detail(loaded: _Loaded, el: ElementRecord | None) -> dict[str, Any] | None:
    if el is None:
        return None
    if el.error:
        return {"error": el.error, "ms": el.ms}
    data: dict[str, Any] = {
        "role": el.role,
        "role_raw": el.role_raw,
        "name": el.name,
        "automation_id": el.automation_id,
        "class_name": el.class_name,
        "framework": el.framework,
        "password": el.password,
        "ancestors": list(el.ancestors),
        "query_ms": el.ms,
    }
    if el.bounds:
        bx, by, bw, bh = el.bounds
        top_left = _local(loaded, bx, by)
        data["bounds"] = (
            {**top_left, "width": bw, "height": bh}
            if top_left
            else {"gx": bx, "gy": by, "width": bw, "height": bh}
        )
    if el.content:
        data["content"] = {
            "kind": el.content_kind,
            "chars": el.content.get("chars"),
            "truncated": bool(el.content.get("truncated")),
            "text": loaded.log.contents.get(el.content_hash or ""),
        }
    if el.win in loaded.log.windows:
        w = loaded.log.windows[el.win]
        data["window"] = {"title": w.title, "process": w.process}
    return {k: v for k, v in data.items() if v not in (None, "", [], False)}


def _point_detail(p: Point) -> dict[str, Any]:
    return {"screen": p.screen, "x": p.x, "y": p.y, "gx": p.gx, "gy": p.gy}


def _action_detail(loaded: _Loaded, a: MouseAction) -> dict[str, Any]:
    log = loaded.log
    data: dict[str, Any] = {
        "kind": a.kind,
        "t": a.t,
        "until": a.until,
        "button": a.button,
        "clicks": a.clicks if a.kind != "drag" else None,
        "at": _point_detail(a.at),
        "end": _point_detail(a.end) if a.end else None,
        "path": [_local(loaded, x, y) or {"gx": x, "gy": y} for x, y in a.path] or None,
        "length_px": a.length_px or None,
        "axis": a.axis,
        "direction": a.direction,
        "notches": a.notches or None,
        "gesture": a.gesture,
        "scale": a.scale,
        "held_ms": a.held_ms,
        "mods": list(a.mods) or None,
        "injected": a.injected or None,
        "unfinished": a.unfinished or None,
        "element": _element_detail(loaded, log.element_for(a.ref) if a.ref is not None else None),
    }
    if a.end_ref is not None and a.end_ref != a.ref:
        data["end_element"] = _element_detail(loaded, log.element_for(a.end_ref))
    win = log.windows.get(a.win) if a.win is not None else None
    if win is not None:
        data["window"] = {"title": win.title, "process": win.process}
    return {k: v for k, v in data.items() if v is not None}


def mouse_detail(
    paths: SessionPaths,
    manifest: dict[str, Any],
    window: Crop,
    at_ms: int | None = None,
) -> dict[str, Any]:
    """Every mouse action (and rest point) in ``window`` with everything recorded about
    it; with ``at_ms`` only the action at that time (the time a timeline line starts
    with, or the nearest one within AT_TOLERANCE_MS)."""
    loaded = _load(paths, manifest)
    mouse = manifest.get("mouse") if isinstance(manifest.get("mouse"), dict) else None
    if loaded is None:
        return {"recorded": False, "mouse": mouse, "actions": [], "rest_points": []}
    actions = derive_actions(loaded.log, loaded.params, loaded.duration_ms or None)
    actions = [a for a in actions if a.target == "app"]
    rests = loaded.log.hovers
    if at_ms is not None:
        exact = [a for a in actions if a.t == at_ms]
        if not exact and actions:
            nearest = min(actions, key=lambda a: abs(a.t - at_ms))
            exact = [nearest] if abs(nearest.t - at_ms) <= AT_TOLERANCE_MS else []
        actions = exact
        rests = [h for h in rests if h.t == at_ms]
    else:
        actions = [a for a in actions if window.start_ms <= a.t <= window.end_ms]
        rests = [h for h in rests if window.start_ms <= h.t <= window.end_ms]
    return {
        "recorded": True,
        "range": window.to_dict(),
        "at_ms": at_ms,
        "params": loaded.params.to_dict(),
        "actions": [_action_detail(loaded, a) for a in actions],
        "rest_points": [
            {
                "t": h.t,
                "at": {"screen": h.screen, "x": h.x, "y": h.y},
                "element": _element_detail(loaded, loaded.log.element_for(h.seq)),
            }
            for h in rests
        ],
        "note": "coordinates are pixels of that screen's image (gx/gy: virtual desktop); "
        "content is cut at 4096 characters (truncated: true)",
    }


__all__ = [
    "LEGEND",
    "MOUSE_MODES",
    "BUTTON_NAMES",
    "action_lines",
    "MouseTimeline",
    "mouse_detail",
    "mouse_events",
]
