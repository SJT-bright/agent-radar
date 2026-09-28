"""Read-only, bounded local Codex and Claude Code session discovery.

Only metadata and event kinds are retained. Authentication/config files are never
opened. SQLite connections use mode=ro; no hooks are installed into either app.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote
try:
    from .failure_reasons import failure_reason
except ImportError:
    from failure_reasons import failure_reason

MAX_SESSIONS = 50
TAIL_BYTES = 262_144
CODEX_SCAN_BYTES = 8 * 1024 * 1024


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value / 1000 if value > 10_000_000_000 else value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def _text(value: Any, limit: int = 180) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 65536:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def _readonly(path: Path):
    # URI mode also prevents a missing or concurrently removed database from
    # being silently created; query_only is a second read-only boundary.
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.12)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = ON")
        yield con
    finally:
        con.close()


def _processes() -> dict[int, dict]:
    """ps comm contains executable names only, never command arguments/secrets."""
    try:
        raw = subprocess.run(
            ["ps", "-axo", "pid=,lstart=,comm="], capture_output=True,
            text=True, timeout=3, check=True, env={**os.environ, "TZ": "UTC"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in raw.splitlines():
        parts = line.strip().split(None, 6)
        if len(parts) != 7 or not parts[0].isdigit():
            continue
        executable = parts[6]
        leaf = Path(executable).name.lower()
        if leaf not in {"codex", "claude", "claude-code"}:
            continue
        found[int(parts[0])] = {
            "app": "codex" if leaf == "codex" else "claude",
            "started": " ".join(parts[1:6]),
        }
    return found


def _codex_owners(processes: dict[int, dict], root: Path) -> dict[str, int]:
    pids = [str(pid) for pid, proc in processes.items() if proc["app"] == "codex"]
    if not pids:
        return {}
    prefix = str(root / "thread-writer-locks") + os.sep
    owners: dict[str, int] = {}
    # lsof over every codex process can transiently return nothing while the
    # kernel table is busy; one bounded retry keeps running tasks confirmed.
    for attempt in range(2):
        try:
            result = subprocess.run(
                ["lsof", "-nP", "-p", ",".join(pids), "-F", "pn"],
                capture_output=True, text=True, timeout=4,
            )
        except (OSError, subprocess.SubprocessError):
            if attempt == 0:
                time.sleep(0.3)
                continue
            return {}
        owners, current = {}, None
        for line in result.stdout.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current = int(line[1:])
            elif current in processes and line.startswith("n" + prefix) and line.endswith(".lock"):
                owners[Path(line[1:]).stem] = current
        if owners or attempt == 1:
            return owners
        time.sleep(0.3)
    return owners


class TailCache:
    """Cache parsed metadata keyed by inode/size/mtime; retain no conversation text."""
    def __init__(self):
        self.entries: dict[str, tuple[tuple, dict]] = {}
        self.offsets: dict[str, int] = {}
        self.pending: dict[str, dict[str, float]] = {}

    @staticmethod
    def _lines(raw: bytes, skip_partial: bool) -> list[bytes]:
        lines = raw.splitlines()
        if skip_partial and lines:
            lines = lines[1:]
        return lines

    def _window(self, stream, size: int, app: str) -> tuple[int, bytes]:
        start = max(0, size - TAIL_BYTES)
        stream.seek(start)
        raw = stream.read(size - start)
        if app != "codex":
            return start, raw
        # Large tool outputs can exceed the ordinary tail window and erase the
        # latest task_started. Find its actual event, never infer running from
        # a recent file write. Cold reads remain bounded; warm reads are incremental.
        while True:
            found = False
            for line in reversed(self._lines(raw, bool(start))):
                if b"task_started" not in line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if (isinstance(event, dict) and event.get("type") == "event_msg"
                        and isinstance(event.get("payload"), dict)
                        and event["payload"].get("type") == "task_started"):
                    found = True
                    break
            if found or start == 0 or len(raw) >= CODEX_SCAN_BYTES:
                return start, raw
            next_start = max(0, size - min(CODEX_SCAN_BYTES, len(raw) * 2))
            stream.seek(next_start)
            raw = stream.read(start - next_start) + raw
            start = next_start

    def read(self, path: Path, app: str) -> dict:
        key = str(path)
        try:
            stat = path.stat()
            signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if key in self.entries and self.entries[key][0] == signature:
                return self.entries[key][1].copy()
            previous = self.entries.get(key)
            offset = self.offsets.get(key, 0)
            incremental = (app == "codex" and previous is not None
                           and previous[0][0] == stat.st_ino and previous[0][1] < stat.st_size
                           and 0 <= stat.st_size - offset <= CODEX_SCAN_BYTES)
            with path.open("rb") as stream:
                if incremental:
                    start = offset
                    stream.seek(start)
                    raw = stream.read(stat.st_size - start)
                else:
                    start, raw = self._window(stream, stat.st_size, app)
            # Never consume a partial final JSON record: reread it when completed.
            newline = raw.rfind(b"\n")
            complete = (raw[:newline + 1] if newline >= 0 else b"") if app == "codex" else raw
            self.offsets[key] = start + len(complete)
            lines = self._lines(complete, bool(start) and not incremental)
            state: dict[str, Any] = previous[1].copy() if incremental else {"updated_at": 0.0}
            pending: dict[str, float] = self.pending.get(key, {}).copy() if incremental else {}
            if app == "codex" and not incremental and start:
                state["lifecycle_scan_incomplete"] = True
            # Waiting is derived from pending requests and must clear on an answer.
            # 已无未决请求时不得停留在等待态：清空 pending 后解除等待，
            # 否则整轮剩余时间都误显示 waiting（原条件写反成死分支）。
            if incremental and state.get("event") == "request_user_input" and not pending:
                state.update(status="running", event="task_started", state_at=state.get("started_at", 0))
            for line in lines:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                stamp = _timestamp(event.get("timestamp"))
                state["updated_at"] = max(state["updated_at"], stamp)
                kind = event.get("type")
                if app == "codex":
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    subtype = payload.get("type")
                    if kind == "event_msg":
                        states = {"task_started": "running", "task_complete": "completed",
                                  "turn_aborted": "interrupted", "task_failed": "interrupted",
                                  "exec_approval_request": "waiting",
                                  "apply_patch_approval_request": "waiting",
                                  "request_user_input": "waiting"}
                        if subtype in states:
                            turn = payload.get("turn_id")
                            current_turn = state.get("turn_id")
                            if (subtype != "task_started" and turn and current_turn and turn != current_turn):
                                continue  # A late old-turn abort cannot terminate the current turn.
                            state.update(status=states[subtype], state_at=stamp,
                                         event=subtype, turn_id=turn or current_turn)
                            if subtype == "task_started":
                                state.pop("lifecycle_scan_incomplete", None)
                                state["started_at"] = stamp
                                state.pop("ended_at", None)
                                state.pop("status_reason", None)
                                pending.clear()
                            elif subtype in {"task_complete", "turn_aborted", "task_failed"}:
                                state["ended_at"] = stamp
                                if subtype != "task_complete":
                                    state["status_reason"] = failure_reason(payload.get("error"), subtype)
                                pending.clear()
                    if kind == "response_item":
                        if subtype in {"function_call", "custom_tool_call"}:
                            name = str(payload.get("name", ""))
                            if name.rsplit(".", 1)[-1] in {"request_user_input", "request_user_input_async"}:
                                pending[str(payload.get("call_id"))] = stamp
                        elif subtype in {"function_call_output", "custom_tool_call_output"}:
                            pending.pop(str(payload.get("call_id")), None)
                else:
                    if isinstance(event.get("cwd"), str):
                        state["project"] = event["cwd"]
                    if kind in {"custom-title", "summary"}:
                        title = event.get("customTitle") or event.get("summary")
                        if title:
                            state["title"] = _text(title)
                    message = event.get("message")
                    stop = message.get("stop_reason") if isinstance(message, dict) else None
                    if kind == "assistant" and stop == "end_turn":
                        state.update(status="completed", state_at=stamp, event="end_turn")
                    elif kind == "system" and event.get("subtype") == "turn_duration":
                        state.update(status="completed", state_at=stamp, event="turn_duration")
                    elif kind == "user" or (kind == "assistant" and stop == "tool_use"):
                        # A user message alone is not evidence of a live model run.
                        state.update(status="unknown", state_at=stamp, event=kind)
            if pending:
                pending_at = max(pending.values())
                if pending_at >= state.get("state_at", 0):
                    state.update(status="waiting", state_at=pending_at, event="request_user_input")
            if key not in self.entries and len(self.entries) >= MAX_SESSIONS * 3:
                oldest = next(iter(self.entries))
                self.entries.pop(oldest)
                self.offsets.pop(oldest, None)
                self.pending.pop(oldest, None)
            self.pending[key] = pending
            self.entries[key] = (signature, state.copy())
            return state
        except OSError:
            return {"unreadable": True, "updated_at": 0.0}


def _codex_lifecycle(history: dict, tail: dict) -> tuple[dict, str]:
    """Choose one coherent round before deriving its status and clock."""
    history_at = _timestamp(history.get("completed_at") or history.get("started_at"))
    history_start = _timestamp(history.get("started_at"))
    tail_start = tail.get("started_at", 0)
    event_at = tail.get("state_at", 0)
    if (tail.get("lifecycle_scan_incomplete") and not tail.get("status")
            and tail.get("updated_at", 0) > history_at
            and history.get("status") != "inProgress"):
        return {"status": "unknown"}, "日志生命周期超出有界读取范围；旧状态表不足以确认当前轮次"
    same_turn = bool(history.get("turn_id") and history.get("turn_id") == tail.get("turn_id"))
    different_turn = bool(history.get("turn_id") and tail.get("turn_id") and not same_turn)
    if (tail.get("lifecycle_scan_incomplete") and not tail_start and not same_turn
            and tail.get("status") in {"completed", "interrupted"}
            and event_at >= history_start):
        # An end event without its start cannot establish whether this is a
        # newer round or a late old-round record. Do not join it to another
        # round's projected status/start, even when that projection says stopped.
        return {"status": "unknown"}, "日志结束事件缺少可核验的同轮起点；当前轮次待确认"
    # Database times are whole seconds, while lifecycle events include fractions.
    # Comparing round starts avoids a delayed old terminal event winning by date.
    if different_turn and tail_start and history_start:
        tail_wins = tail_start >= history_start
    elif different_turn and not tail_start:
        tail_wins = False
    else:
        tail_wins = bool(tail.get("status") and event_at >= history_at)
    if (tail_wins or not history.get("status")) and tail.get("status"):
        chosen = tail.copy()
        if same_turn and not chosen.get("started_at"):
            chosen["started_at"] = history_start
        return chosen, "日志 " + tail.get("event", "缺少状态事件")
    chosen = dict(history)
    chosen["status"] = {"inProgress": "running", "completed": "completed",
                        "interrupted": "interrupted", "failed": "interrupted"}.get(history.get("status"), "unknown")
    chosen["raw_status"] = history.get("status")
    chosen["started_at"] = history_start
    chosen["ended_at"] = _timestamp(history.get("completed_at"))
    return chosen, "会话表 " + str(history.get("status", "缺少状态事件"))


def _codex_status(history: dict, tail: dict, pid: int | None) -> tuple[str, str]:
    lifecycle, origin = _codex_lifecycle(history, tail)
    status = lifecycle.get("status", "unknown")
    if status in {"running", "waiting"}:
        if not pid:
            return "unknown", origin + "；未找到持有本会话的存活进程"
        return status, origin + "；会话锁关联存活进程"
    if status == "completed":
        return "completed", origin + "；本轮已结束"
    if status == "interrupted":
        return "interrupted", origin + "；本轮已中断"
    return "unknown", origin


def _codex_state_databases(root: Path) -> list[Path]:
    """state_N numbering is a migration counter; sort numerically so state_10
    outranks state_9, and never let an unreadable name ordering pick a stale one."""
    def order(path: Path):
        digits = "".join(ch if ch.isdigit() else "" for ch in path.stem)
        return (int(digits) if digits else -1, path.name)
    return sorted(root.glob("state_*.sqlite"), key=order, reverse=True)


class SessionCollector:
    def __init__(self, codex_home: Path | None = None, claude_home: Path | None = None):
        self.codex_home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.claude_home = claude_home or Path.home() / ".claude"
        self.tails = TailCache()
        self.codex_rows: list[dict] = []
        self.last_errors: list[str] = []
        self._claude_paths: list[Path] = []
        self._claude_scan_at = float("-inf")

    def _read_threads_database(self, database: Path) -> bool:
        """Read one state database; True when it yielded the session rows."""
        for attempt in range(2):
            try:
                with _readonly(database) as con:
                    columns = {row[1] for row in con.execute("PRAGMA table_info(threads)")}
                    if "id" not in columns:
                        return False
                    requested = [c for c in ("id", "name", "title", "cwd", "rollout_path", "updated_at",
                                             "agent_path", "agent_nickname") if c in columns]
                    # Delegated agents share their parent's desktop task. Filter before
                    # LIMIT so background workers cannot evict visible conversations.
                    main_tasks = (" AND (agent_path IS NULL OR agent_path='' OR agent_path='/root')"
                                  if "agent_path" in columns else "")
                    rows = con.execute("SELECT " + ",".join(requested) +
                                       " FROM threads WHERE archived=0" + main_tasks +
                                       " ORDER BY updated_at DESC LIMIT ?",
                                       (MAX_SESSIONS,)).fetchall()
                    self.codex_rows = [dict(row) for row in rows]
                    return True
            except (OSError, sqlite3.Error) as error:
                # A busy Codex writer locks its state database briefly; one
                # bounded retry avoids a whole cycle of degraded unknowns.
                transient = (isinstance(error, sqlite3.OperationalError) and
                             ("locked" in str(error).lower() or "busy" in str(error).lower()))
                if attempt == 0 and transient:
                    time.sleep(0.2)
                    continue
                return False
        return False

    def _codex(self, processes: dict[int, dict]) -> list[dict]:
        root = self.codex_home
        databases = _codex_state_databases(root)
        if not databases:
            return []
        available = True
        opened = False
        # Use the newest database that still exposes the projected threads
        # table; a just-migrated or partial state file must not hide sessions.
        # On total failure codex_rows keeps the previous collection for degrade.
        for database in databases[:3]:
            if self._read_threads_database(database):
                opened = True
                break
        if not opened:
            available = False
            self.last_errors.append("Codex 会话数据库暂时不可读")
        owners = _codex_owners(processes, root)
        histories: dict[str, dict] = {}
        try:
            with _readonly(root / "thread_history_1.sqlite") as con:
                columns = {row[1] for row in con.execute("PRAGMA table_info(thread_turns)")}
                fields = "status,started_at,completed_at" + (",turn_id" if "turn_id" in columns else "")
                for row in self.codex_rows:
                    history = con.execute(
                        "SELECT " + fields + " FROM thread_turns "
                        "WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1", (row["id"],),
                    ).fetchone()
                    if history:
                        histories[row["id"]] = dict(history)
        except (OSError, sqlite3.Error):
            self.last_errors.append("Codex 状态表不可读，使用有界日志事件")
        result = []
        for row in self.codex_rows:
            thread = row["id"]
            path = Path(row.get("rollout_path") or "")
            # Local session rows cannot cause reads outside the known session roots.
            allowed = any(path.resolve().is_relative_to((root / d).resolve())
                          for d in ("sessions", "archived_sessions"))
            tail = self.tails.read(path, "codex") if allowed else {}
            pid = owners.get(thread)
            state, evidence = _codex_status(histories.get(thread, {}), tail, pid)
            if not available:
                state, evidence = "unknown", "会话数据库暂时不可读；显示上次采集的会话信息"
            title = row.get("name") or row.get("title")
            if not title:
                title = row.get("agent_path") or row.get("agent_nickname") or "未命名任务"
            item = dict(id="codex:" + thread, app_id="codex", app_name="Codex", title=_text(title),
                        project=row.get("cwd") or "", status=state, evidence=evidence,
                        updated_at=max(_timestamp(row.get("updated_at")), tail.get("updated_at", 0)),
                        source="local-session", target="codex://threads/" + quote(thread, safe=""))
            history = histories.get(thread, {})
            lifecycle, _ = _codex_lifecycle(history, tail)
            start = _timestamp(lifecycle.get("started_at"))
            end = _timestamp(lifecycle.get("ended_at"))
            if start:
                item.update(started_at=start, timing_basis="turn")
            item["status_reason"] = lifecycle.get("status_reason") if state == "interrupted" else evidence
            if not item["status_reason"]:
                # 按会话表真实状态回退：failed 是崩溃失败（非阻断文案），
                # 仅 interrupted 保留「中止事件」阻断语义。
                item["status_reason"] = failure_reason(stop=str(lifecycle.get("raw_status") or "interrupted"))
            item["timing_reason"] = ("按 Codex 本轮开始事件计算" if start else
                                     "Codex 状态表及有界日志均未提供本轮开始时间")
            if state in {"completed", "interrupted"} and end >= start and end:
                item["ended_at"] = end
            if tail.get("updated_at"):
                item["last_activity_at"] = tail["updated_at"]
            if pid:
                item["pid"] = pid
            result.append(item)
        return result

    def _claude_recent(self) -> list[Path]:
        now = time.monotonic()
        if now - self._claude_scan_at < 30:
            return self._claude_paths
        self._claude_scan_at = now
        stamped: list[tuple[float, Path]] = []
        try:
            # Directory metadata only; do not walk archives, subagents or credentials.
            directories = sorted(self.claude_home.joinpath("projects").iterdir(),
                                 key=lambda p: p.stat().st_mtime, reverse=True)[:60]
        except OSError:
            return self._claude_paths
        for directory in directories:
            if not directory.is_dir():
                continue
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 1000:
                        break
                    if entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                        try:
                            stamped.append((entry.stat().st_mtime, Path(entry.path)))
                        except OSError:
                            continue  # A transcript deleted mid-scan only removes itself.
        stamped.sort(key=lambda pair: pair[0], reverse=True)
        self._claude_paths = [path for _, path in stamped[:MAX_SESSIONS]]
        return self._claude_paths

    def _claude(self, processes: dict[int, dict]) -> list[dict]:
        records: dict[str, dict] = {}
        try:
            paths = sorted(self.claude_home.joinpath("sessions").glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_SESSIONS]
        except OSError:
            paths = []
        for path in paths:
            data = _read_json(path)
            sid, pid = data.get("sessionId"), data.get("pid")
            proc = processes.get(pid) if isinstance(pid, int) else None
            if not isinstance(sid, str) or not proc or proc["app"] != "claude":
                continue
            start = _text(data.get("procStart"))
            if start and proc.get("started") != start:
                continue  # stale descriptor whose PID has been reused
            records[sid] = data
        recent = {path.stem: path for path in self._claude_recent()}
        for sid, data in records.items():
            # Derive exactly the project location for live sessions, even if discovery is capped.
            encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(data.get("cwd", "")))
            candidate = self.claude_home / "projects" / encoded / (sid + ".jsonl")
            if re.fullmatch(r"[a-zA-Z0-9-]+", sid) and candidate.is_file():
                recent[sid] = candidate
        ordered = list(records) + [sid for sid in recent if sid not in records]
        result = []
        for sid in ordered[:MAX_SESSIONS]:
            data = records.get(sid, {})
            tail = self.tails.read(recent[sid], "claude") if sid in recent else {}
            raw_status = str(data.get("status", ""))
            status = {"idle": "idle", "busy": "running", "working": "running",
                      "running": "running", "thinking": "running", "waiting": "waiting",
                      "waiting_for_input": "waiting", "waiting_for_permission": "waiting",
                      "needs_attention": "waiting"}.get(raw_status)
            if data and status:
                evidence = "CLI 会话状态 " + raw_status + "；PID 与启动时间匹配"
            elif tail.get("status") == "completed":
                status, evidence = "completed", "日志 " + tail.get("event", "end_turn") + "；本轮已结束"
            else:
                status, evidence = "unknown", "缺少可核验的 CLI 运行状态"
            project = data.get("cwd") or tail.get("project") or ""
            title = tail.get("title") or data.get("name") or (Path(project).name if project else "Claude 会话")
            item = dict(id="claude:" + sid, app_id="claude-code", app_name="Claude Code", title=_text(title),
                        project=project, status=status, evidence=evidence,
                        updated_at=max(_timestamp(data.get("updatedAt")), tail.get("updated_at", 0)),
                        source="local-session", target="")
            item["status_reason"] = evidence
            item["timing_reason"] = "CLI 进程开始时间不等于本轮开始时间；当前状态数据未提供本轮起点"
            if data:
                item["pid"] = data["pid"]
            result.append(item)
        return result

    def collect(self) -> list[dict]:
        self.last_errors = []
        processes = _processes()
        rows = self._codex(processes) + self._claude(processes)
        return sorted(rows, key=lambda r: (r["status"] in {"running", "waiting"},
                                          r["updated_at"]), reverse=True)[:MAX_SESSIONS]


_collector = SessionCollector()


def collect() -> list[dict]:
    return _collector.collect()


if __name__ == "__main__":
    rows = collect()
    print(json.dumps({"count": len(rows), "sessions": rows, "diagnostics": _collector.last_errors},
                     ensure_ascii=False, indent=2))
