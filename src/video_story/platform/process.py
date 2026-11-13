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
            # Access denied is not evidence that a GUI in another permission context died.
            return kernel32.GetLastError() == 5
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


def console_python() -> str:
    """Interpreter for a worker whose stdout is read through a pipe: ``python.exe``
    rather than ``pythonw.exe`` on Windows (the parent, a GUI process, creates it
    without a console window)."""
    exe = Path(sys.executable)
    if sys.platform.startswith("win") and exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
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


def lower_own_priority(mode: str) -> str:
    """Lower the scheduling priority of this process for a background job.

    ``light``: the lowest class the OS offers for work nobody waits for - Windows
    background mode (idle CPU priority plus low I/O and memory priority), Linux
    ``SCHED_IDLE``, macOS ``PRIO_DARWIN_BG``. ``faster``: only slightly below normal.
    Returns what was set (``below_normal`` when ``light`` could not be had); failures
    are swallowed, the job then runs at the priority it was given.
    """
    try:
        if sys.platform.startswith("win"):
            return _lower_windows(mode)
        if sys.platform == "darwin":
            return _lower_macos(mode)
        return _lower_linux(mode)
    except Exception:  # noqa: BLE001 - a priority is a wish, never a reason to fail
        return "unchanged"


def _lower_windows(mode: str) -> str:
    import ctypes

    PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000
    BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    process = kernel32.GetCurrentProcess()
    if mode == "light" and kernel32.SetPriorityClass(process, PROCESS_MODE_BACKGROUND_BEGIN):
        return "background"
    if kernel32.SetPriorityClass(process, BELOW_NORMAL_PRIORITY_CLASS):
        return "below_normal"
    return "unchanged"


def _lower_linux(mode: str) -> str:
    if mode == "light" and hasattr(os, "SCHED_IDLE"):
        try:
            os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))  # type: ignore[attr-defined]
        except OSError:
            pass
        else:
            _linux_idle_io()
            return "background"
    os.nice(10)
    return "below_normal"


def _linux_idle_io() -> None:
    """I/O scheduling class idle (``ioprio_set``), best effort."""
    import ctypes
    import platform

    numbers = {"x86_64": 251, "aarch64": 30, "arm64": 30}
    number = numbers.get(platform.machine().lower())
    if number is None:
        return
    IOPRIO_WHO_PROCESS = 1
    IOPRIO_CLASS_IDLE = 3
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall(number, IOPRIO_WHO_PROCESS, 0, IOPRIO_CLASS_IDLE << 13)


def _lower_macos(mode: str) -> str:
    if mode == "light":
        import ctypes
        import ctypes.util

        PRIO_DARWIN_PROCESS = 4
        PRIO_DARWIN_BG = 0x1000
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        if libc.setpriority(PRIO_DARWIN_PROCESS, 0, PRIO_DARWIN_BG) == 0:
            return "background"
    os.nice(10)
    return "below_normal"


__all__ = ["console_python", "gui_python", "lower_own_priority", "pid_alive", "spawn_detached"]
