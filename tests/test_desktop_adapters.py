import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from collector.desktop_adapters import DesktopCollector, SESSION_LIMIT, _epoch


NOW = 1_790_073_990.0


class DesktopAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.processes = {42: "/Applications/WorkBuddy.app/Contents/MacOS/Electron",
                          43: "/Applications/WorkBuddy AI.app/Contents/MacOS/Electron",
                          44: "/Applications/ZCode.app/Contents/MacOS/ZCode"}
        self.reader = DesktopCollector(self.home, lambda: NOW, lambda: self.processes)

    def tearDown(self):
        self.temp.cleanup()

    def workbuddy(self, rows, edition=".workbuddy"):
        root = self.home / edition
        root.mkdir(parents=True, exist_ok=True)
        path = root / "workbuddy.db"
        with sqlite3.connect(path) as connection:
            connection.execute("""CREATE TABLE sessions (
                id TEXT, cwd TEXT, title TEXT, custom_title TEXT, status TEXT,
                updated_at INTEGER, last_activity_at INTEGER, deleted_at INTEGER)""")
            for session_id, status, timestamp, deleted in rows:
                connection.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                                   (session_id, "/workspace/demo", "original", "custom", status,
                                    timestamp * 1000, timestamp * 1000, deleted))
        return root

    def zcode(self, rows):
        root = self.home / ".zcode/v2"
        root.mkdir(parents=True)
        with sqlite3.connect(root / "tasks-index.sqlite") as connection:
            connection.execute("""CREATE TABLE tasks (workspace_key TEXT, workspace_path TEXT,
                task_id TEXT, title TEXT, task_status TEXT, updated_at INTEGER,
                deleted INTEGER, archived INTEGER)""")
            connection.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)", rows)

    def zcode_turns(self, rows, directory="/w", model_events=(), tool_events=()):
        root = self.home / ".zcode/cli/db"
        root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(root / "db.sqlite") as connection:
            connection.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT)")
            connection.execute("""CREATE TABLE turn_usage (session_id TEXT, turn_id TEXT, status TEXT,
                started_at INTEGER, completed_at INTEGER, cancelled_by_user INTEGER,
                context_exceeded INTEGER, error_type TEXT, error_code TEXT)""")
            connection.executemany("INSERT INTO session VALUES (?,?)", [(sid, directory) for sid in {r[0] for r in rows}])
            connection.executemany("INSERT INTO turn_usage VALUES (?,?,?,?,?,?,?,?,?)", rows)
            for table, events in (("model_usage", model_events), ("tool_usage", tool_events)):
                connection.execute("CREATE TABLE " + table + " (session_id TEXT, turn_id TEXT, started_at INTEGER, completed_at INTEGER)")
                connection.executemany("INSERT INTO " + table + " VALUES (?,?,?,?)", events)

    def test_working_requires_live_process_and_fresh_evidence(self):
        self.workbuddy([("active", "working", NOW - 1, None),
                        ("stale", "working", NOW - 3600, None),
                        ("done", "completed", NOW - 3600, None),
                        ("error", "error", NOW, None)])
        result = {row["id"]: row for row in self.reader.collect()}
        self.assertEqual(result["workbuddy:active"]["status"], "running")
        self.assertEqual(result["workbuddy:stale"]["status"], "unknown")
        self.assertEqual(result["workbuddy:done"]["status"], "completed")
        self.assertEqual(result["workbuddy:error"]["status"], "interrupted")
        stopped = DesktopCollector(self.home, lambda: NOW, lambda: {})
        self.assertEqual({r["id"]: r for r in stopped.collect()}["workbuddy:active"]["status"], "unknown")

    def test_real_transcript_event_can_refresh_stale_index(self):
        root = self.workbuddy([("s1", "working", NOW - 3600, None)])
        project = root / "projects/workspace-demo"
        project.mkdir(parents=True)
        transcript = project / "s1.jsonl"
        transcript.write_text(json.dumps({"sessionId": "s1", "type": "function_call",
                                          "timestamp": NOW * 1000, "arguments": "never returned"}) + "\n")
        result = self.reader.collect()[0]
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["updated_at"], NOW)
        self.assertNotIn("never returned", str(result))
        # Touching/copying an old event does not make the event recent.
        transcript.write_text(json.dumps({"sessionId": "s1", "type": "function_call",
                                          "timestamp": (NOW - 3600) * 1000}) + "\n")
        self.assertEqual(self.reader.collect()[0]["status"], "unknown")

    def test_runtime_pid_must_belong_to_correct_edition(self):
        root = self.workbuddy([("s1", "working", NOW - 3600, None)])
        runtime = root / "sessions"
        runtime.mkdir()
        file = runtime / "43.json"
        file.write_text(json.dumps({"pid": 43, "sessionId": "s1", "lastHeartbeat": NOW * 1000}))
        self.assertEqual(self.reader.collect()[0]["status"], "unknown")
        file.write_text(json.dumps({"pid": 42, "sessionId": "s1", "lastHeartbeat": NOW * 1000,
                                    "endpoint": "sensitive endpoint not retained"}))
        row = self.reader.collect()[0]
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["pid"], 42)
        self.assertNotIn("endpoint", str(self.reader._file_cache))

    def test_zcode_compound_ids_and_deleted_archive_filters(self):
        self.zcode([
            ("a", "/a", "same", "A", "completed", NOW * 1000, 0, 0),
            ("b", "/b", "same", "B", "running", NOW * 1000, 0, 0),
            ("c", "/c", "deleted", "C", "completed", NOW * 1000, 1, 0),
            ("d", "/d", "archived", "D", "completed", NOW * 1000, 0, 1),
        ])
        rows = self.reader.collect()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row["id"] for row in rows}), 2)
        self.assertEqual({row["status"] for row in rows}, {"running", "completed"})
        self.assertEqual({row["target"] for row in rows},
                         {"zcode://workspace/open?path=%2Fa", "zcode://workspace/open?path=%2Fb"})

    def test_zcode_workspace_link_needs_safe_absolute_path(self):
        self.zcode([
            ("cn", "/Users/example/中文项目", "cn", "中文工作区", "completed", NOW * 1000, 0, 0),
            ("relative", "Downloads/relative", "rel", "相对路径", "completed", NOW * 1000, 0, 0),
            ("traversal", "/a/../secret", "trav", "穿越", "completed", NOW * 1000, 0, 0),
            ("empty", "", "void", "无工作区", "completed", NOW * 1000, 0, 0),
        ])
        targets = {row["id"]: row["target"] for row in self.reader.collect()}
        self.assertEqual(targets["zcode:" + hashlib.sha256(b"cn").hexdigest()[:12] + ":cn"],
                         "zcode://workspace/open?path=%2FUsers%2Fexample%2F"
                         "%E4%B8%AD%E6%96%87%E9%A1%B9%E7%9B%AE")
        self.assertEqual(targets["zcode:" + hashlib.sha256(b"relative").hexdigest()[:12] + ":rel"], "")
        self.assertEqual(targets["zcode:" + hashlib.sha256(b"traversal").hexdigest()[:12] + ":trav"], "")
        self.assertEqual(targets["zcode:" + hashlib.sha256(b"empty").hexdigest()[:12] + ":void"], "")


    def test_runtime_heartbeat_scan_survives_vanishing_file(self):
        root = self.workbuddy([("s1", "working", NOW - 1, None)])
        runtime = root / "sessions"
        runtime.mkdir()
        (runtime / "42.json").write_text(json.dumps({"pid": 42, "sessionId": "s1",
                                                     "lastHeartbeat": NOW * 1000}))
        dangling = runtime / "ghost.json"
        dangling.symlink_to(runtime / "missing.json")   # 列出后 stat 必失败
        row = self.reader.collect()[0]
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["pid"], 42)
        self.assertEqual(self.reader.last_errors, [])

    def test_zcode_running_survives_long_quiet_stretch_while_app_live(self):
        self.processes = {44: "/Applications/ZCode.app/Contents/ZCode"}
        self.zcode([("a", "/a", "long", "L", "running", (NOW - 1800) * 1000, 0, 0)])
        live = DesktopCollector(self.home, lambda: NOW, lambda: self.processes)
        self.assertEqual(live.collect()[0]["status"], "running")
        dead = DesktopCollector(self.home, lambda: NOW, lambda: {})
        self.assertEqual(dead.collect()[0]["status"], "unknown")

    def iso(self, stamp):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def rollout(self, task_id, records):
        directory = self.home / ".zcode/cli/rollout"
        directory.mkdir(parents=True, exist_ok=True)
        lines = []
        for turn_id, started, completed in records:
            row = {"type": "model_io", "sessionId": task_id, "turnId": turn_id,
                   "startedAt": self.iso(started)}
            if completed:
                row["completedAt"] = self.iso(completed)
            # 真实 rollout 是紧凑 JSON（无空格），正则按字节匹配。
            lines.append(json.dumps(row, separators=(",", ":")))
        (directory / ("model-io-" + task_id + ".jsonl")).write_text("\n".join(lines) + "\n")

    def zcode_log(self, records):
        directory = self.home / ".zcode/cli/log"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "zcode-2026-09-27.jsonl"
        with path.open("a") as stream:
            for sid, turn, event, stamp in records:
                stream.write(json.dumps({"sessionId": sid, "turnId": turn,
                                         "event": event, "timestamp": self.iso(stamp),
                                         "message": "private conversation must not be retained"}) + "\n")
        return path

    def test_zcode_new_log_turn_supersedes_stale_completed_ledger(self):
        self.zcode([("wk", "/w", "sess_a1", "新轮执行中", "completed", (NOW - 2400) * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "old", "completed", (NOW - 3000) * 1000,
                           (NOW - 2400) * 1000, 0, 0, None, None)])
        self.zcode_log([("sess_a1", "old", "turn.completed", NOW - 2400),
                        ("sess_a1", "new", "turn.started", NOW - 120),
                        ("sess_a1", "new", "tool.call.started", NOW - 4)])
        row = self.zcode_row()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["started_at"], NOW - 120)
        self.assertEqual(row["last_activity_at"], NOW - 4)
        self.assertNotIn("private conversation", str(self.reader._zcode_log_cache))
        self.zcode_log([("sess_a1", "new", "turn.completed", NOW - 1)])
        self.assertEqual(self.zcode_row()["status"], "completed")

    def test_zcode_same_log_and_ledger_turn_keeps_start_through_failure(self):
        db_start = NOW - 120
        self.zcode([("wk", "/w", "sess_a1", "同一轮", "completed", (NOW - 2400) * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "turn1", "running", int(db_start * 1000),
                           None, 0, 0, None, None)],
                         model_events=[("sess_a1", "turn1", int((NOW - 4) * 1000), None)])
        self.zcode_log([("sess_a1", "turn1", "turn.started", db_start + 0.017),
                        ("sess_a1", "turn1", "model.request.started", NOW - 4)])
        running = self.zcode_row()
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["started_at"], db_start)
        with sqlite3.connect(self.home / ".zcode/cli/db/db.sqlite") as connection:
            connection.execute("UPDATE turn_usage SET status='error', completed_at=? WHERE turn_id='turn1'",
                               (int(NOW * 1000),))
        with sqlite3.connect(self.home / ".zcode/v2/tasks-index.sqlite") as connection:
            connection.execute("UPDATE tasks SET task_status='error', updated_at=? WHERE task_id='sess_a1'",
                               (int(NOW * 1000),))
        failed = self.zcode_row()
        self.assertEqual(failed["status"], "interrupted")
        self.assertEqual(failed["started_at"], running["started_at"])

    def test_zcode_new_log_turn_expires_and_failed_turn_is_not_auto_recoverable(self):
        self.zcode([("wk", "/w", "sess_a1", "新轮待确认", "completed", (NOW - 2400) * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "old", "completed", (NOW - 3000) * 1000,
                           (NOW - 2400) * 1000, 0, 0, None, None)])
        self.zcode_log([("sess_a1", "new", "turn.started", NOW - 900)])
        self.assertEqual(self.zcode_row()["status"], "unknown")
        self.zcode_log([("sess_a1", "new", "turn.failed", NOW - 1)])
        self.assertEqual(self.zcode_row()["status"], "unknown")

    def test_zcode_log_terminal_after_midnight_closes_previous_day_turn(self):
        self.zcode([("wk", "/w", "sess_a1", "跨日任务", "completed", (NOW - 2400) * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "old", "completed", (NOW - 3000) * 1000,
                           (NOW - 2400) * 1000, 0, 0, None, None)])
        self.zcode_log([("sess_a1", "new", "turn.started", NOW - 120)])
        next_day = self.home / ".zcode/cli/log/zcode-2026-09-28.jsonl"
        next_day.write_text(json.dumps({"sessionId": "sess_a1", "turnId": "new",
                                        "event": "turn.completed", "timestamp": self.iso(NOW - 1)}) + "\n")
        self.assertEqual(self.zcode_row()["status"], "completed")

    def zcode_row(self):
        rows = {r["id"]: r for r in self.reader.collect() if r["app_id"] == "zcode"}
        return next(iter(rows.values())) if rows else None

    def test_zcode_active_turn_overrides_lagging_completed_index(self):
        # 索引只在轮次结束改写：上一轮 01:11 完成、新轮次 01:39 已在运行，
        # 索引仍是 completed。会话日志的在飞请求才是运行中的证据。
        self.processes = {44: "/Applications/ZCode.app/Contents/ZCode"}
        self.zcode([("wk", "/w", "sess_a1", "运行中的对话", "completed", (NOW - 2400) * 1000, 0, 0)])
        self.rollout("sess_a1", [("turnA", NOW - 3000, NOW - 2400),
                            ("turnB", NOW - 30, None)])
        row = self.zcode_row()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["started_at"], NOW - 30)
        self.assertEqual(row["timing_basis"], "turn")
        self.assertIn("会话日志", row["evidence"])
        self.assertNotIn("未提供开始时间", row["timing_reason"])

    def test_zcode_between_requests_turn_stays_running_until_index_confirms_end(self):
        self.processes = {44: "/Applications/ZCode.app/Contents/ZCode"}
        self.zcode([("wk", "/w", "sess_a1", "工具执行中", "completed", (NOW - 2400) * 1000, 0, 0)])
        self.rollout("sess_a1", [("turnA", NOW - 3000, NOW - 2400),
                            ("turnB", NOW - 120, NOW - 90)])
        row = self.zcode_row()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["started_at"], NOW - 120)

    def test_zcode_finished_turn_falls_back_to_index_ledger(self):
        self.processes = {44: "/Applications/ZCode.app/Contents/ZCode"}
        self.zcode([("wk", "/w", "sess_a1", "已结束", "completed", (NOW - 1) * 1000, 0, 0)])
        self.rollout("sess_a1", [("turnA", NOW - 400, NOW - 300)])
        row = self.zcode_row()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["started_at"], NOW - 400)
        self.assertEqual(row["timing_basis"], "turn")

    def test_zcode_stale_inflight_request_is_not_running_forever(self):
        self.processes = {44: "/Applications/ZCode.app/Contents/ZCode"}
        self.zcode([("wk", "/w", "sess_a1", "陈旧在飞", "completed", (NOW - 4000) * 1000, 0, 0)])
        self.rollout("sess_a1", [("turnA", NOW - 2000, None)])
        row = self.zcode_row()
        # 新轮陈旧在飞也不能冒充旧轮 completed；真实起点仍用于计时展示。
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["started_at"], NOW - 2000)

    def test_zcode_index_error_with_real_turn_start_is_recoverable_timing(self):
        self.processes = {44: "/Applications/ZCode.app/Contents/ZCode"}
        self.zcode([("wk", "/w", "sess_a1", "出错的会话", "error", (NOW - 20) * 1000, 0, 0)])
        self.rollout("sess_a1", [("turnA", NOW - 96, NOW - 20)])
        row = self.zcode_row()
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["started_at"], NOW - 96)
        self.assertEqual(row["timing_basis"], "turn")

    def test_zcode_without_rollout_keeps_previous_behavior(self):
        self.processes = {44: "/Applications/ZCode.app/Contents/ZCode"}
        self.zcode([("wk", "/w", "sess_a1", "无日志", "completed", (NOW - 1) * 1000, 0, 0)])
        row = self.zcode_row()
        self.assertEqual(row["status"], "completed")
        self.assertNotIn("started_at", row)
        self.assertIn("没有本轮开始时间", row["timing_reason"])

    def test_zcode_real_turn_ledger_running_beats_completed_task_index(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "completed", (NOW - 2000) * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "turn1", "running", (NOW - 120) * 1000, None, 0, 0, None, None)])
        row = self.zcode_row()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["started_at"], NOW - 120)
        self.assertNotIn("ended_at", row)
        self.assertIn("轮次数据库", row["timing_reason"])

    def test_zcode_resumed_old_task_is_not_lost_after_recent_index_limit(self):
        self.zcode([("wk", "/w", "recent" + str(i), "T", "completed", (NOW - i) * 1000, 0, 0)
                    for i in range(50)] +
                   [("wk", "/w", "sess_old", "Resumed", "completed", (NOW - 90000) * 1000, 0, 0)])
        self.zcode_turns([("sess_old", "turn1", "running", (NOW - 3000) * 1000, None, 0, 0, None, None)],
                         tool_events=[("sess_old", "turn1", (NOW - 40) * 1000, None)])
        rows = self.reader.collect()
        self.assertEqual(len(rows), SESSION_LIMIT)
        running = [row for row in rows if row["id"].endswith(":sess_old")]
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["status"], "running")

    def test_zcode_recent_tool_lifecycle_preserves_long_running_turn(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "completed", (NOW - 3000) * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "turn1", "running", (NOW - 2400) * 1000, None, 0, 0, None, None)],
                         tool_events=[("sess_a1", "turn1", (NOW - 80) * 1000, (NOW - 3) * 1000)])
        self.assertEqual(self.zcode_row()["status"], "running")
        self.reader.clock = lambda: NOW + 610
        self.assertEqual(self.zcode_row()["status"], "unknown")
        self.reader.clock = lambda: NOW
        self.processes.clear()
        self.assertEqual(self.zcode_row()["status"], "unknown")

    def test_zcode_terminal_ledger_provides_exact_start_end_and_ignores_model_tail(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "running", (NOW - 20) * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "old", "running", (NOW - 4000) * 1000, None, 0, 0, None, None),
                          ("sess_a1", "new", "completed", (NOW - 120) * 1000, (NOW - 10) * 1000, 0, 0, None, None)])
        self.rollout("sess_a1", [("new", NOW - 15, None)])
        row = self.zcode_row()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["started_at"], NOW - 120)
        self.assertEqual(row["ended_at"], NOW - 10)

    def test_zcode_lifecycle_refuses_same_session_id_from_wrong_workspace(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "completed", NOW * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "turn1", "running", (NOW - 120) * 1000, None, 0, 0, None, None)], directory="/another")
        self.rollout("sess_a1", [("turn1", NOW - 10, None)])
        row = self.zcode_row()
        self.assertEqual(row["status"], "unknown")
        self.assertIn("工作区不一致", row["status_reason"])
        self.assertNotIn("started_at", row)

    def test_zcode_terminal_round_does_not_depend_on_usage_tables(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "completed", (NOW - 10) * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "t1", "completed", (NOW - 120) * 1000,
                          (NOW - 10) * 1000, 0, 0, None, None)])
        # An incompatible auxiliary schema must not hide an authoritative end.
        with sqlite3.connect(self.home / ".zcode/cli/db/db.sqlite") as connection:
            connection.execute("DROP TABLE model_usage")
            connection.execute("CREATE TABLE model_usage (changed_schema TEXT)")
        row = self.zcode_row()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["ended_at"], NOW - 10)
        self.assertEqual(self.reader.last_errors, [])

    def test_zcode_terminal_activity_cannot_move_past_authoritative_end(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "completed", (NOW - 10) * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "t1", "completed", (NOW - 120) * 1000,
                          (NOW - 10) * 1000, 0, 0, None, None)],
                         tool_events=[("sess_a1", "t1", NOW * 1000, None)])
        row = self.zcode_row()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["last_activity_at"], NOW - 10)

    def test_zcode_budget_timeout_is_explained_without_claiming_completion(self):
        self.zcode_turns([])
        with patch("collector.desktop_adapters.sqlite3.connect",
                   side_effect=sqlite3.OperationalError("interrupted")):
            self.assertEqual(self.reader._zcode_turns([]), {})
        self.assertIn("读取超过 1 秒预算", self.reader.last_errors[0])
        self.assertIn("当前状态待确认", self.reader.last_errors[0])

    def test_zcode_explicit_stop_and_rate_limit_are_not_plain_failures(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "completed", (NOW - 20) * 1000, 0, 0),
                    ("wk", "/w", "sess_a2", "U", "completed", (NOW - 20) * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "t1", "cancelled", (NOW - 120) * 1000, (NOW - 10) * 1000, 1, 0, None, None),
                          ("sess_a2", "t2", "error", (NOW - 120) * 1000, (NOW - 10) * 1000, 0, 0, "APIError", "429")])
        rows = {row["id"].split(":")[-1]: row for row in self.reader.collect()}
        self.assertTrue(rows["sess_a1"]["user_stopped"])
        self.assertIn("用户主动", rows["sess_a1"]["status_reason"])
        self.assertIn("429", rows["sess_a2"]["status_reason"])
        self.assertEqual(rows["sess_a2"]["status"], "interrupted")

    def test_zcode_final_429_log_classifies_unknown_database_error(self):
        self.zcode([("wk", "/w", "sess_a1", "限流轮次", "error", NOW * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "turn1", "error", (NOW - 120) * 1000,
                           NOW * 1000, 0, 0, "unknown_error", "UNKNOWN_ERROR")])
        self.zcode_log([("sess_a1", "turn1", "turn.started", NOW - 120)])
        log = self.home / ".zcode/cli/log/zcode-2026-09-27.jsonl"
        with log.open("a") as stream:
            for event, stamp, context in [
                ("model.request.failed", NOW - 1, {"statusCode": 429, "statusMessage": "private 1302"}),
                ("turn.failed", NOW, {}),
            ]:
                stream.write(json.dumps({"sessionId": "sess_a1", "turnId": "turn1",
                                         "event": event, "timestamp": self.iso(stamp),
                                         "context": context}) + "\n")
        row = self.zcode_row()
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(row["status_reason"], "使用频率或配额超限（HTTP 429）")
        self.assertNotIn("private", str(self.reader._zcode_log_cache))

    def test_zcode_lifecycle_database_failure_degrades_current_status(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "completed", NOW * 1000, 0, 0)])
        root = self.home / ".zcode/cli/db"
        root.mkdir(parents=True)
        (root / "db.sqlite").write_bytes(b"not a database")
        self.assertEqual(self.zcode_row()["status"], "unknown")
        self.assertIn("轮次数据库读取失败", self.reader.last_errors[0])

    def test_zcode_new_index_running_cannot_be_overridden_by_old_completed_turn(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "running", NOW * 1000, 0, 0)])
        self.zcode_turns([("sess_a1", "old", "completed", (NOW - 120) * 1000, (NOW - 100) * 1000, 0, 0, None, None)])
        self.assertEqual(self.zcode_row()["status"], "running")

    def test_zcode_large_completed_line_is_not_inflight_and_nested_metadata_is_ignored(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "completed", (NOW - 1) * 1000, 0, 0)])
        self.rollout("sess_a1", [("oldA", NOW - 3000, NOW - 2990), ("oldB", NOW - 2000, NOW - 1990)])
        path = self.home / ".zcode/cli/rollout/model-io-sess_a1.jsonl"
        with path.open("a") as stream:
            stream.write(json.dumps({"completedAt": self.iso(NOW - 10),
                "request": {"turnId": "spoof", "startedAt": self.iso(NOW), "text": "x" * 600000},
                "type": "model_io", "sessionId": "sess_a1", "turnId": "real",
                "startedAt": self.iso(NOW - 60)}, separators=(",", ":")) + "\n")
        row = self.zcode_row()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["started_at"], NOW - 60)
        self.assertNotIn("spoof", str(self.reader._file_cache))

    def test_zcode_fallback_cached_inflight_expires_without_file_changes(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "completed", (NOW - 100) * 1000, 0, 0)])
        self.rollout("sess_a1", [("new", NOW - 10, None)])
        self.assertEqual(self.zcode_row()["status"], "running")
        self.reader.clock = lambda: NOW + 901
        self.assertEqual(self.zcode_row()["status"], "unknown")

    def test_zcode_partial_append_cannot_change_round_or_status(self):
        self.zcode([("wk", "/w", "sess_a1", "T", "completed", (NOW - 10) * 1000, 0, 0)])
        self.rollout("sess_a1", [("completed", NOW - 60, NOW - 20)])
        path = self.home / ".zcode/cli/rollout/model-io-sess_a1.jsonl"
        with path.open("a") as stream:
            stream.write('{"type":"model_io","sessionId":"sess_a1","turnId":"new","startedAt":"' + self.iso(NOW) + '"')
        self.assertEqual(self.zcode_row()["status"], "completed")
        self.assertEqual(self.zcode_row()["started_at"], NOW - 60)
        with path.open("a") as stream:
            stream.write(',"request":"' + "x" * 600000)
        self.assertEqual(self.zcode_row()["status"], "completed")
        self.assertEqual(self.zcode_row()["started_at"], NOW - 60)

    def test_limit_custom_title_and_only_verified_scheme(self):
        self.workbuddy([(str(i), "completed", NOW - i, None) for i in range(60)] +
                       [("deleted", "completed", NOW + 1, NOW)])
        self.workbuddy([("ai", "completed", NOW, None)], ".workbuddy-ai")
        rows = self.reader.collect()
        regular = [r for r in rows if r["app_id"] == "workbuddy"]
        self.assertEqual(len(regular), SESSION_LIMIT)
        self.assertEqual(regular[0]["title"], "custom")
        self.assertEqual(regular[0]["target"], "workbuddy://chat/0")
        self.assertEqual(rows[-1]["target"], "")

    def test_missing_corrupt_database_and_partial_log_are_harmless(self):
        self.assertEqual(self.reader.collect(), [])
        self.assertEqual(self.reader.last_errors, [])
        root = self.home / ".workbuddy"
        root.mkdir()
        (root / "workbuddy.db").write_bytes(b"not a database")
        self.assertEqual(self.reader.collect(), [])
        self.assertEqual(self.reader.last_errors, ["WorkBuddy：会话索引读取失败（DatabaseError）"])
        (root / "workbuddy.db").unlink()
        self.workbuddy([("s1", "working", NOW - 3600, None)])
        project = root / "projects/workspace-demo"
        project.mkdir(parents=True)
        (project / "s1.jsonl").write_bytes(b'x' * 100000 + b'\n{"type":')
        self.assertEqual(self.reader.collect()[0]["status"], "unknown")
        self.assertEqual(self.reader.last_errors, [])

    def test_locked_database_keeps_titles_but_degrades_all_cached_states(self):
        root = self.workbuddy([("active", "working", NOW, None),
                              ("done", "completed", NOW - 1, None)])
        self.workbuddy([("other-app", "completed", NOW, None)], ".workbuddy-ai")
        before = {row["id"]: row for row in self.reader.collect()}
        self.assertEqual(before["workbuddy:active"]["status"], "running")
        connection = sqlite3.connect(root / "workbuddy.db")
        try:
            connection.execute("BEGIN EXCLUSIVE")
            during = {row["id"]: row for row in self.reader.collect()}
            self.assertEqual(self.reader.last_errors, ["WorkBuddy：会话索引读取失败（OperationalError）"])
            for session_id in ("workbuddy:active", "workbuddy:done"):
                self.assertEqual(during[session_id]["status"], "unknown")
                self.assertEqual(during[session_id]["title"], before[session_id]["title"])
                self.assertEqual(during[session_id]["updated_at"], before[session_id]["updated_at"])
                self.assertNotIn("pid", during[session_id])
            self.assertEqual(during["workbuddy-ai:other-app"]["status"], "completed")
        finally:
            connection.rollback()
            connection.close()
        recovered = {row["id"]: row for row in self.reader.collect()}
        self.assertEqual(recovered["workbuddy:active"]["status"], "running")
        self.assertEqual(recovered["workbuddy:done"]["status"], "completed")
        self.assertEqual(self.reader.last_errors, [])

    def test_corruption_keeps_last_success_but_missing_app_clears_cache(self):
        root = self.workbuddy([("s1", "completed", NOW, None)])
        self.reader.collect()
        database = root / "workbuddy.db"
        database.write_bytes(b"private-path-and-data-must-never-appear-in-diagnostics")
        row = self.reader.collect()[0]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["id"], "workbuddy:s1")
        self.assertEqual(self.reader.last_errors, ["WorkBuddy：会话索引读取失败（DatabaseError）"])
        database.unlink()
        self.assertEqual(self.reader.collect(), [])
        self.assertEqual(self.reader.last_errors, [])

    def test_database_is_unchanged_and_timestamp_units(self):
        root = self.workbuddy([("s1", "completed", NOW, None)])
        before = (root / "workbuddy.db").read_bytes()
        self.reader.collect()
        self.assertEqual(before, (root / "workbuddy.db").read_bytes())
        self.assertEqual(_epoch(NOW * 1000), NOW)
        self.assertEqual(_epoch(float("nan")), 0)

    def log(self, root, events):
        project = root / "projects/workspace-demo"
        project.mkdir(parents=True, exist_ok=True)
        (project / "s1.jsonl").write_text("\n".join(json.dumps(dict(sessionId="s1", **event)) for event in events) + "\n")

    def test_failure_reason_and_exact_turn_duration_without_raw_error(self):
        root = self.workbuddy([("s1", "error", NOW, None)])
        self.log(root, [dict(type="message", role="user", timestamp=(NOW-96)*1000, content="private prompt"),
                        dict(type="message", role="assistant", status="incomplete", timestamp=NOW*1000,
                             providerData={"error": {"status": 429, "message": "usage exceeds frequency limit; reset at 2026-09-23 11:13:48 UTC+8; secret-request-id"}})])
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(row['ended_at'] - row['started_at'], 96)
        self.assertIn('429', row['status_reason'])
        self.assertIn('2026-09-23 11:13:48 UTC+8', row['status_reason'])
        self.assertNotIn('secret-request-id', str(self.reader._file_cache))
        self.assertNotIn('private prompt', str(self.reader._file_cache))

    def test_new_user_turn_does_not_inherit_old_error(self):
        root = self.workbuddy([("s1", "error", NOW-20, None)])
        self.log(root, [dict(type="message", role="assistant", status="incomplete", timestamp=(NOW-21)*1000,
                             providerData={"error": {"message": "getaddrinfo ENOTFOUND private-host"}}),
                        dict(type="message", role="user", timestamp=(NOW-1)*1000)])
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'unknown')
        self.assertNotIn('ended_at', row)
        self.assertNotIn('DNS', row['status_reason'])
        self.assertEqual(row['started_at'], NOW-1)

    def test_missing_log_explains_unknown_start_without_inventing_time(self):
        self.workbuddy([("s1", "error", NOW, None)])
        row = self.reader.collect()[0]
        self.assertNotIn('started_at', row)
        self.assertIn('未找到', row['timing_reason'])

    def test_oversized_turn_recovers_its_start_by_backward_scan(self):
        root = self.workbuddy([("s1", "error", NOW, None)])
        self.log(root, [dict(type="message", role="user", timestamp=(NOW-60)*1000),
                        dict(type="function_call_result", timestamp=(NOW-1)*1000, output='x'*(2*1024*1024)),
                        dict(type="message", role="assistant", status="incomplete", timestamp=NOW*1000,
                             providerData={"error": {"status": 502, "message": "socket hang up"}})])
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(row['started_at'], NOW - 60)
        self.assertEqual(row['timing_basis'], 'turn')
        self.assertEqual(row['ended_at'] - row['started_at'], 60)
        self.assertIn('502', row['status_reason'])

    def test_backward_scan_respects_cap_and_stays_explicit(self):
        import collector.transcript_scan as transcript_scan
        root = self.workbuddy([("s1", "error", NOW, None)])
        self.log(root, [dict(type="message", role="user", timestamp=(NOW-60)*1000),
                        dict(type="function_call_result", timestamp=(NOW-1)*1000, output='x'*(2*1024*1024)),
                        dict(type="message", role="assistant", status="incomplete", timestamp=NOW*1000)])
        original = transcript_scan.BACKSCAN_CAP_BYTES
        transcript_scan.BACKSCAN_CAP_BYTES = 1024
        try:
            reader = DesktopCollector(self.home, lambda: NOW, lambda: self.processes)
            row = reader.collect()[0]
            self.assertNotIn('started_at', row)
            self.assertIn('回溯', row['timing_reason'])
        finally:
            transcript_scan.BACKSCAN_CAP_BYTES = original

    def test_appended_error_after_cached_turn_is_detected_incrementally(self):
        import sqlite3 as sqlite
        root = self.workbuddy([("s1", "working", NOW - 60, None)])
        self.log(root, [dict(type="message", role="user", timestamp=(NOW-60)*1000),
                        dict(type="message", role="assistant", status="completed", timestamp=(NOW-50)*1000)])
        first = self.reader.collect()[0]
        self.assertEqual(first['status'], 'running')
        self.assertEqual(first['started_at'], NOW - 60)
        # A later turn fails after megabytes of output; the cursor absorbs only the tail.
        self.log(root, [dict(type="message", role="user", timestamp=(NOW-60)*1000),
                        dict(type="message", role="assistant", status="completed", timestamp=(NOW-50)*1000),
                        dict(type="message", role="user", timestamp=(NOW-30)*1000),
                        dict(type="function_call_result", timestamp=(NOW-2)*1000, output='y'*(2*1024*1024)),
                        dict(type="message", role="assistant", status="incomplete", timestamp=NOW*1000,
                             providerData={"error": {"status": 502, "message": "socket hang up"}})])
        with sqlite.connect(root / "workbuddy.db") as connection:
            connection.execute("UPDATE sessions SET status='error', updated_at=? WHERE id='s1'", (NOW * 1000,))
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(row['started_at'], NOW - 30)
        self.assertEqual(row['ended_at'] - row['started_at'], 30)
        self.assertIn('502', row['status_reason'])

    def test_rewritten_log_does_not_inherit_stale_turn_start(self):
        root = self.workbuddy([("s1", "error", NOW, None)])
        self.log(root, [dict(type="message", role="user", timestamp=(NOW-60)*1000)])
        self.reader.collect()
        self.log(root, [dict(type="message", role="user", timestamp=(NOW-5)*1000)])
        row = self.reader.collect()[0]
        self.assertEqual(row['started_at'], NOW - 5)


if __name__ == "__main__":
    unittest.main()
