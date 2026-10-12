"""Portable locations, file locks, and read-only Windows process proof.

Mac paths stay on ~/Library/Application Support. Windows application data is
taken only from the current user's APPDATA or LOCALAPPDATA. A candidate whose
bytes do not match the reader this process already understands is a format
mismatch, not a guessed session.
"""
from __future__ import annotations

import csv
import os
import re
import time
from pathlib import Path

IMAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,80}\.exe$")
_LOCK_BYTE = 1


class SupportRoot:
    """A resolved application directory and why it was or was not accepted."""

    __slots__ = ("path", "state")

    def __init__(self, path, state):
        self.path = path
        self.state = state


def support_directory(home=None, environ=None, platform=None):
    """This app's own directory: AgentRadar under roaming APPDATA, else the Mac path."""
    home_path = Path(home) if home is not None else Path.home()
    if (platform or os.name) != "nt":
        return home_path / "Library" / "Application Support" / "AgentRadar"
    roaming = _data_bases(home_path, environ, "nt")[0]
    return roaming / "AgentRadar"


def is_user_local_path(value):
    """Absolute path on this machine. UNC shares, including WSL shares, are not local."""
    if not isinstance(value, str) or not value or any(ch in value for ch in ("\x00", "\n", "\r")):
        return False
    if value.startswith("/") and not value.startswith("//"):
        return True
    if value.startswith("\\\\?\\") and len(value) >= 7 and value[4].isalpha() and value[5] == ":" and value[6] in "\\/":
        return True
    return len(value) >= 3 and value[0].isalpha() and value[1] == ":" and value[2] in "\\/"


def app_support_root(home, mac_parts, windows_names, markers=(), platform=None, environ=None):
    """Mac Application Support folder, or one proven user-owned Windows root.

    `markers` is a sequence of (relative path, kind). kind is sqlite, json, or
    leveldb. On Mac the historical folder is returned even when it is absent;
    callers keep their own existence checks. On Windows a missing, ambiguous,
    or unrecognizable root is not treated as a readable database.
    """
    home_path = Path(home) if home is not None else Path.home()
    system = platform or os.name
    if system != "nt":
        return SupportRoot(home_path.joinpath("Library", "Application Support", *mac_parts), "ok")
    bases = _data_bases(home_path, environ, system)
    good, bad = [], False
    seen = set()
    for base in bases:
        for name in windows_names:
            parts = name if isinstance(name, tuple) else (name,)
            candidate = base.joinpath(*parts)
            try:
                if candidate.is_symlink() or not _owned(candidate, home_path):
                    bad = candidate.is_symlink() or bad
                    continue
                key = str(candidate)
            except OSError:
                bad = True
                continue
            if key in seen:
                continue
            seen.add(key)
            score = _score_root(candidate, markers)
            if score == "good":
                good.append(candidate)
            elif score == "bad":
                bad = True
    if len(good) == 1 and not bad:
        return SupportRoot(good[0], "ok")
    if len(good) > 1 or (good and bad):
        return SupportRoot(None, "ambiguous")
    if bad:
        return SupportRoot(None, "format_mismatch")
    first = windows_names[0]
    fallback = bases[0].joinpath(*(first if isinstance(first, tuple) else (first,)))
    return SupportRoot(fallback, "missing")


def windows_image_matches(command, app_name, aliases=()):
    """True only for a bare tasklist image name equal to the app's own exe name.

    Paths are rejected, so a Mac executable path cannot accidentally match.
    """
    if not isinstance(command, str) or not isinstance(app_name, str) or not app_name:
        return False
    if "/" in command or "\\" in command or not IMAGE_NAME.fullmatch(command):
        return False
    names = {app_name.lower() + ".exe"}
    for alias in aliases:
        if isinstance(alias, str) and alias:
            leaf = alias.lower()
            names.add(leaf if leaf.endswith(".exe") else leaf + ".exe")
    return command.lower() in names


