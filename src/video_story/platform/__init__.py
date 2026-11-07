"""Platform factory. Import platform modules lazily so each OS only loads its own code."""

from __future__ import annotations

import sys

from .base import Platform, PlatformError, SupportStatus

_instance: Platform | None = None


def create_platform(name: str | None = None) -> Platform:
    name = name or sys.platform
    if name.startswith("win"):
        from .windows import WindowsPlatform

        return WindowsPlatform()
    if name.startswith("linux"):
        from .linux import LinuxPlatform

        return LinuxPlatform()
    if name == "darwin":
        from .macos import MacOSPlatform

        return MacOSPlatform()
    raise PlatformError(f"unsupported platform {name!r}")


def get_platform() -> Platform:
    """Process-wide platform singleton."""
    global _instance
    if _instance is None:
        _instance = create_platform()
    return _instance


__all__ = ["Platform", "PlatformError", "SupportStatus", "create_platform", "get_platform"]
