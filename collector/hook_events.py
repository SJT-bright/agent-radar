"""App-owned Claude Code lifecycle events.

The file stores only session_id, turn_id, event, timestamp, and cwd. Prompt text,
tool arguments, transcripts, and assistant messages are never written. Nothing
here installs or edits Claude's own hook settings.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:
    from collector.runtime_paths import fcntl_compat as fcntl
import json
import math
import os
from datetime import datetime
from pathlib import Path
import re
import time
from typing import Any

MAX_STDIN = 262_144
MAX_LINE = 2048
MAX_EVENTS = 512
MAX_FILE_BYTES = 256 * 1024
# Clock skew only. This is not an idle timeout and never turns busy into interrupted.
FUTURE_SKEW_SECONDS = 2.0
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
TURN_KEYS = ("turn_id", "turnId", "prompt_id", "promptId")
STORED_KEYS = ("session_id", "turn_id", "event", "timestamp", "cwd")
EVENTS = {"turn_started", "turn_completed", "turn_failed", "user_stopped", "permission_wait"}
TERMINAL_EVENTS = {"turn_completed", "turn_failed", "user_stopped"}
HOOK_EVENTS = {
    "UserPromptSubmit": "turn_started",
    "Stop": "turn_completed",
    "StopFailure": "turn_failed",
    "PermissionRequest": "permission_wait",
}


def default_path() -> Path:
    from collector.runtime_paths import support_directory
    return support_directory() / "claude-lifecycle.jsonl"


def turn_id_of(data: dict) -> str | None:
    """Structural turn id only. Nested message bodies are not inspected."""
    if not isinstance(data, dict):
        return None
    for key in TURN_KEYS:
        value = data.get(key)
        if isinstance(value, str) and ID_RE.fullmatch(value):
            return value
    return None


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0.0
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return 0.0
        return float(value / 1000 if value > 10_000_000_000 else value)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _cwd_ok(value: str) -> bool:
    return bool(value) and len(value) <= 1024 and not any(ch in value for ch in ("\n", "\r", "\x00"))


def _stored(item: Any) -> dict | None:
    if not isinstance(item, dict) or set(item) != set(STORED_KEYS):
        return None
    if item.get("event") not in EVENTS:
        return None
    session, turn, cwd = item.get("session_id"), item.get("turn_id"), item.get("cwd")
    stamp = item.get("timestamp")
    if not isinstance(session, str) or not ID_RE.fullmatch(session):
        return None
    if not isinstance(turn, str) or not ID_RE.fullmatch(turn):
        return None
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp <= 0:
        return None
    if not isinstance(cwd, str) or not _cwd_ok(cwd):
        return None
    return {"session_id": session, "turn_id": turn, "event": item["event"],
            "timestamp": float(stamp), "cwd": cwd}


def _key(event: dict) -> tuple:
    return (event["session_id"], event["turn_id"], event["event"], int(round(event["timestamp"] * 1000)))


def normalize(payload: Any, now: float) -> tuple[dict | None, str]:
    """Keep the allowlisted metadata. Any other hook field is discarded."""
    if not isinstance(payload, dict):
        return None, "invalid"
    hook_name = payload.get("hook_event_name")
    explicit = payload.get("event")
    if hook_name is not None and not isinstance(hook_name, str):
        return None, "unsupported"
    if isinstance(hook_name, str) and hook_name in HOOK_EVENTS:
        kind = HOOK_EVENTS[hook_name]
        if explicit not in (None, kind):
            return None, "unsupported"
        # Stop does not fire for a user interrupt. An active stop hook is
        # Claude continuing the same turn, not a completed turn.
        if hook_name == "Stop" and payload.get("stop_hook_active") is True:
            return None, "continued"
    elif explicit == "user_stopped" and hook_name is None:
        kind = "user_stopped"
    else:
        return None, "unsupported"
    session = payload.get("session_id")
    if not isinstance(session, str) or not ID_RE.fullmatch(session):
        return None, "invalid"
    turn = turn_id_of(payload)
    if not turn:
        return None, "turn_unknown"
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not _cwd_ok(cwd):
        return None, "invalid"
    if payload.get("timestamp") is None:
        stamp = float(now)
    else:
        stamp = _timestamp(payload.get("timestamp"))
        if stamp <= 0:
            return None, "invalid"
    if stamp > float(now) + FUTURE_SKEW_SECONDS:
        return None, "future"
    return {"session_id": session, "turn_id": turn, "event": kind,
            "timestamp": stamp, "cwd": cwd}, "ok"


def _accept(events: list[dict]) -> list[dict]:
    """Arrival order. A later turn makes older-turn events late; they are dropped."""
    seen = set()
    started: dict[str, tuple[str, float]] = {}
    finished: set[tuple[str, str]] = set()
    kept = []
    for event in events:
        sid, turn, kind, ts = event["session_id"], event["turn_id"], event["event"], event["timestamp"]
        key = _key(event)
        if key in seen:
            continue
        current = started.get(sid)
        if kind == "turn_started":
            if current and (ts < current[1] or current[0] == turn):
                continue
            started[sid] = (turn, ts)
            seen.add(key)
            kept.append(event)
            continue
        if not current or current[0] != turn or ts < current[1] or (sid, turn) in finished:
            continue
        if kind in TERMINAL_EVENTS:
            finished.add((sid, turn))
        seen.add(key)
        kept.append(event)
    return kept


def _classify(accepted: list[dict], event: dict) -> str:
    if any(_key(item) == _key(event) for item in accepted):
        return "duplicate"
    current = None
    finished = False
    for item in accepted:
        if item["session_id"] != event["session_id"]:
            continue
        if item["event"] == "turn_started":
            current = item
            finished = False
        elif current and item["turn_id"] == current["turn_id"] and item["event"] in TERMINAL_EVENTS:
            finished = True
    if event["event"] == "turn_started":
        if current and current["turn_id"] == event["turn_id"]:
            return "duplicate"
        return "late_turn"
    if (current is None or current["turn_id"] != event["turn_id"] or event["timestamp"] < current["timestamp"]
            or finished):
        return "late_turn"
    return "duplicate"


def _decode(raw: bytes, truncated: bool) -> list[dict]:
    if truncated:
        cut = raw.find(b"\n")
        raw = raw[cut + 1:] if cut >= 0 else b""
    found = []
    for line in raw.splitlines():
        if len(line) > MAX_LINE:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        stored = _stored(item)
        if stored:
            found.append(stored)
    return found


def _read_fd(fd: int) -> list[dict]:
    size = os.fstat(fd).st_size
    start = max(0, size - MAX_FILE_BYTES)
    os.lseek(fd, start, os.SEEK_SET)
    remaining = min(size - start, MAX_FILE_BYTES)
    chunks = []
    while remaining:
        part = os.read(fd, remaining)
        if not part:
            break
        chunks.append(part)
        remaining -= len(part)
    return _decode(b"".join(chunks), start > 0)


def _read_events(path: Path) -> list[dict]:
    try:
        if path.is_symlink() or not path.is_file():
            return []
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return []
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        return _read_fd(fd)
    except OSError:
        return []
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _encode(events: list[dict]) -> bytes:
    return "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for event in events).encode("utf-8")


def _fit(events: list[dict], max_events: int) -> list[dict]:
    if max_events < 1:
        return []
    if len(events) > max_events:
        events = _accept(events[-max_events:])
    payload = _encode(events)
    while events and len(payload) > MAX_FILE_BYTES:
        events = _accept(events[1:])
        payload = _encode(events)
    return events


def _write_fd(fd: int, events: list[dict]) -> None:
    payload = _encode(events)
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = payload
    while view:
        wrote = os.write(fd, view)
        if wrote <= 0:
            raise OSError("short write")
        view = view[wrote:]
    os.fsync(fd)
    if hasattr(os,"fchmod"): os.fchmod(fd, 0o600)


def _kept(before: list[dict], after: list[dict], event: dict) -> bool:
    if event["event"] == "turn_started":
        opened = any(item["session_id"] == event["session_id"] and item["turn_id"] == event["turn_id"]
                     and item["event"] == "turn_started" for item in before)
        return not opened and any(item == event for item in after)
    return _key(event) not in {_key(item) for item in before} and any(item == event for item in after)


def record(payload: Any, path: Path, now: float | None = None, max_events: int = MAX_EVENTS) -> dict:
    """Append one accepted event. Rejected input is not written."""
    now = time.time() if now is None else float(now)
    event, reason = normalize(payload, now)
    if event is None:
        return {"ok": True, "stored": False, "reason": reason}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"ok": True, "stored": False, "reason": "io"}
    if path.is_symlink():
        return {"ok": True, "stored": False, "reason": "symlink"}
    existed = path.exists()
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        return {"ok": True, "stored": False, "reason": "symlink" if path.is_symlink() else "io"}
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = [item for item in _read_fd(fd) if item["timestamp"] <= now + FUTURE_SKEW_SECONDS]
        before = _accept(existing)
        after = _fit(_accept(before + [event]), max_events)
        if not _kept(before, after, event):
            reason = _classify(before, event)
            if not existed:
                os.close(fd)
                fd = -1
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return {"ok": True, "stored": False, "reason": reason}
        _write_fd(fd, after)
        return {"ok": True, "stored": True, "reason": "stored"}
    except OSError:
        return {"ok": True, "stored": False, "reason": "io"}
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


def load(path: Path, now: float | None = None) -> dict[str, list[dict]]:
    """Bounded read of this app's event file. Missing or hostile lines are skipped."""
    now = time.time() if now is None else float(now)
    grouped: dict[str, list[dict]] = {}
    for event in _accept([item for item in _read_events(path) if item["timestamp"] <= now + FUTURE_SKEW_SECONDS]):
        grouped.setdefault(event["session_id"], []).append(event)
    return grouped


def session_state(grouped: dict[str, list[dict]], session_id: str) -> dict | None:
    """Latest accepted state of the current turn. Older turns are not revived."""
    current = None
    started_at = None
    last = None
    for event in grouped.get(session_id) or []:
        if event["event"] == "turn_started":
            current = event["turn_id"]
            started_at = event["timestamp"]
            last = event
        elif current and event["turn_id"] == current:
            last = event
    if not last or not current or started_at is None:
        return None
    status = {"turn_started": "running", "permission_wait": "waiting", "turn_completed": "completed",
              "turn_failed": "interrupted", "user_stopped": "interrupted"}[last["event"]]
    return {
        "turn_id": current, "event": last["event"], "timestamp": last["timestamp"], "cwd": last["cwd"],
        "started_at": started_at,
        "ended_at": last["timestamp"] if last["event"] in TERMINAL_EVENTS else None,
        "status": status, "user_stopped": last["event"] == "user_stopped",
    }