def parse_tasklist(text):
    """Image name and PID only. Non-CSV lines and path-like names are ignored."""
    found = {}
    if not isinstance(text, str):
        return found
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith('"'):
            continue
        try:
            row = next(csv.reader([stripped]))
        except (csv.Error, StopIteration):
            continue
        if len(row) < 2 or not IMAGE_NAME.fullmatch(row[0]) or not row[1].isdigit():
            continue
        pid = int(row[1])
        if pid > 0:
            found[pid] = row[0]
    return found


def read_tasklist(runner=None):
    """Read-only tasklist. None means the probe failed and proves nothing."""
    import subprocess
    if runner is None and os.name != "nt":
        return None
    try:
        if runner is None:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=2, check=False,
                creationflags=flags,
            )
        else:
            completed = runner()
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(completed, "returncode", 1) not in (0, 1):
        return None
    stdout = getattr(completed, "stdout", "") or ""
    if not isinstance(stdout, str):
        return None
    return parse_tasklist(stdout)


def windows_file_holders(path):
    """PIDs proven to have this file open, or None when proof is unavailable.

    An empty set means the probe succeeded and found no holder. It is never
    inferred from a process merely existing.
    """
    if os.name != "nt":
        return None
    return _restart_manager_holders(path)


def codex_lock_owners(processes, root, holders=None):
    """Map a Codex thread to a PID only when that PID is shown holding its lock."""
    codex_pids = {pid for pid, proc in processes.items()
                  if isinstance(proc, dict) and proc.get("app") == "codex" and isinstance(pid, int)}
    if not codex_pids:
        return {}
    lock_dir = Path(root) / "thread-writer-locks"
    try:
        paths = [path for path in lock_dir.iterdir()
                 if path.suffix == ".lock" and path.is_file() and not path.is_symlink()]
    except OSError:
        return {}
    probe = holders or windows_file_holders
    owners = {}
    for path in paths[:64]:
        try:
            found = probe(path)
        except OSError:
            continue
        if not isinstance(found, (set, list, tuple)):
            continue  # None, or any non-list, is not proof.
        proven = [pid for pid in found if pid in codex_pids]
        if len(proven) == 1 and path.stem and "/" not in path.stem and "\\" not in path.stem:
            owners[path.stem] = proven[0]
    return owners


def lock_fd(fd, exclusive=True, blocking=True):
    """Whole-file lock. Non-blocking failure raises BlockingIOError on both platforms.

    Windows CRT locking is exclusive only, so a shared request still takes the
    same one-byte region. Callers that need the Mac shared lock keep it.
    """
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        deadline = time.monotonic() + (30 if blocking else 0)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_BYTE)
                return
            except OSError:
                if not blocking or time.monotonic() >= deadline:
                    raise BlockingIOError("lock")
                time.sleep(0.05)
        return
    import fcntl
    flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        flags |= fcntl.LOCK_NB
    fcntl.flock(fd, flags)


def unlock_fd(fd):
    if fd is None or fd < 0:
        return
    if os.name == "nt":
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_BYTE)
        except OSError:
            return
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


def chmod_fd(fd, mode=0o600):
    if hasattr(os, "fchmod"):
        os.fchmod(fd, mode)


class HeldLock:
    """Lock held until close(). close() is safe to call twice."""

    def __init__(self, path, blocking=False, exclusive=True):
        self._fd = -1
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self._fd = fd
        try:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            lock_fd(fd, exclusive=exclusive, blocking=blocking)
        except BaseException:
            self.close()
            raise

    def fileno(self):
        return self._fd

    def close(self):
        fd, self._fd = self._fd, -1
        if fd < 0:
            return
        try:
            unlock_fd(fd)
        finally:
            os.close(fd)


def _data_bases(home, environ, platform):
    if platform != "nt":
        return [Path(home) / "Library" / "Application Support"]
    env = os.environ if environ is None else environ
    fallbacks = (("APPDATA", home / "AppData" / "Roaming"),
                 ("LOCALAPPDATA", home / "AppData" / "Local"))
    bases = []
    for key, fallback in fallbacks:
        raw = env.get(key) if key in env and env.get(key) else None
        candidate = Path(raw) if raw else fallback
        if _owned(candidate, home) and candidate not in bases:
            bases.append(candidate)
    if not bases:
        bases.append(home / "AppData" / "Roaming")
    return bases


