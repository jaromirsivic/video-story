"""Mouse input of a recording: schema of ``input/mouse.jsonl``, its writer and its reader.

The file holds *raw* events (``down``, ``up``, ``move``, ``wheel``, ``gesture``) plus what
was found under the mouse (``element``, ``content``, ``window``, ``hover``) and the
health of the source (``status``). What the user did - click, double click, drag,
scroll - is derived when the file is read (:mod:`video_story.core.mouse_actions`), so
the rules can change without touching old recordings.

One JSON object per line, UTF-8, ``seq`` ascending, ``t`` in milliseconds of the
recording timeline. Coordinates: ``screen``/``x``/``y`` are pixels of that screen's
image (the same as the cursor in the frame sidecars), ``gx``/``gy`` physical pixels of
the virtual desktop (distances are measured there, also across screens).

Nothing here depends on the operating system or Qt: every platform fills the same
records, and the limits (4096 characters of content, the ``...`` cut, the hash that
stores each content once, never the content of a password field) apply everywhere.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal

from .textcut import truncate_text

log = logging.getLogger(__name__)

MAX_ELEMENT_TEXT_CHARS = 4096  # content of an element (value, paragraph, selection)
MAX_ELEMENT_NAME_CHARS = 200
MAX_WINDOW_TITLE_CHARS = 120
MAX_ANCESTORS = 3
PASSWORD_NAME = "(password field)"

FLUSH_INTERVAL_S = 1.0  # a crash loses at most the last second

# Wheel events of one axis and the same modifiers closer than this are one line.
WHEEL_COALESCE_MS = 40
# Path of a drag: one point per this many ms and at least this many px of movement;
# the interval doubles after every MOVE_POINTS_PER_STEP points of the same drag.
MOVE_MIN_INTERVAL_MS = 16
MOVE_MIN_DISTANCE_PX = 2
MOVE_POINTS_PER_STEP = 1000


class MouseButton(StrEnum):
    LEFT = "left"
    MIDDLE = "middle"
    RIGHT = "right"
    X1 = "x1"  # back
    X2 = "x2"  # forward
    # Windows knows five buttons only; X11 and macOS report more.
    B6 = "b6"
    B7 = "b7"
    B8 = "b8"
    B9 = "b9"
    B10 = "b10"
    B11 = "b11"


class Target(StrEnum):
    APP = "app"  # a window of another application
    VIDEO_STORY = "video_story"  # Video Story's own control window or dialogs
    DRAWING = "drawing"  # the drawing layer (pen, zoom); covered by DRAW / ZOOM


class ContentKind(StrEnum):
    VALUE = "value"  # value of a field, combo box, address bar, cell
    TEXT = "text"  # the paragraph at the mouse (not the whole document)
    SELECTION = "selection"  # the selected text after a drag or a multi click


class ContentRequest(StrEnum):
    """What an element inspector reads besides the element itself."""

    VALUE_AND_TEXT = "value_and_text"  # at a press, a hover
    SELECTION = "selection"  # after a drag or a multi click
    NONE = "none"  # role and name only


MODIFIERS = ("ctrl", "shift", "alt", "meta")


def content_hash(text: str) -> str:
    """SHA-1 of the (already cut) text: equal contents are stored once."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def normalize_modifiers(mods: Any) -> tuple[str, ...]:
    """Known modifier names, in a fixed order."""
    if not mods:
        return ()
    present = {str(m).lower() for m in mods}
    return tuple(m for m in MODIFIERS if m in present)


# Values handed over by a platform ----------------------------------------------


@dataclass(frozen=True)
class RawMouseEvent:
    """What a platform's mouse source reports; no interpretation."""

    kind: Literal["down", "up", "move", "wheel"]
    t_ms: int  # recording clock
    gx: int  # physical pixels of the virtual desktop
    gy: int
    button: MouseButton | None = None
    axis: Literal["v", "h"] | None = None
    delta: int = 0  # wheel: 120 = one notch, positive = up / right
    modifiers: tuple[str, ...] = ()
    injected: bool = False  # created by a program (SendInput, remote desktop)
    window: int | None = None  # native handle of the top-level window under the point
    pid: int | None = None  # process of that window


