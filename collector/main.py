#!/usr/bin/python3
"""Local-only bounded collectors. Emits one complete JSON snapshot per line."""
import argparse
import json
import os
import time
import sys
import math
from concurrent.futures import ThreadPoolExecutor, wait

import codex_adapter
import desktop_adapters
import autoclaw_adapter
import extended_adapters
import cline_adapter
import openclaw_adapter


# Leave most of the ten-second recovery budget to verified navigation/input.
# Adapters reuse their read-only caches; a slow scan never overlaps the next one.
POLL_INTERVAL_SECONDS = 1.0
WATCH_COLLECT_BUDGET_SECONDS = 0.8
ADAPTER_STALL_SECONDS = 45.0
WATCH_RESTART_EXIT_CODE = 75


def _adapters():
    return [("Codex / Claude", codex_adapter), ("ZCode / WorkBuddy", desktop_adapters),
            ("AutoClaw", autoclaw_adapter), ("Qoder / Grok", extended_adapters),
            ("Cline", cline_adapter), ("OpenClaw", openclaw_adapter)]


def _collect_adapter(name, module):
    rows = [row.copy() if isinstance(row, dict) else row for row in module.collect()]
    # Copy diagnostics on the same worker, before another scan can reset them.
    try:
        errors = (list(module._collector.last_errors) if module is codex_adapter else
                  list(module.diagnostics()) if hasattr(module, "diagnostics") else [])
    except Exception as exc:
        errors = [name + " 诊断失败 (" + type(exc).__name__ + ")"]
    return rows, errors


def _assembled(rows, errors):
    unique = {}
    invalid = 0
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        row = row.copy()
        # A damaged external row must not poison Swift's entire JSON snapshot.
        for key in ("target", "project"):
            if row.get(key) is None:
                row[key] = ""
        strings = ("id", "app_id", "app_name", "title", "project", "status", "evidence", "source", "target")
        numbers = ("updated_at", "started_at", "ended_at", "last_activity_at", "care_retry_at")
        if any(not isinstance(row.get(k), str) for k in strings) or any(
            (k == "updated_at" or row.get(k) is not None) and
            (isinstance(row.get(k), bool) or not isinstance(row.get(k), (int, float)) or not math.isfinite(row[k]))
            for k in numbers):
            invalid += 1
            continue
        optional_strings = ("timing_basis", "status_reason", "timing_reason", "navigation_title", "navigation_key", "lifecycle_revision", "run_id")
        for key in optional_strings:
            if row.get(key) is not None and not isinstance(row[key], str):
                row.pop(key)
        for key in ("pid", "window_id"):
            if row.get(key) is not None and (isinstance(row[key], bool) or not isinstance(row[key], int)):
                row.pop(key)
        if row.get("user_stopped") is not None and not isinstance(row["user_stopped"], bool):
            # A malformed stop flag must never silently regain send authority.
            row.update(status="unknown", user_stopped=True, status_reason="会话停止标记异常，保持待确认")
        if row.get("manual_only") is not None and not isinstance(row["manual_only"], bool):
            row.update(status="unknown", manual_only=True, status_reason="会话操作范围异常，保持待确认")
        if row.get("continuous_authorized") is not None and not isinstance(row["continuous_authorized"], bool):
            row.update(status="unknown", continuous_authorized=False, status_reason="持续关照标记异常，保持待确认")
        unique[row["id"]] = row
    diagnostics = list(errors)
    if invalid:
        diagnostics.append("已隔离 " + str(invalid) + " 条格式异常会话，其他会话继续更新")
    return {"sessions": list(unique.values()), "errors": diagnostics, "collected_at": time.time()}


def snapshot():
    """A one-shot probe waits for the complete, bounded adapter results."""
    rows, errors = [], []
    adapters = _adapters()
    with ThreadPoolExecutor(max_workers=len(adapters)) as executor:
        futures = [(name, executor.submit(_collect_adapter, name, module))
                   for name, module in adapters]
        for name, future in futures:
            try:
                result, diagnostics = future.result()
                rows.extend(result)
                errors.extend(diagnostics)
            except Exception as exc:
                errors.append(name + " 采集失败 (" + type(exc).__name__ + ")")
    return _assembled(rows, errors)


class WatchCollector:
    """Publish healthy sources while a slow source finishes one existing scan.

    There is at most one in-flight collection per adapter. A late or failed
    source keeps its last session identity but never keeps a trusted status.
    """
    def __init__(self, adapters=None):
        self.adapters = _adapters() if adapters is None else adapters
        self.executor = ThreadPoolExecutor(max_workers=len(self.adapters))
        self.pending = {}
        self.pending_since = {}
        self.last_rows = {}
        self.last_errors = {}
        self.stalled_sources = []

    @staticmethod
    def _unconfirmed(rows, reason):
        result = []
        for row in rows:
            if isinstance(row, dict):
                row = row.copy()
                row.update(status="unknown", status_reason=reason,
                           evidence=reason + "；" + row.get("evidence", ""))
            result.append(row)
        return result

    def snapshot(self, budget=WATCH_COLLECT_BUDGET_SECONDS):
        self.stalled_sources = []
        for name, module in self.adapters:
            if name not in self.pending:
                self.pending[name] = self.executor.submit(_collect_adapter, name, module)
                self.pending_since[name] = time.monotonic()
        wait(self.pending.values(), timeout=budget)
        rows, errors = [], []
        now = time.monotonic()
        for name, _ in self.adapters:
            future = self.pending[name]
            if not future.done():
                reason = name + " 采集尚未完成，当前状态待确认"
                if now - self.pending_since[name] >= ADAPTER_STALL_SECONDS:
                    self.stalled_sources.append(name)
                    reason = name + " 采集持续无响应，当前状态待确认；正在重启本地采集器"
                rows.extend(self._unconfirmed(self.last_rows.get(name, []), reason))
                errors.extend(self.last_errors.get(name, []))
                errors.append(reason)
                continue
            self.pending.pop(name)
            self.pending_since.pop(name)
            try:
                result, diagnostics = future.result()
                self.last_rows[name] = result
                self.last_errors[name] = diagnostics
                rows.extend(result)
                errors.extend(diagnostics)
            except Exception as exc:
                reason = name + " 采集失败 (" + type(exc).__name__ + ")"
                self.last_errors[name] = [reason]
                rows.extend(self._unconfirmed(self.last_rows.get(name, []), reason))
                errors.append(reason)
        return _assembled(rows, errors)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    watcher = WatchCollector() if args.watch else None
    try:
        while True:
            started = time.monotonic()
            print(json.dumps(watcher.snapshot() if watcher else snapshot(), ensure_ascii=False), flush=True)
            if watcher and watcher.stalled_sources:
                # A stuck Python worker cannot be cancelled or joined. Exit
                # only this app-owned, read-only collector after its final
                # unknown frame; the native process watchdog relaunches it.
                os._exit(WATCH_RESTART_EXIT_CODE)
            if not args.watch:
                break
            time.sleep(max(0.1, POLL_INTERVAL_SECONDS - (time.monotonic() - started)))
    finally:
        if watcher:
            watcher.close()


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
