"""Read-only local session adapters for ZCode and the two WorkBuddy apps.

Only whitelisted database columns and bounded runtime metadata are inspected.
Only event timestamps and classified errors are retained from bounded log tails.
Settings, credentials, cookies, and network endpoints are never used.
The active status is corroborated by a live app process and recent lifecycle
evidence; a filesystem modification time is never a running signal.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Callable, Dict, Optional
from urllib.parse import quote
try:
    from .failure_reasons import failure_reason
    from .transcript_scan import scan_blocks, complete_lines, cursor_after
except ImportError:
    from failure_reasons import failure_reason
    from transcript_scan import scan_blocks, complete_lines, cursor_after


SESSION_LIMIT = 40
RUNTIME_LIMIT = 50
MAX_METADATA_BYTES = 16 * 1024
MAX_TAIL_BYTES = 2 * 1024 * 1024
ACTIVE_MAX_AGE = 600
HEARTBEAT_MAX_AGE = 90
ZCODE_ROLLOUT_TAIL_BYTES = 256 * 1024
ZCODE_INFLIGHT_MAX_AGE = 900
ZCODE_LOG_TAIL_BYTES = 32 * 1024 * 1024
RUNNING_STATES = {"working", "running", "in_progress", "processing", "generating"}
WAITING_STATES = {"waiting", "waiting_for_user", "waiting_for_input", "awaiting_approval", "needs_attention"}
COMPLETED_STATES = {"completed", "complete", "done", "success", "succeeded"}
IDLE_STATES = {"idle", "pending"}
INTERRUPTED_STATES = {"stopped", "cancelled", "canceled", "interrupted"}
ERROR_STATES = {"error", "failed", "failure"}


def _epoch(value) -> float:
    try:
        result = float(value or 0)
        if not math.isfinite(result) or result < 0:
            return 0.0
        return result / 1000 if result > 100_000_000_000 else result
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _iso_epoch(value) -> float:
    """UTC ISO-8601 timestamps from ZCode rollout records; 0 when absent."""
    if not isinstance(value, str) or "T" not in value:
        return 0.0
    try:
        number = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        return number if math.isfinite(number) and number > 0 else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


_ZCODE_ROLLOUT_ID = re.compile(r"\Asess_[A-Za-z0-9_-]{1,128}\Z")


def _recent(timestamp: float, now: float, maximum: int) -> bool:
    return timestamp > 0 and -30 <= now - timestamp <= maximum


def _processes() -> Dict[int, str]:
    """Read executable names only, never command line arguments."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,comm="], capture_output=True,
            text=True, timeout=2, check=False,
        )
        if result.returncode:
            return {}
        processes = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                processes[int(parts[0])] = parts[1]
        return processes
    except (OSError, subprocess.SubprocessError):
        return {}


def _belongs_to_app(command: str, app_name: str) -> bool:
    return f"/{app_name}.app/Contents/" in command