@dataclass(frozen=True)
class MouseCapabilities:
    buttons: tuple[MouseButton, ...] = ()
    horizontal_wheel: bool = False
    high_resolution_wheel: bool = False
    touchpad_gestures: bool = False  # two-finger pinch and scroll straight from the touchpad
    elements: bool = False
    element_content: bool = False  # value, text at the point, selection

    def to_dict(self) -> dict[str, Any]:
        return {
            "buttons": [str(b) for b in self.buttons],
            "horizontal_wheel": self.horizontal_wheel,
            "high_resolution_wheel": self.high_resolution_wheel,
            "touchpad_gestures": self.touchpad_gestures,
            "elements": self.elements,
            "element_content": self.element_content,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MouseCapabilities:
        data = data or {}
        buttons = []
        for name in data.get("buttons", []):
            try:
                buttons.append(MouseButton(name))
            except ValueError:
                continue
        return cls(
            buttons=tuple(buttons),
            horizontal_wheel=bool(data.get("horizontal_wheel", False)),
            high_resolution_wheel=bool(data.get("high_resolution_wheel", False)),
            touchpad_gestures=bool(data.get("touchpad_gestures", False)),
            elements=bool(data.get("elements", False)),
            element_content=bool(data.get("element_content", False)),
        )


@dataclass(frozen=True)
class MouseSystemParams:
    """System settings the classification uses; stored in the manifest."""

    double_click_ms: int = 500
    double_click_px: tuple[int, int] = (4, 4)  # rectangle centred on the first click
    drag_px: tuple[int, int] = (4, 4)  # movement beyond this many px starts a drag

    def to_dict(self) -> dict[str, Any]:
        return {
            "double_click_ms": self.double_click_ms,
            "double_click_px": list(self.double_click_px),
            "drag_px": list(self.drag_px),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MouseSystemParams:
        data = data or {}
        default = cls()

        def pair(key: str, fallback: tuple[int, int]) -> tuple[int, int]:
            value = data.get(key)
            if isinstance(value, list | tuple) and len(value) == 2:
                try:
                    return (max(0, int(value[0])), max(0, int(value[1])))
                except (TypeError, ValueError):
                    return fallback
            return fallback

        try:
            dbl = int(data.get("double_click_ms", default.double_click_ms))
        except (TypeError, ValueError):
            dbl = default.double_click_ms
        return cls(
            double_click_ms=max(1, dbl),
            double_click_px=pair("double_click_px", default.double_click_px),
            drag_px=pair("drag_px", default.drag_px),
        )


@dataclass(frozen=True)
class ElementContent:
    """Content of an element, already cut to :data:`MAX_ELEMENT_TEXT_CHARS`."""

    kind: ContentKind
    text: str  # cut text (with ``...`` when it was longer)
    chars: int  # length of the original text
    truncated: bool

    @property
    def hash(self) -> str:
        return content_hash(self.text)


def make_content(
    kind: ContentKind | str, text: str | None, limit: int = MAX_ELEMENT_TEXT_CHARS
) -> ElementContent | None:
    """``None`` for an empty text; otherwise the cut content.

    An inspector asks the OS for ``limit + 1`` characters (the APIs return at most
    what is asked and do not say whether there is more), so ``chars`` is exact up to
    ``limit`` and ``limit + 1`` means "longer".
    """
    if not text:
        return None
    return ElementContent(
        kind=ContentKind(kind),
        text=truncate_text(text, limit),
        chars=len(text),
        truncated=len(text) > limit,
    )


@dataclass(frozen=True)
class Ancestor:
    role: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "name": self.name}


@dataclass(frozen=True)
class ElementInfo:
    """The element under a point, as an element inspector reports it."""

    role: str  # normalised, see normalize_role
    role_raw: str = ""  # the platform's own name (UIA control type, AXRole, AT-SPI role)
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    framework: str = ""
    bounds: tuple[int, int, int, int] | None = None  # x, y, width, height (physical px)
    is_password: bool = False
    ancestors: tuple[Ancestor, ...] = ()
    process: str = ""
    pid: int | None = None
    window_title: str = ""
    content: ElementContent | None = None
    query_ms: int | None = None  # how long the platform took (for debugging)

    def sanitized(self) -> ElementInfo:
        """Limits applied; a password field keeps no name and no content."""
        name = PASSWORD_NAME if self.is_password else _clip(self.name, MAX_ELEMENT_NAME_CHARS)
        ancestors = tuple(
            Ancestor(a.role, _clip(a.name, MAX_ELEMENT_NAME_CHARS))
            for a in self.ancestors
            if a.name
        )[:MAX_ANCESTORS]
        content = None if self.is_password else self.content
        if content is not None and len(content.text) > MAX_ELEMENT_TEXT_CHARS + len("..."):
            content = make_content(content.kind, content.text)
        return ElementInfo(
            role=self.role,
            role_raw=self.role_raw,
            name=name,
            automation_id=_clip(self.automation_id, MAX_ELEMENT_NAME_CHARS),
            class_name=_clip(self.class_name, MAX_ELEMENT_NAME_CHARS),
            framework=self.framework,
            bounds=self.bounds,
            is_password=self.is_password,
            ancestors=ancestors,
            process=self.process,
            pid=self.pid,
            window_title=_clip(self.window_title, MAX_WINDOW_TITLE_CHARS),
            content=content,
            query_ms=self.query_ms,
        )


@dataclass(frozen=True)
class WindowInfo:
    title: str = ""
    process: str = ""
    pid: int | None = None


def _clip(text: str, limit: int) -> str:
    return truncate_text(text or "", limit)


# Normalised roles ---------------------------------------------------------------

ROLES = (
    "button",
    "edit",
    "link",
    "menu_item",
    "tab",
    "list_item",
    "tree_item",
    "checkbox",
    "radio",
    "combobox",
    "cell",
    "document",
    "text",
    "image",
    "scrollbar",
    "slider",
    "toolbar",
    "menu",
    "list",
    "tree",
    "table",
    "pane",
    "window",
    "other",
)

_ROLE_ALIASES: dict[str, str] = {
    # Windows UI Automation control types
    "button": "button",
    "splitbutton": "button",
    "edit": "edit",
    "hyperlink": "link",
    "menuitem": "menu_item",
    "tabitem": "tab",
    "listitem": "list_item",
    "treeitem": "tree_item",
    "checkbox": "checkbox",
    "radiobutton": "radio",
    "combobox": "combobox",
    "dataitem": "cell",
    "document": "document",
    "text": "text",
    "image": "image",
    "scrollbar": "scrollbar",
    "slider": "slider",
    "toolbar": "toolbar",
    "menu": "menu",
    "menubar": "menu",
    "list": "list",
    "tree": "tree",
    "table": "table",
    "datagrid": "table",
    "pane": "pane",
    "group": "pane",
    "window": "window",
    # macOS accessibility roles
    "axbutton": "button",
    "axmenubutton": "button",
    "axpopupbutton": "combobox",
    "axtextfield": "edit",
    "axtextarea": "edit",
    "axsearchfield": "edit",
    "axcombobox": "combobox",
    "axsecuretextfield": "edit",
    "axlink": "link",
    "axmenuitem": "menu_item",
    "axmenubaritem": "menu_item",
    "axtab": "tab",
    "axrow": "list_item",
    "axoutlinerow": "tree_item",
    "axcheckbox": "checkbox",
    "axradiobutton": "radio",
    "axcell": "cell",
    "axwebarea": "document",
    "axstatictext": "text",
    "aximage": "image",
    "axscrollbar": "scrollbar",
    "axslider": "slider",
    "axtoolbar": "toolbar",
    "axmenu": "menu",
    "axmenubar": "menu",
    "axlist": "list",
    "axoutline": "tree",
    "axtable": "table",
    "axgroup": "pane",
    "axscrollarea": "pane",
    "axwindow": "window",
    "axsheet": "window",
    # Linux AT-SPI2 roles
    "push button": "button",
    "toggle button": "button",
    "entry": "edit",
    "password text": "edit",
    "spin button": "edit",
    "link": "link",
    "menu item": "menu_item",
    "check menu item": "menu_item",
    "radio menu item": "menu_item",
    "page tab": "tab",
    "list item": "list_item",
    "tree item": "tree_item",
    "check box": "checkbox",
    "radio button": "radio",
    "combo box": "combobox",
    "table cell": "cell",
    "document web": "document",
    "document text": "document",
    "document frame": "document",
    "label": "text",
    "static": "text",
    "icon": "image",
    "scroll bar": "scrollbar",
    "tool bar": "toolbar",
    "menu bar": "menu",
    "tree table": "tree",
    "panel": "pane",
    "filler": "pane",
    "scroll pane": "pane",
    "frame": "window",
    "dialog": "window",
}


def normalize_role(raw: str | None) -> str:
    """The platform's role name mapped onto :data:`ROLES` (``other`` when unknown)."""
    if not raw:
        return "other"
    key = raw.strip().lower()
    if key.endswith("controltypeid"):  # "UIA_ButtonControlTypeId"
        key = key.removeprefix("uia_").removesuffix("controltypeid")
    elif key.startswith("role_"):  # pyatspi "ROLE_PUSH_BUTTON"
        key = key.removeprefix("role_").replace("_", " ")
    return _ROLE_ALIASES.get(key, "other")


# Records of the file -------------------------------------------------------------


def _drop_empty(data: dict[str, Any]) -> dict[str, Any]:
    """Compact lines: no ``None``, no empty lists, no ``False`` flags."""
    return {
        k: v for k, v in data.items() if v is not None and v != [] and v != () and v is not False
    }


@dataclass(frozen=True)
class _Record:
    TYPE: ClassVar[str] = ""
    seq: int = 0
    t: int = 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"seq": self.seq, "t": self.t, "type": self.TYPE}
        for f in fields(self):
            if f.name in ("seq", "t"):
                continue
            value = getattr(self, f.name)
            if isinstance(value, tuple):
                value = list(value)
            out[f.name] = value
        return _drop_empty(out) | {"seq": self.seq, "t": self.t, "type": self.TYPE}


