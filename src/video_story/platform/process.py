"""Process helpers that differ per OS (liveness check, detached spawning)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` exists (best effort, no permissions needed)."""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def gui_python() -> str:
    """Interpreter for the GUI process: ``pythonw.exe`` on Windows when available."""
    exe = Path(sys.executable)
    if sys.platform.startswith("win") and exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def spawn_detached(args: list[str], log_path: Path, cwd: Path | None = None) -> int:
    """Start a process that outlives the caller; stdout/stderr go to ``log_path``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")  # noqa: SIM115 - handed to the child
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": log,
        "cwd": cwd,
    }
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **kwargs)  # type: ignore[arg-type]
    finally:
        log.close()
    return proc.pid


__all__ = ["gui_python", "pid_alive", "spawn_detached"]