def _owned(path, home):
    try:
        home_real = Path(home).resolve()
        raw = Path(path)
        if not raw.is_absolute():
            return False
        raw.resolve().relative_to(home_real)
    except (OSError, ValueError):
        return False
    return True


def _score_root(root, markers):
    if not markers:
        return "good" if root.exists() else "absent"
    saw_good = False
    for relative, kind in markers:
        target = root if relative in ("", ".") else root.joinpath(relative)
        try:
            exists = target.exists()
        except OSError:
            return "bad"
        if not exists:
            return "absent"
        result = _probe(target, kind)
        if result is True:
            saw_good = True
            continue
        return "bad"
    return "good" if saw_good else "absent"


def _probe(path, kind):
    try:
        if path.is_symlink():
            return None
        if kind == "leveldb":
            if not path.is_dir():
                return False
            names = []
            for index, child in enumerate(path.iterdir()):
                if index >= 80:
                    break
                names.append(child.name)
            return any(name == "CURRENT" or name.endswith((".ldb", ".sst", ".log")) for name in names)
        if not path.is_file():
            return False
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError:
        return None
    if kind == "sqlite":
        return head.startswith(b"SQLite format 3\x00")
    if kind == "json":
        stripped = head.lstrip()
        return bool(stripped[:1] in (b"{", b"["))
    return False


def _restart_manager_holders(path):
    """Read-only Restart Manager list. Any failure returns None, never a guess."""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    try:
        if Path(path).is_symlink() or not Path(path).is_file():
            return None
    except OSError:
        return None
    rstrtmgr = ctypes.WinDLL("rstrtmgr")
    handle = wintypes.DWORD()
    session_key = ctypes.create_unicode_buffer(33)
    ERROR_MORE_DATA = 234
    started = False
    try:
        if rstrtmgr.RmStartSession(ctypes.byref(handle), 0, session_key) != 0:
            return None
        started = True
        resources = ctypes.c_wchar_p(str(path))
        if rstrtmgr.RmRegisterResources(handle, 1, ctypes.byref(resources), 0, None, 0, None) != 0:
            return None
        needed = wintypes.UINT(0)
        count = wintypes.UINT(0)
        reasons = wintypes.DWORD()
        status = rstrtmgr.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        if status == 0 and needed.value == 0:
            return set()
        if status not in (0, ERROR_MORE_DATA) or needed.value <= 0 or needed.value > 8:
            return None
        info_type = _process_info_type()
        if info_type is None:
            return None
        array = (info_type * needed.value)()
        count = wintypes.UINT(needed.value)
        status = rstrtmgr.RmGetList(handle, ctypes.byref(needed), ctypes.byref(count), array, ctypes.byref(reasons))
        if status != 0:
            return None
        return {int(array[index].Process.dwProcessId) for index in range(count.value)
                if array[index].Process.dwProcessId > 0}
    except (OSError, AttributeError, ValueError):
        return None
    finally:
        if started:
            try:
                rstrtmgr.RmEndSession(handle)
            except (OSError, AttributeError):
                pass


def _process_info_type():
    import ctypes
    from ctypes import wintypes

    class UniqueProcess(ctypes.Structure):
        _fields_ = [("dwProcessId", wintypes.DWORD),
                    ("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [("Process", UniqueProcess),
                    ("strAppName", ctypes.c_wchar * 256),
                    ("strServiceShortName", ctypes.c_wchar * 64),
                    ("ApplicationType", wintypes.DWORD),
                    ("AppStatus", wintypes.ULONG),
                    ("TSSessionId", wintypes.DWORD),
                    ("bRestartable", wintypes.BOOL)]

    return ProcessInfo


class fcntl_compat:
    LOCK_SH=1;LOCK_EX=2;LOCK_NB=4;LOCK_UN=8
    @staticmethod
    def flock(fd, operation):
        if operation & fcntl_compat.LOCK_UN: return unlock_fd(fd)
        return lock_fd(fd, exclusive=bool(operation & fcntl_compat.LOCK_EX), blocking=not bool(operation & fcntl_compat.LOCK_NB))