@dataclass(frozen=True)
class _PointRecord(_Record):
    screen: str | None = None
    x: int | None = None
    y: int | None = None
    gx: int = 0
    gy: int = 0


@dataclass(frozen=True)
class ButtonRecord(_PointRecord):
    """``down`` or ``up`` of one button."""

    TYPE: ClassVar[str] = "button"
    kind: Literal["down", "up"] = "down"
    button: str = MouseButton.LEFT.value
    mods: tuple[str, ...] = ()
    target: str = Target.APP.value
    win: int | None = None
    injected: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["type"] = data.pop("kind")
        data["target"] = self.target  # always written: the timeline filters on it
        return data


@dataclass(frozen=True)
class MoveRecord(_PointRecord):
    """A point of a drag path (moves without a pressed button are not stored)."""

    TYPE: ClassVar[str] = "move"


@dataclass(frozen=True)
class WheelRecord(_PointRecord):
    """Wheel events of one axis merged within :data:`WHEEL_COALESCE_MS`."""

    TYPE: ClassVar[str] = "wheel"
    axis: Literal["v", "h"] = "v"
    delta: int = 0  # sum; 120 = one notch, positive = up / right
    count: int = 1
    until: int | None = None  # time of the last merged event
    mods: tuple[str, ...] = ()
    target: str = Target.APP.value
    win: int | None = None
    injected: bool = False

    @property
    def end(self) -> int:
        return self.until if self.until is not None else self.t