class DesktopCollector:
    """Injectable reader; collect() below is the integration entry point."""

    def __init__(self, home: Optional[Path] = None,
                 clock: Callable[[], float] = time.time,
                 process_supplier: Callable[[], Dict[int, str]] = _processes):
        self.home = Path(home) if home is not None else Path.home()
        self.clock = clock
        self.process_supplier = process_supplier
        self._process_cache = {}
        self._process_cache_at = -float("inf")
        self._file_cache = {}
        self._zcode_log_cache = {}
        self.last_errors: list[str] = []
        self._failed_apps = set()
        self._last_results = {}

    def _live_processes(self, now: float) -> Dict[int, str]:
        if now - self._process_cache_at >= 2 or now < self._process_cache_at:
            try:
                self._process_cache = self.process_supplier()
            except (OSError, ValueError, subprocess.SubprocessError):
                self._process_cache = {}
            self._process_cache_at = now
        return self._process_cache

    def _record_read_failure(self, app_name: str, error: Exception) -> None:
        # Exception messages can contain paths or SQL; emit only the type.
        message = app_name + "：会话索引读取失败（" + type(error).__name__ + "）"
        self.last_errors.append(message)
        self._failed_apps.add(app_name)

    def _rows(self, path: Path, query: str, app_name: str, parameters=()):
        last_error: Exception | None = None
        for attempt in range(2):
            connection = None
            try:
                # Missing applications are normal, and stat() first guarantees a
                # plain connect can never create a database file. query_only=ON
                # keeps every session below strictly read-only.
                path.stat()
                connection = sqlite3.connect(str(path), timeout=0.15)
                connection.execute("PRAGMA query_only = ON")
                connection.row_factory = sqlite3.Row
                return [dict(row) for row in connection.execute(query, (*parameters, SESSION_LIMIT))]
            except FileNotFoundError:
                return []
            except (OSError, sqlite3.Error, ValueError) as error:
                last_error = error
                # A busy writer briefly locks the index; one bounded retry keeps
                # the panel from flapping to unknown for a whole cycle.
                transient = (isinstance(error, sqlite3.OperationalError) and
                             ("locked" in str(error).lower() or "busy" in str(error).lower()))
                if attempt == 0 and transient:
                    time.sleep(0.25)
                    continue
                self._record_read_failure(app_name, error)
                return []
            finally:
                if connection is not None:
                    connection.close()
        raise last_error if last_error else RuntimeError("unreachable")

    @staticmethod
    def _absorb(state: dict, stem: str, lines) -> None:
        for line in lines:
            try:
                data = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(data, dict) or data.get("sessionId") != stem:
                continue
            stamp, event_kind, role = _epoch(data.get("timestamp")), data.get("type"), data.get("role")
            if event_kind == "message" and role == "user":
                state["user_at"] = max(state["user_at"], stamp)
            if event_kind in {"reasoning", "function_call", "function_call_result"} or (event_kind == "message" and role == "assistant"):
                state["activity"] = max(state["activity"], stamp)
            provider = data.get("providerData")
            error = provider.get("error") if isinstance(provider, dict) else None
            if event_kind == "message" and role == "assistant" and (error or data.get("status") == "incomplete") and stamp >= state["error_at"]:
                state["error_at"] = stamp
                state["error_reason"] = failure_reason(error)

    def _cached_file(self, path: Path, kind: str):
        """Cache bounded parsed metadata, not conversation bodies."""
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            key = (str(path), kind)
            previous = self._file_cache.get(key)
            if previous and previous[0] == signature:
                return previous[1]
            if kind == "runtime":
                if stat.st_size > MAX_METADATA_BYTES:
                    return None
                with path.open("rb") as stream:
                    data = json.loads(stream.read(MAX_METADATA_BYTES + 1))
                if not isinstance(data, dict):
                    return None
                # Never retain url/endpoint, or unexpected metadata fields.
                parsed = {name: data.get(name) for name in
                          ("pid", "sessionId", "lastHeartbeat")}
            else:
                # A long turn pushes its user message megabytes behind the
                # tail. Only an append since the previous scan may continue
                # from the stored cursor; any rewrite, rotation or first
                # sight re-scans the tail and walks backwards until the
                # user turn is found, so the turn start survives big logs.
                state = {"activity": 0.0, "user_at": 0.0, "error_at": 0.0,
                         "timing_reason": "会话日志没有本轮开始事件"}
                prior = previous[1] if previous and isinstance(previous[1], dict) else None
                scan = prior.get("_scan") if prior else None
                resumable = (isinstance(scan, dict) and scan.get("ino") == stat.st_ino
                             and isinstance(scan.get("end"), int) and isinstance(scan.get("size"), int)
                             and 0 < scan["end"] < stat.st_size and stat.st_size > scan["size"])
                if resumable:
                    for name in ("activity", "user_at", "error_at"):
                        state[name] = prior.get(name, 0.0)
                    if prior.get("error_reason"):
                        state["error_reason"] = prior["error_reason"]
                    state["timing_reason"] = prior.get("timing_reason", state["timing_reason"])
                with path.open("rb") as stream:
                    if resumable:
                        start = scan["end"]
                        stream.seek(start)
                        data = stream.read(stat.st_size - start)
                        self._absorb(state, path.stem, complete_lines(data, drop_first=False))
                        end = cursor_after(data, start)
                    else:
                        end = 0
                        for start, stop in scan_blocks(stat.st_size):
                            stream.seek(start)
                            data = stream.read(stop - start)
                            self._absorb(state, path.stem, complete_lines(data, drop_first=start > 0))
                            if end == 0:
                                end = cursor_after(data, start)
                            if state["user_at"]:
                                break
                if not state["user_at"]:
                    state["timing_reason"] = ("日志已回溯扫描仍未包含本轮开始事件" if stat.st_size > MAX_TAIL_BYTES
                                              else "会话日志没有本轮开始事件")
                state["_scan"] = {"ino": stat.st_ino, "end": end, "size": stat.st_size}
                parsed = state
            if len(self._file_cache) >= 256:
                self._file_cache.clear()
            self._file_cache[key] = (signature, parsed)
            return parsed
        except (OSError, ValueError, UnicodeError) as error:
            if kind == "transcript":
                reason = ("未找到对应的会话日志" if isinstance(error, FileNotFoundError) else
                          "系统拒绝读取会话日志" if isinstance(error, PermissionError) else "会话日志暂不可读")
                return {"timing_reason": reason + "，无法取得本轮开始时间"}
            return None

    def _runtime_sessions(self, root: Path, app_name: str, processes: dict):
        directory = root / "sessions"
        try:
            # Metadata-only directory listing; no recursive historical scan.
            # A heartbeat file deleted mid-scan only removes itself; the whole
            # liveness table must not vanish for a cycle (running rows would
            # lose their corroboration and flap to unknown).
            stamped = []
            for path in directory.glob("*.json"):
                try:
                    stamped.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            stamped.sort(key=lambda pair: pair[0], reverse=True)
            paths = [path for _, path in stamped[:RUNTIME_LIMIT]]
        except OSError:
            return {}
        result = {}
        for path in paths:
            value = self._cached_file(path, "runtime")
            if not value:
                continue
            pid = value.get("pid")
            session_id = value.get("sessionId")
            if (not isinstance(pid, int) or isinstance(pid, bool) or
                    not isinstance(session_id, str) or
                    not _belongs_to_app(processes.get(pid, ""), app_name)):
                continue
            heartbeat = _epoch(value.get("lastHeartbeat"))
            old = result.get(session_id)
            if old is None or heartbeat > old[1]:
                result[session_id] = (pid, heartbeat)
        return result

    def _transcript_metadata(self, root: Path, project: str, session_id: str) -> dict:
        # Both installed WorkBuddy editions use this directory spelling.
        # Require a single safe filename rather than accepting path traversal.
        if not project.startswith("/") or not session_id or any(c in session_id for c in "/\\"):
            return {"timing_reason": "缺少有效的项目路径或会话编号，无法定位开始事件"}
        directory = project.strip("/").replace("/", "-")
        path = root / "projects" / directory / (session_id + ".jsonl")
        return self._cached_file(path, "transcript") or {}

    @staticmethod
    def _status(raw, is_live: bool, fresh: bool):
        status = str(raw or "").strip().lower()
        if status in COMPLETED_STATES:
            return "completed", "会话索引明确记录本轮已完成"
        if status in ERROR_STATES | INTERRUPTED_STATES:
            return "interrupted", "会话索引明确记录错误终止或中断（" + status + "）"
        if status in IDLE_STATES:
            return "idle", "会话索引记录空闲、待开始或已停止"
        if status in WAITING_STATES:
            if is_live and fresh:
                return "waiting", "会话索引记录等待输入/确认，应用进程存活"
            return "unknown", "保留等待记录，但缺少近期存活证据"
        if status in RUNNING_STATES:
            if is_live and fresh:
                return "running", "索引运行状态与存活进程、近期会话事件相符"
            return "unknown", "索引保留运行状态，但缺少近期存活证据"
        return "unknown", "会话索引没有可确认的生命周期状态"

    def _workbuddy(self, app_id: str, app_name: str, root_name: str,
                   now: float, processes: dict):
        root = self.home / root_name
        rows = self._rows(root / "workbuddy.db", """
            SELECT id, cwd, title, custom_title, status, updated_at, last_activity_at
            FROM sessions WHERE deleted_at IS NULL
            ORDER BY updated_at DESC LIMIT ?
        """, app_name)
        runtime = self._runtime_sessions(root, app_name, processes) if rows else {}
        app_is_live = any(_belongs_to_app(command, app_name) for command in processes.values())
        result = []
        for row in rows:
            session_id = str(row["id"] or "")
            if not session_id:
                continue
            project = str(row["cwd"] or "")
            updated_at = max(_epoch(row["updated_at"]), _epoch(row["last_activity_at"]))
            pid, heartbeat = runtime.get(session_id, (None, 0.0))
            meta = self._transcript_metadata(root, project, session_id)
            event_at, start = meta.get("activity", 0), meta.get("user_at", 0)
            fresh = (_recent(heartbeat, now, HEARTBEAT_MAX_AGE) or
                     _recent(event_at, now, ACTIVE_MAX_AGE) or
                     _recent(updated_at, now, ACTIVE_MAX_AGE))
            status, evidence = self._status(row["status"], app_is_live, fresh)
            reason = evidence
            # A newer user turn cannot inherit an old terminal index or error.
            if status in {"interrupted", "completed", "idle"} and start > updated_at:
                status = "unknown"
                evidence = reason = "已有更新的用户消息，但会话索引尚未确认这一轮状态；不沿用上一轮结果"
            elif status == "interrupted":
                reason = (meta["error_reason"] if meta.get("error_at", 0) >= start and meta.get("error_reason") else
                          failure_reason(stop=str(row["status"] or "")))
            elif status == "unknown":
                if not app_is_live:
                    reason = "会话索引仍保留旧状态，但对应应用进程未运行"
                elif not fresh:
                    reason = "应用进程存在，但会话心跳和运行事件已过期，无法确认是否仍在执行"
            item = {
                "id": app_id + ":" + session_id, "app_id": app_id, "app_name": app_name,
                "title": str(row["custom_title"] or row["title"] or "未命名会话")[:160],
                "project": project, "status": status, "evidence": evidence,
                "updated_at": max(updated_at, event_at, start), "source": "local-session",
                "status_reason": reason,
                # Verified in WorkBuddy.app main/index.js showTaskNotification.
                # The AI edition's scheme and source disagree, so leave empty.
                "target": "workbuddy://chat/" + quote(session_id, safe="") if app_id == "workbuddy" else "",
            }
            if start:
                item.update(started_at=start, timing_basis="turn", timing_reason="从本轮用户消息的时间开始计算")
            else:
                item["timing_reason"] = meta.get("timing_reason", "会话索引没有本轮开始时间")
            if event_at:
                item["last_activity_at"] = event_at
            if status == "interrupted" and meta.get("error_at", 0) >= start and meta.get("error_at"):
                item["ended_at"] = meta["error_at"]
            elif status in {"interrupted", "completed", "idle"} and event_at >= start and event_at:
                item.update(ended_at=event_at, timing_basis="last-confirmed",
                            timing_reason="应用未提供准确结束时间，计时截止到本轮最后一条执行事件")
            if pid is not None:
                item["pid"] = pid
            result.append(item)
        return result

    @staticmethod
    def _zcode_workspace_link(project: str) -> str:
        # ZCode's own CLI opens workspaces via this exact deep link
        # (encodeURIComponent on the absolute path). NativeMonitor re-validates
        # and only opens it when the registered handler is ZCode itself.
        if not project.startswith("/") or len(project) > 512:
            return ""
        if any(not part or part in {".", ".."} for part in project.split("/")[1:]) or \
                any(c in project for c in "\0\n\r"):
            return ""
        return "zcode://workspace/open?path=" + quote(project, safe="")

    def _zcode_turns(self, rows):
        """Read actual lifecycle rows, never infer turn completion from model I/O.

        The desktop index is a task list; the CLI database owns turn_usage.
        Join session.directory to bind that ID to the indexed workspace. Only
        bounded lifecycle columns are selected, never message/tool bodies.
        """
        self._zcode_turn_error = ""
        path = self.home / ".zcode/cli/db/db.sqlite"
        if not path.is_file():
            return {}
        connection = None
        result = {}
        try:
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.15)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            # Keep identity, lifecycle and activity reads on one SQLite snapshot.
            connection.execute("BEGIN")
            deadline = time.monotonic() + 1.0
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"turn_usage", "session"} <= tables:
                return {}  # Older ZCode versions retain the journal fallback.
            for row in rows[:SESSION_LIMIT]:
                turn = connection.execute("""
                    SELECT t.session_id, t.turn_id, t.status, t.started_at, t.completed_at,
                           t.cancelled_by_user, t.context_exceeded, t.error_type, t.error_code
                    FROM turn_usage t JOIN session s ON s.id = t.session_id
                    WHERE t.session_id = ? AND s.directory = ?
                    ORDER BY t.started_at DESC LIMIT 1
                """, (row["task_id"], row["workspace_path"])).fetchone()
                if turn is None:
                    owner = connection.execute("SELECT directory FROM session WHERE id = ?", (row["task_id"],)).fetchone()
                    if owner and owner[0] != row["workspace_path"]:
                        result[(row["workspace_key"], row["task_id"])] = {"identity_mismatch": True}
                    continue
                meta = dict(turn)
                activity = _epoch(meta["started_at"])
                for table in ("model_usage", "tool_usage"):
                    # Terminal rounds already have authoritative timestamps.
                    # Scanning their potentially large usage histories wastes
                    # the shared query budget and can hide unrelated live tasks.
                    if meta["status"] != "running" or table not in tables:
                        continue
                    stamp = connection.execute(
                        "SELECT MAX(MAX(started_at, COALESCE(completed_at, 0))) FROM " + table +
                        " WHERE session_id = ? AND turn_id = ?", (meta["session_id"], meta["turn_id"])
                    ).fetchone()[0]
                    activity = max(activity, _epoch(stamp))
                meta["activity"] = max(activity, _epoch(meta["completed_at"]))
                result[(row["workspace_key"], row["task_id"])] = meta
            return result
        except (OSError, sqlite3.Error, ValueError) as error:
            detail = ("读取超过 1 秒预算，将在下轮重试" if
                      (getattr(error, "sqlite_errorcode", None) == 9 or
                       isinstance(error, sqlite3.OperationalError) and str(error) == "interrupted") else
                      "轮次数据库读取失败（" + type(error).__name__ + "）")
            self._zcode_turn_error = "ZCode：" + detail + "，当前状态待确认"
            self.last_errors.append(self._zcode_turn_error)
            return {}
        finally:
            if connection is not None:
                connection.close()

    def _zcode_active_ids(self, now):
        """Prioritize actual live rounds before the task list's 40-row cap.

        A newly resumed old task can keep its old index timestamp until it
        finishes, so sorting that index alone can omit the running task.
        """
        path = self.home / ".zcode/cli/db/db.sqlite"
        if not path.is_file():
            return []
        connection = None
        try:
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.15)
            connection.execute("PRAGMA query_only = ON")
            deadline = time.monotonic() + 1.0
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "turn_usage" not in tables:
                return []
            active = []
            for sid, turn, started in connection.execute("""
                SELECT t.session_id, t.turn_id, t.started_at FROM turn_usage t
                WHERE t.status = 'running' AND NOT EXISTS (
                    SELECT 1 FROM turn_usage newer WHERE newer.session_id = t.session_id
                    AND newer.started_at > t.started_at)
                ORDER BY t.started_at DESC LIMIT ?
            """, (SESSION_LIMIT,)):
                activity = _epoch(started)
                for table in ("model_usage", "tool_usage"):
                    if table in tables:
                        stamp = connection.execute(
                            "SELECT MAX(MAX(started_at, COALESCE(completed_at, 0))) FROM " + table +
                            " WHERE session_id = ? AND turn_id = ?", (sid, turn)).fetchone()[0]
                        activity = max(activity, _epoch(stamp))
                if _recent(activity, now, ACTIVE_MAX_AGE):
                    active.append(sid)
            return active
        except (OSError, sqlite3.Error, ValueError):
            return []  # The main lifecycle reader reports database errors.
        finally:
            if connection is not None:
                connection.close()

    def _zcode_log_turns(self):
        """Read bounded ZCode lifecycle metadata; never retain log bodies.

        The CLI database can lag behind a newly started desktop turn. Logs use
        exact session and turn IDs, so a later start can supersede that ledger
        without treating a file modification or app process as a running turn.
        """
        directory = self.home / ".zcode/cli/log"
        try:
            paths = sorted(directory.glob("zcode-????-??-??.jsonl"))[-2:]
        except OSError:
            return {}
        selected = {str(path) for path in paths}
        self._zcode_log_cache = {key: value for key, value in self._zcode_log_cache.items()
                                 if key in selected}
        previous_day = None
        for path in paths:
            key = str(path)
            try:
                stat = path.stat()
                old = self._zcode_log_cache.get(key)
                reset = not old or old["inode"] != stat.st_ino or old["offset"] > stat.st_size or \
                    (old["offset"] == stat.st_size and old["mtime"] != stat.st_mtime_ns) or \
                    stat.st_size - old["offset"] > ZCODE_LOG_TAIL_BYTES
                start = max(0, stat.st_size - ZCODE_LOG_TAIL_BYTES) if reset else old["offset"]
                turns = ({sid: turn.copy() for sid, turn in previous_day.items()}
                         if reset and previous_day else {} if reset else old["turns"])
                with path.open("rb") as stream:
                    stream.seek(start)
                    data = stream.read(min(stat.st_size - start, ZCODE_LOG_TAIL_BYTES))
                end = data.rfind(b"\n")
                if end < 0:
                    continue
                lines = data[:end].split(b"\n")
                if reset and start > 0:
                    lines = lines[1:]  # Tail may start inside a JSON record.
                for line in lines:
                    if len(line) > 8192 or b'"turnId"' not in line or b'"sessionId"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    event = record.get("event")
                    if event not in {"turn.started", "turn.completed", "turn.failed",
                                     "model.request.started", "model.request.completed", "model.request.failed",
                                     "tool.call.started", "tool.call.completed", "tool.call.failed",
                                     "turn.phase.started", "turn.phase.completed",
                                     "subagent.spawned", "subagent.completed"}:
                        continue
                    sid, turn = record.get("sessionId"), record.get("turnId")
                    stamp = _iso_epoch(record.get("timestamp"))
                    if not isinstance(sid, str) or not _ZCODE_ROLLOUT_ID.fullmatch(sid) or \
                            not isinstance(turn, str) or not turn or len(turn) > 128 or not stamp:
                        continue
                    prior = turns.get(sid)
                    if event == "turn.started":
                        if prior is None or stamp >= prior["start"]:
                            turns[sid] = {"turn": turn, "start": stamp, "activity": stamp,
                                          "end": 0.0, "terminal": "", "rate_limited": False}
                    elif prior and prior["turn"] == turn and stamp >= prior["start"]:
                        prior["activity"] = max(prior["activity"], stamp)
                        if event == "model.request.failed":
                            context = record.get("context")
                            code = context.get("statusCode") if isinstance(context, dict) else None
                            prior["rate_limited"] = code in (429, "429")
                        elif event == "model.request.completed":
                            prior["rate_limited"] = False
                        if event in {"turn.completed", "turn.failed"}:
                            prior["end"] = stamp
                            prior["terminal"] = event
                self._zcode_log_cache[key] = {"inode": stat.st_ino, "mtime": stat.st_mtime_ns,
                                               "offset": start + end + 1, "turns": turns}
                previous_day = turns
            except OSError:
                continue
        latest = {}
        for state in self._zcode_log_cache.values():
            for sid, turn in state["turns"].items():
                if sid not in latest or turn["start"] >= latest[sid]["start"]:
                    latest[sid] = turn
        return latest

    def _zcode_rollout(self, task_id: str, now: float) -> Optional[dict]:
        """Compatibility fallback: complete, top-level model I/O metadata only.

        Completed records put completedAt BEFORE their potentially huge body.
        Reading only the last 256 KB loses that field and invents an in-flight
        request. Assemble bounded full lines backwards; never regex body text.
        """
        if not _ZCODE_ROLLOUT_ID.fullmatch(task_id):
            return None
        path = self.home / ".zcode/cli/rollout" / ("model-io-" + task_id + ".jsonl")
        try:
            stat = path.stat()
        except OSError:
            return None
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        key = (str(path), "zcode-rollout")
        previous = self._file_cache.get(key)
        if previous and previous[0] == signature:
            return previous[1]
        meta = {"turn_id": "", "turn_start": 0.0, "last_started": 0.0,
                "last_completed": 0.0}
        earliest, boundary = 0.0, False
        try:
            with path.open("rb") as stream:
                end, carry = stat.st_size, b""
                scanned, first = 0, True
                while end > 0 and scanned < 16 * 1024 * 1024 and not boundary:
                    start = max(0, end - ZCODE_ROLLOUT_TAIL_BYTES)
                    stream.seek(start)
                    data = stream.read(end - start) + carry
                    scanned += end - start
                    if first and b"\n" not in data:
                        end = start
                        continue  # Still inside a large incomplete last line.
                    lines = data.split(b"\n")
                    if first:
                        lines.pop()  # Empty newline suffix or a partial append.
                        first = False
                    carry = lines.pop(0) if start > 0 and lines else b""
                    if len(carry) > 8 * 1024 * 1024:
                        break  # Missing identity/timing is safer than fragments.
                    for line in reversed(lines):
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(record, dict) or record.get("sessionId") != task_id or record.get("type") != "model_io":
                            continue
                        turn = record.get("turnId")
                        stamp = _iso_epoch(record.get("startedAt"))
                        if not isinstance(turn, str) or not turn or not stamp:
                            continue
                        if not meta["turn_id"]:
                            meta.update(turn_id=turn, last_started=stamp,
                                        last_completed=_iso_epoch(record.get("completedAt")))
                        if turn != meta["turn_id"]:
                            boundary = True
                            break
                        earliest = min(earliest or stamp, stamp)
                    end = start
                if boundary or end == 0:
                    meta["turn_start"] = earliest
            if len(self._file_cache) >= 256:
                self._file_cache.clear()
            self._file_cache[key] = (signature, meta)
            return meta
        except OSError:
            return None

    def _zcode(self, now: float, processes: dict):
        log_turns = self._zcode_log_turns()
        active_ids = self._zcode_active_ids(now)
        active_ids.extend(sid for sid, turn in log_turns.items()
                          if not turn["terminal"] and _recent(turn["activity"], now, ACTIVE_MAX_AGE)
                          and sid not in active_ids)
        active_ids = active_ids[:SESSION_LIMIT]
        priority = "CASE WHEN task_id IN (" + ",".join("?" for _ in active_ids) + ") THEN 0 ELSE 1 END, " if active_ids else ""
        rows = self._rows(self.home / ".zcode/v2/tasks-index.sqlite", """
            SELECT workspace_key, workspace_path, task_id, title, task_status, updated_at
            FROM tasks WHERE deleted = 0 AND archived = 0
            ORDER BY """ + priority + "updated_at DESC LIMIT ?", "ZCode", active_ids)
        turns = self._zcode_turns(rows)
        app_is_live = any(_belongs_to_app(command, "ZCode") for command in processes.values())
        result = []
        for row in rows:
            task_id = str(row["task_id"] or "")
            if not task_id:
                continue
            project = str(row["workspace_path"] or "")
            # The real database primary key is workspace_key + task_id.
            workspace_id = hashlib.sha256(str(row["workspace_key"]).encode()).hexdigest()[:12]
            updated_at = _epoch(row["updated_at"])
            # The ZCode index only rewrites updated_at on task state changes, so
            # a long quiet model stretch would fade a genuinely running task
            # after ten minutes. With the app process alive, the explicit
            # running status plus its live owner stay confirmed for 30 minutes.
            status, evidence = self._status(
                row["task_status"], app_is_live,
                _recent(updated_at, now, 1800 if app_is_live else ACTIVE_MAX_AGE))
            lifecycle = turns.get((row["workspace_key"], row["task_id"]))
            identity_mismatch = bool(lifecycle and lifecycle.get("identity_mismatch"))
            if identity_mismatch:
                lifecycle = None
            if (lifecycle and lifecycle["status"] != "running" and
                    str(row["task_status"]).lower() in RUNNING_STATES and
                    updated_at > _epoch(lifecycle["completed_at"]) + 1):
                lifecycle = None  # The index already admitted a newer turn.
            rollout = None if lifecycle or identity_mismatch else self._zcode_rollout(task_id, now)
            # 索引只在轮次结束时改写：整个运行中的轮次都会读成 completed。
            # 会话日志是地面真相——最后一次模型请求晚于索引时间（或仍有在飞
            # 请求）且应用存活，就是活跃轮次，覆盖索引的滞后状态。
            turn_live = False
            if rollout and app_is_live and rollout["last_started"] > 0:
                if (not rollout["last_completed"] and
                        _recent(rollout["last_started"], now, ZCODE_INFLIGHT_MAX_AGE) and
                        rollout["last_started"] > updated_at):
                    turn_live = True
                elif (rollout["last_started"] > updated_at and rollout["last_completed"] > 0
                      and _recent(rollout["last_completed"], now, 300)):
                    turn_live = True
            if turn_live:
                status, evidence = "running", "会话日志显示本轮模型请求进行中，索引状态滞后"
            elif rollout and rollout["last_started"] > updated_at:
                status, evidence = "unknown", "本轮日志晚于索引，运行证据已过期，结束状态待确认"
            item = {
                "id": "zcode:" + workspace_id + ":" + task_id,
                "app_id": "zcode", "app_name": "ZCode",
                "title": str(row["title"] or "未命名会话")[:160], "project": project,
                "status": status, "evidence": evidence, "updated_at": updated_at,
                "status_reason": failure_reason(stop=str(row["task_status"])) if status == "interrupted" else evidence,
                "timing_reason": "ZCode 会话索引仅提供更新时间，没有本轮开始时间",
                "source": "local-session",
                "target": self._zcode_workspace_link(project),
            }
            if lifecycle and _epoch(lifecycle["started_at"]) > 0:
                start, end = _epoch(lifecycle["started_at"]), _epoch(lifecycle["completed_at"])
                phase = lifecycle["status"]
                item.update(started_at=start, timing_basis="turn",
                            timing_reason="按 ZCode 轮次数据库的真实开始事件计算",
                            last_activity_at=lifecycle["activity"],
                            updated_at=max(updated_at, lifecycle["activity"]))
                if phase == "running":
                    live = app_is_live and _recent(lifecycle["activity"], now, ACTIVE_MAX_AGE)
                    status = "running" if live else "unknown"
                    evidence = ("轮次尚未结束，模型或工具生命周期事件仍新鲜" if live else
                                "轮次未结束，但执行事件已过期或应用已退出，状态待确认")
                elif phase in {"completed", "error", "cancelled"} and end >= start:
                    status = "completed" if phase == "completed" else "interrupted"
                    evidence = "ZCode 轮次数据库明确记录了结束事件"
                    item["ended_at"] = end
                    if lifecycle["cancelled_by_user"]:
                        item["user_stopped"] = True
                        evidence = "用户主动停止了本轮任务"
                    elif lifecycle["context_exceeded"]:
                        evidence = "上下文超过模型限制"
                    elif phase != "completed":
                        detail = str(lifecycle["error_type"] or "") + " " + str(lifecycle["error_code"] or "")
                        evidence = failure_reason(detail, stop=phase)
                        log_failure = log_turns.get(task_id)
                        if (phase == "error" and log_failure and
                                log_failure["turn"] == str(lifecycle["turn_id"]) and
                                log_failure["terminal"] == "turn.failed" and
                                abs(log_failure["end"] - end) <= 2 and
                                log_failure.get("rate_limited")):
                            evidence = "使用频率或配额超限（HTTP 429）"
                else:
                    status, evidence = "unknown", "轮次数据库尚未提供完整的开始与结束状态"
                item.update(status=status, evidence=evidence, status_reason=evidence)
            if rollout and rollout["turn_start"] > 0:
                item["started_at"] = rollout["turn_start"]
                item["timing_basis"] = "turn"
                item["timing_reason"] = "按本轮首次模型请求时间计算（会话日志）"
                if rollout["last_started"] > 0 or rollout["last_completed"] > 0:
                    item["last_activity_at"] = max(rollout["last_started"], rollout["last_completed"])
                    item["updated_at"] = max(updated_at, item["last_activity_at"])
            if self._zcode_turn_error:
                item.update(status="unknown", evidence=self._zcode_turn_error,
                            status_reason=self._zcode_turn_error)
            if identity_mismatch:
                reason = "任务索引与轮次数据库的工作区不一致，无法核验对应会话"
                item.update(status="unknown", evidence=reason, status_reason=reason)
            log_turn = log_turns.get(task_id)
            ledger_end = _epoch(lifecycle["completed_at"]) if lifecycle and lifecycle.get("completed_at") else 0.0
            if (log_turn and not identity_mismatch and not self._zcode_turn_error and
                    log_turn["start"] > max(updated_at, ledger_end) + 1):
                # The log's turn.started timestamp can trail turn_usage.started_at
                # by milliseconds. Use the database's canonical start whenever
                # both sources name the same turn, including before it ends.
                # Otherwise an interrupted terminal snapshot looks like a
                # different round to the continuation policy.
                same_turn = lifecycle and log_turn["turn"] == str(lifecycle["turn_id"])
                turn_start = _epoch(lifecycle["started_at"]) if same_turn else log_turn["start"]
                item.update(started_at=turn_start, timing_basis="turn",
                            timing_reason="按 ZCode 本机生命周期日志的本轮开始事件计算",
                            last_activity_at=log_turn["activity"],
                            updated_at=max(updated_at, log_turn["activity"]))
                if log_turn["terminal"]:
                    terminal = log_turn["terminal"]
                    evidence = ("ZCode 本机生命周期日志记录了本轮完成事件" if terminal == "turn.completed" else
                                "ZCode 本机生命周期日志记录了本轮失败事件，原因待确认")
                    item.update(status="completed" if terminal == "turn.completed" else "unknown",
                                evidence=evidence, status_reason=evidence, ended_at=log_turn["end"])
                else:
                    live = app_is_live and _recent(log_turn["activity"], now, ACTIVE_MAX_AGE)
                    evidence = ("ZCode 本机生命周期日志记录新轮次正在执行" if live else
                                "新轮次尚无结束事件，但活动证据已过期或应用已退出，状态待确认")
                    item.update(status="running" if live else "unknown",
                                evidence=evidence, status_reason=evidence)
            result.append(item)
        return result

    def collect(self) -> list:
        self.last_errors = []
        self._failed_apps = set()
        now = self.clock()
        processes = self._live_processes(now)
        result = self._remember_or_degrade("zcode", "ZCode", self._zcode(now, processes))
        for app_id, app_name, root_name in (
                ("workbuddy", "WorkBuddy", ".workbuddy"),
                ("workbuddy-ai", "WorkBuddy AI", ".workbuddy-ai")):
            rows = self._workbuddy(app_id, app_name, root_name, now, processes)
            result.extend(self._remember_or_degrade(app_id, app_name, rows))
        return result

    def _remember_or_degrade(self, app_id: str, app_name: str, rows: list) -> list:
        if app_name not in self._failed_apps:
            self._last_results[app_id] = [dict(row) for row in rows]
            return rows
        previous = []
        for cached in self._last_results.get(app_id, []):
            row = dict(cached)
            row["status"] = "unknown"
            row["evidence"] = "会话索引读取失败；显示上次成功采集内容，当前状态待确认"
            row["status_reason"] = "会话索引本轮读取失败；缓存不能证明当前任务状态"
            row.pop("pid", None)
            previous.append(row)
        return previous


_collector = DesktopCollector()


def collect() -> list[dict]:
    return _collector.collect()


def diagnostics() -> list[str]:
    """Errors from the latest collection, without external file contents."""
    return list(_collector.last_errors)
