"""Supervisor-facing portable locks, support directory, and Windows session probes.

OpenClaw's adapter and transport stay owned elsewhere. They can call
`support_directory()` for selected-openclaw-sessions.json and
`is_user_local_path()` before accepting a filesystem location. This module
does not choose a WSL distribution or read credentials.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

try:
    from collector import runtime_paths
except ImportError:  # supervisor directory is on sys.path
    import runtime_paths

support_directory = runtime_paths.support_directory
is_user_local_path = runtime_paths.is_user_local_path
HeldLock = runtime_paths.HeldLock
lock_fd = runtime_paths.lock_fd
unlock_fd = runtime_paths.unlock_fd
windows_file_holders = runtime_paths.windows_file_holders
read_tasklist = runtime_paths.read_tasklist

HELPER_ENV = "AGENT_RADAR_NATIVE_HELPER"
# A request body must never be able to choose the helper, even if a caller
# forwards the JSON unchanged.
REQUEST_HELPER_KEYS = frozenset({
    "helper", "helper_path", "native_helper", "native_helper_exe",
    "native_helper_path", HELPER_ENV,
})


@contextmanager
def exclusive_lock(path, blocking=True):
    held = HeldLock(path, blocking=blocking, exclusive=True)
    try:
        yield held
    finally:
        held.close()


def strip_helper_keys(request):
    """Drop helper-selection fields. Returns a shallow copy."""
    if not isinstance(request, dict):
        return {}
    return {key: value for key, value in request.items() if key not in REQUEST_HELPER_KEYS}


def validate_helper_path(raw):
    """Absolute existing file from the app, never a UNC path or a symlink."""
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        return None
    if any(ch in raw for ch in ("\x00", "\n", "\r")):
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return None
    try:
        if path.is_symlink() or not path.is_file():
            return None
    except OSError:
        return None
    return path


def native_helper_path(environ=None):
    """Path of this app's helper. Only AGENT_RADAR_NATIVE_HELPER is consulted."""
    env = os.environ if environ is None else environ
    return validate_helper_path(env.get(HELPER_ENV, ""))


def workstation_locked():
    """True when the interactive desktop cannot be opened. None if unproven.

    Fail closed at the caller. This is not called on macOS.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.restype=ctypes.c_void_p
        user32.OpenInputDesktop.argtypes=[ctypes.c_uint,ctypes.c_bool,ctypes.c_uint]
        user32.CloseDesktop.argtypes=[ctypes.c_void_p]
        desktop = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
        if not desktop:
            return True
        user32.CloseDesktop(desktop)
        return False
    except (OSError, AttributeError):
        return None


def idle_seconds():
    """Seconds since the last hardware input, or None when the tick cannot be read."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LastInput(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LastInput(ctypes.sizeof(LastInput), 0)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = kernel32.GetTickCount() & 0xFFFFFFFF
        idle_ms = (tick - int(info.dwTime)) & 0xFFFFFFFF
        return idle_ms / 1000.0
    except (OSError, AttributeError, ValueError):
        return None