@dataclass(frozen=True)
class HoverRecord(_PointRecord):
    """The cursor stopped here (an element query follows)."""

    TYPE: ClassVar[str] = "hover"


@dataclass(frozen=True)
class GestureRecord(_PointRecord):
    """A finished two-finger touchpad gesture (see :mod:`video_story.core.gestures`).

    The point is the cursor when the fingers touched the pad."""

    TYPE: ClassVar[str] = "gesture"
    kind: Literal["pinch", "scroll"] = "scroll"
    fingers: int = 2
    until: int | None = None
    scale: float | None = None  # pinch: end distance / start distance
    direction: str | None = None  # pinch: in / out; scroll: up / down / left / right (fingers)
    distance: float | None = None  # scroll: share of the touchpad width the fingers moved

    @property
    def end(self) -> int:
        return self.until if self.until is not None else self.t


@dataclass(frozen=True)
class ElementRecord(_Record):
    """What was under the mouse at the event ``ref`` (written when the query returns)."""

    TYPE: ClassVar[str] = "element"
    ref: int = 0
    role: str = "other"
    role_raw: str | None = None
    name: str | None = None
    automation_id: str | None = None
    class_name: str | None = None
    framework: str | None = None
    bounds: tuple[int, int, int, int] | None = None
    password: bool = False
    ancestors: tuple[dict[str, str], ...] = ()
    win: int | None = None
    content: dict[str, Any] | None = None  # {"kind", "hash", "chars", "truncated"}
    ms: int | None = None
    error: str | None = None  # "timeout", "unavailable", ...

    @property
    def content_kind(self) -> str | None:
        return self.content.get("kind") if self.content else None

    @property
    def content_hash(self) -> str | None:
        return self.content.get("hash") if self.content else None


