"""Fake mouse, element and touchpad sources: the platform contract without an OS."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from video_story.core.gestures import ContactFrame, TouchpadGeometry
from video_story.core.input_events import (
    ContentKind,
    ContentRequest,
    ElementInfo,
    MouseButton,
    MouseCapabilities,
    MouseSystemParams,
    RawMouseEvent,
    WindowInfo,
    make_content,
)
from video_story.core.timeline import RecordingClock
from video_story.platform.base import ElementInspector, MouseSource, TouchpadSource


class FakeMouseSource(MouseSource):
    name = "fake"
    capabilities = MouseCapabilities(
        buttons=(MouseButton.LEFT, MouseButton.MIDDLE, MouseButton.RIGHT),
        horizontal_wheel=True,
        high_resolution_wheel=True,
        elements=True,
        element_content=True,
    )
    params = MouseSystemParams(double_click_ms=480, double_click_px=(4, 4), drag_px=(4, 4))

    def __init__(self) -> None:
        self.sink: Callable[[RawMouseEvent], None] | None = None
        self.clock: RecordingClock | None = None
        self.running = False

    def start(self, sink: Callable[[RawMouseEvent], None], clock: RecordingClock) -> None:
        self.sink, self.clock, self.running = sink, clock, True

    def stop(self) -> None:
        self.running = False

    def healthy(self) -> bool:
        return self.running

    def emit(self, event: RawMouseEvent) -> None:
        assert self.sink is not None and self.running
        self.sink(event)


@dataclass
class FakeElement:
    rect: tuple[int, int, int, int]  # x, y, width, height
    info: ElementInfo
    value: str | None = None
    text: str | None = None
    selection: str | None = None


@dataclass
class FakeElementInspector(ElementInspector):
    elements: list[FakeElement] = field(default_factory=list)
    windows: dict[int, WindowInfo] = field(default_factory=dict)
    calls: list[tuple[int, int, ContentRequest]] = field(default_factory=list)

    def element_at(self, gx: int, gy: int, want: ContentRequest, timeout_ms: int):
        self.calls.append((gx, gy, want))
        for el in self.elements:
            x, y, w, h = el.rect
            if x <= gx < x + w and y <= gy < y + h:
                if el.info.is_password or want == ContentRequest.NONE:
                    return el.info
                if want == ContentRequest.SELECTION:
                    content = make_content(ContentKind.SELECTION, el.selection)
                elif el.value is not None:
                    content = make_content(ContentKind.VALUE, el.value)
                else:
                    content = make_content(ContentKind.TEXT, el.text)
                return ElementInfo(**{**el.info.__dict__, "content": content})
        return None

    def window_info(self, window: int) -> WindowInfo | None:
        return self.windows.get(window)


class FakeTouchpadSource(TouchpadSource):
    geometry = TouchpadGeometry(3000, 2000)

    def __init__(self) -> None:
        self.sink: Callable[[ContactFrame], None] | None = None

    def start(self, sink: Callable[[ContactFrame], None], clock: RecordingClock) -> None:
        self.sink = sink

    def stop(self) -> None:
        self.sink = None

    def emit(self, frame: ContactFrame) -> None:
        assert self.sink is not None
        self.sink(frame)
