"""Read-only, bounded local Codex and Claude Code session discovery.

Only metadata and event kinds are retained. Authentication/config files are never
opened. SQLite connections use mode=ro; no hooks are installed into either app.
Claude can also read this app's own lifecycle file after the user explicitly
installs the reviewed hook command. That file is not Claude's settings.
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
try:
    from . import hook_events
except ImportError:
    import hook_events

MAX_SESSIONS = 50
TAIL_BYTES = 262_144
CODEX_SCAN_BYTES = 8 * 1024 * 1024
CODEX_METADATA_LIMIT = 4096


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
    """普通连接 + query_only：严格只读，且允许热读 WAL。

    mode=ro 在写入方持有 WAL/shm 时无法建立读连接（CANTOPEN，实测整周期
    不可见）；普通连接可以附离 -shm 并读 WAL，query_only 保持只读边界。
    调用方必须先确认文件存在，避免普通连接静默新建空库。
    """
    if not path.is_file():
        # 普通连接会静默新建空库；缺失文件保持与 mode=ro 相同的错误契约。
        raise sqlite3.OperationalError("unable to open database file")
    con = sqlite3.connect(str(path), timeout=0.5)
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
        self.backfills: dict[str, dict] = {}

    @staticmethod
    def _codex_event(state: dict, pending: dict, event: dict):
        """Fold lifecycle metadata in file order, including answered requests."""
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            return
        stamp = _timestamp(event.get("timestamp"))
        subtype, kind = payload.get("type"), event.get("type")
        if kind == "event_msg":
            states = {"task_started": "running", "task_complete": "completed",
                      "turn_aborted": "interrupted", "task_failed": "interrupted",
                      "exec_approval_request": "waiting",
                      "apply_patch_approval_request": "waiting", "request_user_input": "waiting"}
            if subtype in states:
                turn, current_turn = payload.get("turn_id"), state.get("turn_id")
                if subtype != "task_started" and turn and current_turn and turn != current_turn:
                    return
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
                        state["status_reason"] = payload.get("status_reason") or failure_reason(payload.get("error"), subtype)
                    pending.clear()
        elif kind == "response_item":
            if subtype in {"function_call", "custom_tool_call"}:
                if str(payload.get("name", "")).rsplit(".", 1)[-1] in {"request_user_input", "request_user_input_async"}:
                    pending[str(payload.get("call_id"))] = stamp
            elif subtype in {"function_call_output", "custom_tool_call_output"}:
                pending.pop(str(payload.get("call_id")), None)
                if not pending and state.get("event") == "request_user_input" and state.get("started_at"):
                    state.update(status="running", event="task_started", state_at=state["started_at"])

    @staticmethod
    def _metadata(raw: bytes, start: int, skip_partial: bool | None = None) -> dict[int, dict]:
        """Retain only small structural records, never prompts or tool output."""
        records, offset = {}, start
        for index, line in enumerate(raw.splitlines(keepends=True)):
            position, offset = offset, offset + len(line)
            if (index == 0 and (bool(start) if skip_partial is None else skip_partial)) or not line.endswith(b"\n") or len(line) > 65536:
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                continue
            kind, payload = event.get("type"), event["payload"]
            subtype = payload.get("type")
            if kind == "event_msg":
                if subtype not in {"task_started", "task_complete", "turn_aborted", "task_failed",
                                   "exec_approval_request", "apply_patch_approval_request", "request_user_input"}:
                    continue
            elif kind == "response_item":
                if subtype in {"function_call", "custom_tool_call"}:
                    if str(payload.get("name", "")).rsplit(".", 1)[-1] not in {"request_user_input", "request_user_input_async"}:
                        continue
                elif subtype not in {"function_call_output", "custom_tool_call_output"}:
                    continue
            else:
                continue
            metadata = {k: payload[k] for k in ("type", "turn_id", "call_id", "name") if k in payload}
            if subtype in {"turn_aborted", "task_failed"}:
                metadata["status_reason"] = failure_reason(payload.get("error"), subtype)
            records[position] = {"type": kind, "timestamp": event.get("timestamp"), "payload": metadata}
            if len(records) > CODEX_METADATA_LIMIT:
                break  # The caller rejects an over-budget journal as unknown.
        return records

    def _backfill(self, key: str, stream, state: dict, pending: dict):
        scan = self.backfills[key]
        cursor = scan["cursor"]
        # One older block per poll. Overlap recovers small lifecycle records
        # crossing block boundaries; the complete read remains within 8 MiB.
        overlap = min(65536, CODEX_SCAN_BYTES // 8)
        start = max(0, cursor - CODEX_SCAN_BYTES + overlap * 2)
        stream.seek(start)
        raw = stream.read(cursor - start + overlap)
        scan["events"].update(self._metadata(raw, start))
        scan["cursor"] = start
        if len(scan["events"]) > CODEX_METADATA_LIMIT:
            self.backfills.pop(key)
            return state, pending
        starts = [offset for offset, event in scan["events"].items()
                  if event["type"] == "event_msg" and event["payload"]["type"] == "task_started"]
        if starts:
            first = max(starts)
            rebuilt, requests = {"updated_at": state.get("updated_at", 0)}, {}
            for offset in sorted(scan["events"]):
                if offset >= first:
                    self._codex_event(rebuilt, requests, scan["events"][offset])
            self.backfills.pop(key)
            return rebuilt, requests
        if not start:
            self.backfills.pop(key)
        return state, pending

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
            cursor = len(raw)
            while cursor:
                marker = raw.rfind(b"task_started", 0, cursor)
                if marker < 0:
                    break
                line_start = raw.rfind(b"\n", 0, marker) + 1
                line_end = raw.find(b"\n", marker)
                cursor = max(0, line_start - 1)
                # Lifecycle metadata is small. Do not split/copy an entire
                # expanding multi-MiB window just to inspect this one record.
                if ((start and line_start == 0) or line_end < 0 or
                        line_end - line_start > 65536):
                    continue
                try:
                    event = json.loads(raw[line_start:line_end])
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
            if key in self.entries and self.entries[key][0] == signature and key not in self.backfills:
                return self.entries[key][1].copy()
            previous = self.entries.get(key)
            offset = self.offsets.get(key, 0)
            incremental = (app == "codex" and previous is not None
                           and previous[0][0] == stat.st_ino
                           and (previous[0][1] < stat.st_size or previous[0] == signature)
                           and 0 <= stat.st_size - offset <= CODEX_SCAN_BYTES)
            continuing_backfill = incremental and key in self.backfills
            if not incremental:
                self.backfills.pop(key, None)
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
                    self._codex_event(state, pending, event)
                else:
                    if isinstance(event.get("cwd"), str):
                        state["project"] = event["cwd"]
                    if kind in {"custom-title", "summary"}:
                        title = event.get("customTitle") or event.get("summary")
                        if title:
                            state["title"] = _text(title)
                    # Turn id is a structural field. Message text is never retained.
                    turn = hook_events.turn_id_of(event)
                    message = event.get("message")
                    stop = message.get("stop_reason") if isinstance(message, dict) else None
                    if kind == "assistant" and stop == "end_turn":
                        _claude_note(state, "completed", stamp, "end_turn", turn)
                    elif kind == "system" and event.get("subtype") == "turn_duration":
                        _claude_note(state, "completed", stamp, "turn_duration", turn)
                    elif kind == "user" or (kind == "assistant" and stop == "tool_use"):
                        # A user message alone is not evidence of a live model run.
                        _claude_note(state, "unknown", stamp, str(kind or ""), turn)
            if app == "codex":
                if not state.get("lifecycle_scan_incomplete"):
                    self.backfills.pop(key, None)
                elif key not in self.backfills and not incremental:
                    self.backfills[key] = {"cursor": start, "events": self._metadata(complete, start)}
                elif continuing_backfill:
                    self.backfills[key]["events"].update(self._metadata(complete, start, skip_partial=False))
                    with path.open("rb") as stream:
                        state, pending = self._backfill(key, stream, state, pending)
            if pending:
                pending_at = max(pending.values())
                if pending_at >= state.get("state_at", 0):
                    state.update(status="waiting", state_at=pending_at, event="request_user_input")
            if key not in self.entries and len(self.entries) >= MAX_SESSIONS * 3:
                oldest = next(iter(self.entries))
                self.entries.pop(oldest)
                self.offsets.pop(oldest, None)
                self.pending.pop(oldest, None)
                self.backfills.pop(oldest, None)
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


def _claude_note(state: dict, status: str, stamp: float, event_name: str, turn: str | None):
    """Record one Claude transcript fact. A record without a turn id must not keep an older one."""
    state.update(status=status, state_at=stamp, event=event_name)
    state.pop("user_stopped", None)
    if turn:
        state["turn_id"] = turn
    else:
        state.pop("turn_id", None)


def _when(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _turn_relation(left: str | None, right: str | None) -> str:
    if left and right:
        return "same" if left == right else "different"
    return "unknown"


def _claude_unknown(evidence: str) -> dict:
    return {"status": "unknown", "evidence": evidence, "user_stopped": False}


def _claude_from_descriptor(descriptor: dict) -> dict:
    raw = descriptor.get("raw") or descriptor.get("status") or ""
    return {"status": descriptor["status"],
            "evidence": "CLI 会话状态 " + str(raw) + "；PID 与启动时间匹配",
            "user_stopped": False}


def _claude_from_log(tail: dict) -> dict:
    event_name = str(tail.get("event") or "end_turn")
    status = tail.get("status") or "unknown"
    if status == "interrupted" and tail.get("user_stopped"):
        evidence = "日志记录用户主动停止"
    elif status == "interrupted":
        evidence = "日志 " + event_name + "；本轮已中断"
    else:
        evidence = "日志 " + event_name + "；本轮已结束"
    result = {"status": status, "evidence": evidence, "user_stopped": bool(tail.get("user_stopped"))}
    ended = _when(tail.get("state_at"))
    if ended and status in {"completed", "interrupted"}:
        result["ended_at"] = ended
    return result


def _claude_from_hook(hook: dict) -> dict:
    evidence = {
        "turn_started": "生命周期事件记录本轮已提交",
        "turn_completed": "生命周期事件记录本轮正常结束",
        "turn_failed": "生命周期事件记录本轮失败结束",
        "user_stopped": "生命周期事件记录用户主动停止",
        "permission_wait": "生命周期事件记录正在等待权限",
    }.get(hook.get("event"), "生命周期事件无法对应已知状态")
    result = {"status": hook.get("status") or "unknown", "evidence": evidence,
              "user_stopped": bool(hook.get("user_stopped"))}
    started, ended = _when(hook.get("started_at")), _when(hook.get("ended_at"))
    if started:
        result["started_at"] = started
    if ended:
        result["ended_at"] = ended
    return result


def _claude_descriptor_time(data: dict) -> float | None:
    """Explicit descriptor clocks only. File mtime is not a status time."""
    for key in ("updatedAt", "updated_at", "timestamp"):
        if key in data:
            stamp = _timestamp(data.get(key))
            if stamp > 0:
                return stamp
    return None


def _claude_resolve(descriptor: dict | None, tail: dict | None, hook: dict | None) -> dict:
    """A newer explicit end beats an older busy descriptor only for the same turn.

    If the turn cannot be verified, the result is unknown. Busy is not left in
    place, and the end is not treated as confirmed. No age threshold is an
    interruption: a busy descriptor without a newer terminal stays running.
    """
    desc = descriptor or {}
    log = tail or {}
    desc_status = desc.get("status")
    desc_at = _when(desc.get("at"))
    desc_turn = desc.get("turn_id") or None
    log_status = log.get("status")
    log_at = _when(log.get("state_at"))
    log_turn = log.get("turn_id") or None
    log_terminal = log_status in {"completed", "interrupted"}
    desc_live = desc_status in {"running", "waiting"}
    chosen = hook
    if chosen:
        hook_turn = chosen.get("turn_id") or None
        hook_at = _when(chosen.get("timestamp"))
        hook_status = chosen.get("status")
        desc_rel = _turn_relation(desc_turn, hook_turn)
        if desc_rel == "different":
            if hook_at and desc_at and hook_at > desc_at:
                pass
            elif desc_at and hook_at and desc_at > hook_at:
                chosen = None
            else:
                return _claude_unknown("生命周期事件与 CLI 描述符不是同一轮，当前状态待确认")
        if chosen and log_terminal and _turn_relation(log_turn, hook_turn) == "different":
            if log_at and hook_at and log_at > hook_at:
                return _claude_unknown("日志结束事件与生命周期事件不是同一轮，当前状态待确认")
            log_terminal = False
        if (chosen and desc_live and desc_rel == "unknown" and desc_at and hook_at and desc_at > hook_at):
            return _claude_unknown("CLI 描述符晚于生命周期事件且没有轮次，不能确认同一轮")
        if (chosen and log_terminal and log_turn and _turn_relation(log_turn, hook_turn) == "same"
                and log_at and hook_at and log_at > hook_at and hook_status in {"running", "waiting"}):
            if desc_live and desc_at and desc_at > log_at and desc_rel == "same":
                return _claude_from_descriptor(desc)
            if desc_live and desc_rel == "unknown" and desc_at and desc_at > log_at:
                return _claude_unknown("结束事件与较晚的 CLI 描述符无法确认同一轮")
            return _claude_from_log(log)
        if chosen:
            return _claude_from_hook(chosen)
    if desc_live and log_terminal:
        relation = _turn_relation(desc_turn, log_turn)
        if relation == "unknown":
            return _claude_unknown("较新结束事件与 busy 描述符无法确认同一轮，不能沿用运行状态")
        if relation == "different":
            if desc_at and log_at and log_at < desc_at:
                return _claude_from_descriptor(desc)
            return _claude_unknown("结束事件属于另一轮，不能确认当前轮")
        if desc_at and log_at and log_at > desc_at:
            return _claude_from_log(log)
        if desc_at and log_at and desc_at > log_at:
            return _claude_from_descriptor(desc)
        return _claude_unknown("同一轮结束事件与描述符缺少可比时间，不能沿用运行状态")
    if desc_status:
        return _claude_from_descriptor(desc)
    if log_terminal:
        return _claude_from_log(log)
    return _claude_unknown("缺少可核验的 CLI 运行状态")


def _codex_state_databases(root: Path) -> list[Path]:
    """state_N numbering is a migration counter; sort numerically so state_10
    outranks state_9, and never let an unreadable name ordering pick a stale one."""
    def order(path: Path):
        digits = "".join(ch if ch.isdigit() else "" for ch in path.stem)
        return (int(digits) if digits else -1, path.name)
    return sorted(root.glob("state_*.sqlite"), key=order, reverse=True)


class SessionCollector:
    def __init__(self, codex_home: Path | None = None, claude_home: Path | None = None,
                 lifecycle_path: Path | None = None):
        self.codex_home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.claude_home = claude_home or Path.home() / ".claude"
        self.lifecycle_path = lifecycle_path or hook_events.default_path()
        self.tails = TailCache()
        self.codex_rows: list[dict] = []
        self.last_errors: list[str] = []
        self._claude_paths: list[Path] = []
        self._claude_scan_at = float("-inf")

    def _read_threads_database(self, database: Path, thread: str | None = None) -> bool:
        """Read one state database; True when it yielded the session rows."""
        for attempt in range(2):
            try:
                with _readonly(database) as con:
                    return self._load_threads(con, thread)
            except sqlite3.OperationalError as error:
                lower = str(error).lower()
                transient = ("locked" in lower or "busy" in lower or
                             "unable to open database file" in lower or "cantopen" in lower)
                if attempt == 0 and transient:
                    # 忙写入方/WAL 残留：一次有界重试，避免整周期降级 unknown。
                    time.sleep(0.25)
                    continue
                self._last_db_error = type(error).__name__
                return False
            except (OSError, sqlite3.Error) as error:
                self._last_db_error = type(error).__name__
                return False
        return False

    def _load_threads(self, con, thread: str | None = None) -> bool:
        """One bounded read into codex_rows; transient locks re-raise for retry."""
        try:
            columns = {row[1] for row in con.execute("PRAGMA table_info(threads)")}
            if "id" not in columns:
                # 结构级差异（如 Codex 迁移数据位置）不是瞬态错误，
                # 必须在 health 里与「暂时不可读」区分开。
                self._last_db_error = "缺少 threads 表结构"
                return False
            requested = [c for c in ("id", "name", "title", "cwd", "rollout_path", "updated_at",
                                     "agent_path", "agent_nickname") if c in columns]
            # Delegated agents share their parent's desktop task. Filter before
            # LIMIT so background workers cannot evict visible conversations.
            main_tasks = (" AND (agent_path IS NULL OR agent_path='' OR agent_path='/root')"
                          if "agent_path" in columns else "")
            if "thread_source" in columns:
                main_tasks += " AND (thread_source IS NULL OR thread_source NOT IN ('subagent','guardian_review'))"
            target_filter = " AND id=?" if thread is not None else ""
            parameters = (thread, MAX_SESSIONS) if thread is not None else (MAX_SESSIONS,)
            rows = con.execute("SELECT " + ",".join(requested) +
                               " FROM threads WHERE archived=0" + main_tasks + target_filter +
                               " ORDER BY updated_at DESC LIMIT ?", parameters).fetchall()
            self.codex_rows = [dict(row) for row in rows]
            if thread is not None and len(self.codex_rows) == 1:
                # The visible header may export only a title. Check all main
                # tasks, including archived/outside the discovery LIMIT, before
                # allowing that fallback. Too many rows remain ambiguous.
                title_fields = [c for c in ("name", "title") if c in columns]
                titles = (con.execute("SELECT " + ",".join(title_fields) +
                          " FROM threads WHERE 1=1" + main_tasks + " LIMIT 2049").fetchall()
                          if title_fields else [])
                wanted = _text(self.codex_rows[0].get("name") or self.codex_rows[0].get("title"))
                self.codex_rows[0]["title_unique"] = bool(wanted) and len(titles) <= 2048 and sum(
                    _text(dict(title).get("name") or dict(title).get("title")) == wanted
                    for title in titles) == 1
            return True
        except (OSError, sqlite3.Error) as error:
            lower = str(error).lower()
            if isinstance(error, sqlite3.OperationalError) and (
                    "locked" in lower or "busy" in lower or
                    "unable to open database file" in lower or "cantopen" in lower):
                raise  # 瞬态锁竞争交给外层的第二次有界尝试。
            self._last_db_error = type(error).__name__
            return False

    def _codex_rollout_fallback(self, root) -> list[dict]:
        """Enumerate self-describing rollouts while state databases are unusable.

        Codex 迁移期间 threads 表可能暂时缺失或整体移走。rollout 首行
        session_meta 自描述（id/cwd/thread_source）：只取 thread_source=user
        的用户对话（subagent/guardian_review 是内部代理），id 去重、按 mtime
        取最近 7 天、上限 120 个。标题不在 rollout 内，用中性标签占位，
        真实时间线仍由 thread_turns 与尾部事件给出。
        """
        try:
            candidates = []
            # sessions/YYYY/MM/DD/<rollout>.jsonl：年/月/日三层目录。
            for path in (root / "sessions").glob("*/*/*/*.jsonl"):
                try:
                    if time.time() - path.stat().st_mtime <= 7 * 86400:
                        candidates.append(path)
                except OSError:
                    continue
        except OSError:
            return []
        candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        rows, seen = [], set()
        for path in candidates[:120]:
            try:
                with path.open("rb") as fh:
                    head = json.loads(fh.readline(65536))
            except (OSError, ValueError):
                continue
            payload = head.get("payload") if isinstance(head, dict) else None
            if not isinstance(payload, dict) or payload.get("thread_source") != "user":
                continue
            thread = payload.get("id")
            if not isinstance(thread, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", thread) or thread in seen:
                continue
            seen.add(thread)
            cwd = payload.get("cwd")
            rows.append({
                "id": thread, "name": "Codex 会话 " + thread[:8], "title": None,
                "cwd": cwd if isinstance(cwd, str) else "",
                "rollout_path": str(path), "updated_at": int(path.stat().st_mtime),
            })
        return rows

    def _codex(self, processes: dict[int, dict], thread: str | None = None) -> list[dict]:
        root = self.codex_home
        databases = _codex_state_databases(root)
        available = True
        self._last_db_error = None
        opened = False
        # Use the newest database that still exposes the projected threads
        # table; a just-migrated or partial state file must not hide sessions.
        # On total failure codex_rows keeps the previous collection for degrade.
        for database in databases[:3]:
            if self._read_threads_database(database, thread):
                opened = True
                break
        if not opened:
            # 结构迁移期（threads 表缺失/整体移走）枚举 rollout 兜底；
            # 兜底也为空才降级为上一轮缓存并报错。
            # A sender must bind the current exact database row, never fall
            # back to an old title or a cached discovery snapshot.
            fallback_rows = self._codex_rollout_fallback(root) if thread is None else []
            if fallback_rows:
                self.codex_rows = fallback_rows
            else:
                available = False
                cause = getattr(self, "_last_db_error", None)
                self.last_errors.append("Codex 会话数据库暂时不可读" + ("（" + cause + "）" if cause else ""))
                if thread is not None:
                    return []
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
                # 有存活进程且日志尾有明确生命周期的行不整体降级：
                # completed/interrupted 是显式终态证据；running 无法确认活性才降级。
                if state == "running":
                    state, evidence = "unknown", "会话数据库暂时不可读；运行状态待确认"
                elif state not in {"completed", "interrupted"}:
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
            if thread is not None:
                item["lifecycle_pending"] = state == "unknown" and str(path) in self.tails.backfills
                item["title_unique"] = row.get("title_unique", False)
            result.append(item)
        return result

    def collect_session(self, session_id: str, *, max_reads: int = 16, budget: float = 2.0) -> dict | None:
        """Fresh exact-session preflight, with bounded metadata-only backfill.

        Discovery advances one block per poll; a short-lived sender otherwise
        always starts cold and can never reach a long round's start. Reuse only
        this target's TailCache, rereading its DB projection on every pass.
        """
        if not re.fullmatch(r"codex:[A-Za-z0-9_-]{8,128}", session_id):
            return None
        self.last_errors = []
        processes = _processes()
        deadline = time.monotonic() + max(0, min(budget, 2.0))
        row = None
        for _ in range(max(1, min(max_reads, 16))):
            rows = self._codex(processes, session_id.removeprefix("codex:"))
            row = rows[0] if len(rows) == 1 else None
            if not row or not row.get("lifecycle_pending") or time.monotonic() >= deadline:
                break
        return row

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
        try:
            hooks = hook_events.load(self.lifecycle_path)
        except OSError:
            hooks = {}
        result = []
        for sid in ordered[:MAX_SESSIONS]:
            data = records.get(sid, {})
            tail = self.tails.read(recent[sid], "claude") if sid in recent else {}
            raw_status = str(data.get("status", "")) if data else ""
            mapped = {"idle": "idle", "busy": "running", "working": "running",
                      "running": "running", "thinking": "running", "waiting": "waiting",
                      "waiting_for_input": "waiting", "waiting_for_permission": "waiting",
                      "needs_attention": "waiting"}.get(raw_status)
            descriptor = None
            if data and mapped:
                descriptor = {"status": mapped, "at": _claude_descriptor_time(data),
                              "turn_id": hook_events.turn_id_of(data), "raw": raw_status}
            hook = hook_events.session_state(hooks, sid)
            decision = _claude_resolve(descriptor, tail, hook)
            status, evidence = decision["status"], decision["evidence"]
            project = data.get("cwd") or tail.get("project") or ""
            if not project and hook:
                project = hook.get("cwd") or ""
            title = tail.get("title") or data.get("name") or (Path(project).name if project else "Claude 会话")
            updated = [_timestamp(data.get("updatedAt")) if data else 0, tail.get("updated_at", 0) or 0]
            if hook and hook.get("timestamp"):
                updated.append(hook["timestamp"])
            item = dict(id="claude:" + sid, app_id="claude-code", app_name="Claude Code", title=_text(title),
                        project=project, status=status, evidence=evidence,
                        updated_at=max(updated), source="local-session", target="")
            item["status_reason"] = evidence
            if decision.get("started_at"):
                item["started_at"] = decision["started_at"]
                item["timing_basis"] = "turn"
                item["timing_reason"] = "按 Claude 生命周期事件的本轮提交时间计算"
            else:
                item["timing_reason"] = "CLI 进程开始时间不等于本轮开始时间；当前状态数据未提供本轮起点"
            if decision.get("ended_at"):
                item["ended_at"] = decision["ended_at"]
            if decision.get("user_stopped"):
                item["user_stopped"] = True
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


def collect_session(session_id: str) -> dict | None:
    return _collector.collect_session(session_id)


if __name__ == "__main__":
    rows = collect()
    print(json.dumps({"count": len(rows), "sessions": rows, "diagnostics": _collector.last_errors},
                     ensure_ascii=False, indent=2))