@dataclass(frozen=True)
class ContentRecord(_Record):
    """A content text, stored once; elements refer to it by ``hash``."""

    TYPE: ClassVar[str] = "content"
    hash: str = ""
    text: str = ""


@dataclass(frozen=True)
class WindowRecord(_Record):
    """A top-level window, numbered within the recording (``win`` of other records)."""

    TYPE: ClassVar[str] = "window"
    win: int = 0
    title: str | None = None
    process: str | None = None
    pid: int | None = None


@dataclass(frozen=True)
class StatusRecord(_Record):
    """Health of the mouse source: started, restarted, stopped, failed."""

    TYPE: ClassVar[str] = "status"
    source: str = "mouse"
    state: str = ""
    reason: str | None = None


_BY_TYPE: dict[str, type[_Record]] = {
    "down": ButtonRecord,
    "up": ButtonRecord,
    "move": MoveRecord,
    "wheel": WheelRecord,
    "hover": HoverRecord,
    "gesture": GestureRecord,
    "element": ElementRecord,
    "content": ContentRecord,
    "window": WindowRecord,
    "status": StatusRecord,
}


def record_from_dict(data: dict[str, Any]) -> _Record | None:
    """One line of the file; ``None`` for an unknown type (newer writer)."""
    kind = data.get("type")
    cls = _BY_TYPE.get(str(kind))
    if cls is None:
        return None
    known = {f.name: f for f in fields(cls)}
    values: dict[str, Any] = {}
    for key, value in data.items():
        if key in known and not (cls is ButtonRecord and key == "kind"):
            if isinstance(value, list):
                value = tuple(value)
            values[key] = value
    if cls is ButtonRecord:
        values["kind"] = kind
    values["seq"] = int(data.get("seq", 0))
    values["t"] = int(data.get("t", 0))
    return cls(**values)


@dataclass
class MouseLog:
    """Content of ``input/mouse.jsonl``, by record type."""

    buttons: list[ButtonRecord] = field(default_factory=list)
    moves: list[MoveRecord] = field(default_factory=list)
    wheels: list[WheelRecord] = field(default_factory=list)
    hovers: list[HoverRecord] = field(default_factory=list)
    gestures: list[GestureRecord] = field(default_factory=list)
    elements: dict[int, list[ElementRecord]] = field(default_factory=dict)  # by ref
    contents: dict[str, str] = field(default_factory=dict)  # hash -> text
    windows: dict[int, WindowRecord] = field(default_factory=dict)
    statuses: list[StatusRecord] = field(default_factory=list)
    bad_lines: int = 0

    def add(self, record: _Record) -> None:
        if isinstance(record, ButtonRecord):
            self.buttons.append(record)
        elif isinstance(record, MoveRecord):
            self.moves.append(record)
        elif isinstance(record, WheelRecord):
            self.wheels.append(record)
        elif isinstance(record, HoverRecord):
            self.hovers.append(record)
        elif isinstance(record, GestureRecord):
            self.gestures.append(record)
        elif isinstance(record, ElementRecord):
            self.elements.setdefault(record.ref, []).append(record)
        elif isinstance(record, ContentRecord):
            self.contents.setdefault(record.hash, record.text)
        elif isinstance(record, WindowRecord):
            self.windows[record.win] = record
        elif isinstance(record, StatusRecord):
            self.statuses.append(record)

    def element_for(self, seq: int) -> ElementRecord | None:
        """The first element reported for the event ``seq``."""
        found = self.elements.get(seq)
        return found[0] if found else None

    def content_of(self, element: ElementRecord | None) -> str | None:
        if element is None or not element.content_hash:
            return None
        return self.contents.get(element.content_hash)


def read_mouse_log(path: Path) -> MouseLog:
    """Read the file; a cut last line (crash) and unknown types are skipped."""
    result = MouseLog()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return result
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            record = record_from_dict(data) if isinstance(data, dict) else None
        except (ValueError, TypeError):
            result.bad_lines += 1
            continue
        if record is not None:
            result.add(record)
    for items in (result.buttons, result.moves, result.wheels, result.hovers, result.gestures):
        items.sort(key=lambda r: r.seq)
    return result


# Writer ------------------------------------------------------------------------


class MouseLogWriter:
    """Appends records to ``input/mouse.jsonl``; thread safe.

    Assigns ``seq``, stores every content text once (by hash) and every window once
    (by native handle), and flushes at least every :data:`FLUSH_INTERVAL_S`.
    """

    def __init__(self, path: Path, clock: Any = time.monotonic) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = 0
        self._file = None
        self._last_flush = 0.0
        self._contents: set[str] = set()
        self._windows: dict[tuple[int, str, str], int] = {}
        self.counts: dict[str, int] = {"events": 0, "elements": 0, "contents": 0, "windows": 0}

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8", newline="\n")
        self._last_flush = self._clock()

    @property
    def is_open(self) -> bool:
        return self._file is not None

    def next_seq(self) -> int:
        """Reserve a sequence number (an element refers to its event before it is written)."""
        with self._lock:
            self._seq += 1
            return self._seq

    def write(self, record: _Record) -> int:
        """Write ``record`` (``seq`` 0 = assign the next one); returns its ``seq``."""
        with self._lock:
            if record.seq == 0:
                self._seq += 1
                record = _with_seq(record, self._seq)
            self._write_line(record.to_dict())
            if isinstance(record, ButtonRecord | MoveRecord | WheelRecord | GestureRecord):
                self.counts["events"] += 1
            return record.seq

    def window_id(self, t: int, handle: int, info: WindowInfo) -> int:
        """Number of the window (handle + title + process); writes it on first sight.

        A window whose title changes gets a new number, so every action shows the title
        it had at that moment."""
        title = _clip(info.title, MAX_WINDOW_TITLE_CHARS)
        key = (handle, title, info.process)
        with self._lock:
            if key in self._windows:
                return self._windows[key]
            win = len(self._windows) + 1
            self._windows[key] = win
            self._seq += 1
            record = WindowRecord(
                seq=self._seq,
                t=t,
                win=win,
                title=title or None,
                process=info.process or None,
                pid=info.pid,
            )
            self._write_line(record.to_dict())
            self.counts["windows"] += 1
            return win

    def write_element(
        self, t: int, ref: int, info: ElementInfo, win: int | None = None, ms: int | None = None
    ) -> int:
        """Element for event ``ref`` plus its content line when the text is new."""
        info = info.sanitized()
        content_meta = None
        with self._lock:
            if info.content is not None:
                digest = info.content.hash
                content_meta = {
                    "kind": str(info.content.kind),
                    "hash": digest,
                    "chars": info.content.chars,
                }
                if info.content.truncated:
                    content_meta["truncated"] = True
                if digest not in self._contents:
                    self._contents.add(digest)
                    self._seq += 1
                    self._write_line(
                        ContentRecord(
                            seq=self._seq, t=t, hash=digest, text=info.content.text
                        ).to_dict()
                    )
                    self.counts["contents"] += 1
            self._seq += 1
            record = ElementRecord(
                seq=self._seq,
                t=t,
                ref=ref,
                role=info.role,
                role_raw=info.role_raw or None,
                name=info.name or None,
                automation_id=info.automation_id or None,
                class_name=info.class_name or None,
                framework=info.framework or None,
                bounds=info.bounds,
                password=info.is_password,
                ancestors=tuple(a.to_dict() for a in info.ancestors),
                win=win,
                content=content_meta,
                ms=ms if ms is not None else info.query_ms,
            )
            self._write_line(record.to_dict())
            self.counts["elements"] += 1
            return record.seq

    def write_element_error(self, t: int, ref: int, error: str, ms: int | None = None) -> int:
        with self._lock:
            self._seq += 1
            self._write_line(
                ElementRecord(seq=self._seq, t=t, ref=ref, error=error, ms=ms).to_dict()
            )
            return self._seq

    def flush(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()
                self._last_flush = self._clock()

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.flush()
                self._file.close()
                self._file = None

    def _write_line(self, data: dict[str, Any]) -> None:
        if self._file is None:
            raise RuntimeError("mouse log is not open")
        self._file.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
        now = self._clock()
        if now - self._last_flush >= FLUSH_INTERVAL_S:
            self._file.flush()
            self._last_flush = now


def _with_seq(record: _Record, seq: int) -> _Record:
    values = {f.name: getattr(record, f.name) for f in fields(record)}
    values["seq"] = seq
    return type(record)(**values)


# Thinning before writing -------------------------------------------------------


class WheelCoalescer:
    """Merges wheel events of one axis, modifiers and target within ``gap_ms``.

    :meth:`add` returns the records that are finished by the new event; call
    :meth:`flush` when nothing came for a while and at the end of the recording."""

    def __init__(self, gap_ms: int = WHEEL_COALESCE_MS) -> None:
        self.gap_ms = gap_ms
        self._open: WheelRecord | None = None

    def add(self, record: WheelRecord) -> list[WheelRecord]:
        current = self._open
        if (
            current is not None
            and current.axis == record.axis
            and current.mods == record.mods
            and current.target == record.target
            and (current.delta >= 0) == (record.delta >= 0)
            and record.t - current.end <= self.gap_ms
        ):
            self._open = WheelRecord(
                seq=current.seq,
                t=current.t,
                screen=current.screen,
                x=current.x,
                y=current.y,
                gx=current.gx,
                gy=current.gy,
                axis=current.axis,
                delta=current.delta + record.delta,
                count=current.count + record.count,
                until=record.end,
                mods=current.mods,
                target=current.target,
                win=current.win,
                injected=current.injected or record.injected,
            )
            return []
        self._open = record
        return [current] if current is not None else []

    def due(self, now_ms: int) -> list[WheelRecord]:
        """The open record when ``gap_ms`` passed since its last event."""
        if self._open is not None and now_ms - self._open.end > self.gap_ms:
            return self.flush()
        return []

    def flush(self) -> list[WheelRecord]:
        current, self._open = self._open, None
        return [current] if current is not None else []


class MoveThrottle:
    """Which points of a drag path are stored (see the module constants)."""

    def __init__(
        self,
        interval_ms: int = MOVE_MIN_INTERVAL_MS,
        distance_px: int = MOVE_MIN_DISTANCE_PX,
        points_per_step: int = MOVE_POINTS_PER_STEP,
    ) -> None:
        self.interval_ms = interval_ms
        self.distance_px = distance_px
        self.points_per_step = points_per_step
        self.reset()

    def reset(self) -> None:
        """A new drag starts (a button went down)."""
        self._last: tuple[int, int, int] | None = None
        self.points = 0

    def accept(self, t: int, gx: int, gy: int) -> bool:
        if self._last is not None:
            lt, lx, ly = self._last
            interval = self.interval_ms * (2 ** (self.points // self.points_per_step))
            if t - lt < interval:
                return False
            if abs(gx - lx) < self.distance_px and abs(gy - ly) < self.distance_px:
                return False
        self._last = (t, gx, gy)
        self.points += 1
        return True


__all__ = [
    "FLUSH_INTERVAL_S",
    "MAX_ANCESTORS",
    "MAX_ELEMENT_NAME_CHARS",
    "MAX_ELEMENT_TEXT_CHARS",
    "MAX_WINDOW_TITLE_CHARS",
    "MODIFIERS",
    "PASSWORD_NAME",
    "ROLES",
    "WHEEL_COALESCE_MS",
    "Ancestor",
    "ButtonRecord",
    "ContentKind",
    "ContentRecord",
    "ContentRequest",
    "ElementContent",
    "ElementInfo",
    "ElementRecord",
    "GestureRecord",
    "HoverRecord",
    "MouseButton",
    "MouseCapabilities",
    "MouseLog",
    "MouseLogWriter",
    "MouseSystemParams",
    "MoveRecord",
    "MoveThrottle",
    "RawMouseEvent",
    "StatusRecord",
    "Target",
    "WheelCoalescer",
    "WheelRecord",
    "WindowInfo",
    "WindowRecord",
    "content_hash",
    "make_content",
    "normalize_modifiers",
    "normalize_role",
    "read_mouse_log",
    "record_from_dict",
]
