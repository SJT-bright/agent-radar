import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from collector.codex_adapter import SessionCollector, TailCache, _codex_status, _codex_lifecycle, _readonly


def event(kind, stamp=100, **payload):
    return {"type": "event_msg", "timestamp": stamp, "payload": {"type": kind, **payload}}


class StatusTests(unittest.TestCase):
    def test_explicit_interruptions_remain_distinct_from_idle(self):
        self.assertEqual(_codex_status({"status": "interrupted", "completed_at": 110}, {}, None)[0], "interrupted")
        self.assertEqual(_codex_status({}, {"status": "interrupted", "state_at": 110, "event": "turn_aborted"}, None)[0], "interrupted")

    def test_history_running_needs_session_owner(self):
        history = {"status": "inProgress", "started_at": 100}
        self.assertEqual(_codex_status(history, {}, None)[0], "unknown")
        self.assertEqual(_codex_status(history, {}, 22)[0], "running")

    def test_recent_file_timestamp_is_not_running(self):
        self.assertEqual(_codex_status({}, {"updated_at": 99999999}, 22)[0], "unknown")

    def test_history_failed_reason_is_non_blocking(self):
        from collector.failure_reasons import failure_reason
        lifecycle, _ = _codex_lifecycle({"status": "failed", "started_at": 100, "completed_at": 110}, {})
        self.assertEqual(lifecycle["status"], "interrupted")
        self.assertEqual(lifecycle.get("raw_status"), "failed")
        reason = failure_reason(stop=str(lifecycle.get("raw_status")))
        self.assertNotIn("中止事件", reason)
        self.assertNotIn("触发者", reason)

    def test_completed_history_does_not_need_a_live_app(self):
        self.assertEqual(_codex_status({"status": "completed", "completed_at": 100}, {}, None)[0],
                         "completed")

    def test_log_new_turn_can_overtake_history_projection(self):
        history = {"status": "completed", "completed_at": 100}
        tail = {"status": "running", "state_at": 105, "event": "task_started"}
        self.assertEqual(_codex_status(history, tail, 22)[0], "running")

    def test_corrupt_partial_json_and_wait_response(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.jsonl"
            cache = TailCache()
            messages = [event("task_started"), {"type": "response_item", "timestamp": 110,
                        "payload": {"type": "function_call", "name": "request_user_input",
                                    "call_id": "c1", "arguments": "sensitive conversation text"}}]
            path.write_text("\n".join(json.dumps(x) for x in messages) + "\n{broken\n")
            state = cache.read(path, "codex")
            self.assertEqual(state["status"], "waiting")
            self.assertNotIn("sensitive", json.dumps(cache.entries))
            with path.open("a") as out:
                out.write(json.dumps({"type": "response_item", "timestamp": 120,
                                      "payload": {"type": "function_call_output", "call_id": "c1"}}) + "\n")
                out.write(json.dumps(event("task_complete", 130)) + "\n")
            self.assertEqual(cache.read(path, "codex")["status"], "completed")

    def test_cold_scan_recovers_start_hidden_by_large_tool_output(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "large.jsonl"
            messages = [event("task_started", 200, turn_id="new"),
                        {"type": "response_item", "timestamp": 210,
                         "payload": {"type": "function_call_output", "output": "private " * 80_000}}]
            path.write_text("".join(json.dumps(x) + "\n" for x in messages))
            cache = TailCache(); tail = cache.read(path, "codex")
            history = {"status": "interrupted", "started_at": 100, "completed_at": 110, "turn_id": "old"}
            self.assertEqual(_codex_status(history, tail, 22)[0], "running")
            self.assertEqual(tail["started_at"], 200)
            self.assertNotIn("private", json.dumps(cache.entries))
            self.assertEqual(_codex_status(history, tail, None)[0], "unknown")

    def test_incremental_lifecycle_survives_tail_eviction_and_old_abort(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "append.jsonl"
            path.write_text(json.dumps(event("task_started", 200, turn_id="new")) + "\n")
            cache = TailCache(); cache.read(path, "codex")
            with path.open("a") as stream:
                stream.write(json.dumps({"type": "response_item", "timestamp": 210,
                    "payload": {"type": "function_call_output", "output": "x" * 400_000}}) + "\n")
                stream.write(json.dumps(event("turn_aborted", 211, turn_id="old")) + "\n")
            state = cache.read(path, "codex")
            self.assertEqual((state["status"], state["turn_id"], state["started_at"]), ("running", "new", 200))
            self.assertNotIn("ended_at", state)
            with path.open("a") as stream:
                stream.write(json.dumps(event("task_complete", 220, turn_id="new")) + "\n")
            self.assertEqual(cache.read(path, "codex")["status"], "completed")

    def test_new_history_round_ignores_delayed_old_terminal(self):
        history = {"status": "inProgress", "turn_id": "new", "started_at": 200}
        tail = {"status": "interrupted", "turn_id": "old", "started_at": 100,
                "ended_at": 210, "state_at": 210, "event": "turn_aborted"}
        self.assertEqual(_codex_status(history, tail, 22)[0], "running")
        chosen, _ = _codex_lifecycle(history, tail)
        self.assertEqual(chosen["started_at"], 200)
        self.assertEqual(chosen["ended_at"], 0)

    def test_new_turn_within_one_second_overtakes_old_history(self):
        history = {"status": "interrupted", "turn_id": "old", "started_at": 100, "completed_at": 200}
        tail = {"status": "running", "turn_id": "new", "started_at": 200.25,
                "state_at": 200.25, "event": "task_started"}
        self.assertEqual(_codex_status(history, tail, 22)[0], "running")

    def test_same_turn_terminal_uses_matching_history_start(self):
        history = {"status": "inProgress", "turn_id": "same", "started_at": 200}
        tail = {"status": "completed", "turn_id": "same", "ended_at": 201.25,
                "state_at": 201.25, "event": "task_complete"}
        chosen, _ = _codex_lifecycle(history, tail)
        self.assertEqual((chosen["status"], chosen["started_at"], chosen["ended_at"]), ("completed", 200, 201.25))

    def test_partial_appended_record_is_read_when_finished(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "partial.jsonl"
            path.write_text(json.dumps(event("task_started", 100, turn_id="t")) + "\n")
            cache = TailCache(); cache.read(path, "codex")
            final = json.dumps(event("task_complete", 110, turn_id="t")) + "\n"
            with path.open("a") as stream: stream.write(final[:20])
            self.assertEqual(cache.read(path, "codex")["status"], "running")
            with path.open("a") as stream: stream.write(final[20:])
            self.assertEqual(cache.read(path, "codex")["status"], "completed")

    def test_truncated_file_does_not_reuse_previous_running_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "truncated.jsonl"
            path.write_text(json.dumps(event("task_started", 100, turn_id="long-old-id")) + "\n")
            cache = TailCache(); cache.read(path, "codex")
            path.write_text('{}\n')
            self.assertNotIn("status", cache.read(path, "codex"))

    def test_bounded_missing_lifecycle_does_not_republish_stale_interruption(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bounded.jsonl"
            path.write_text(json.dumps(event("task_started", 200, turn_id="new")) + "\n" +
                "".join(json.dumps({"type": "response_item", "timestamp": 210,
                    "payload": {"type": "function_call_output", "output": "x" * 100}}) + "\n" for _ in range(100)))
            with patch("collector.codex_adapter.TAIL_BYTES", 512), patch("collector.codex_adapter.CODEX_SCAN_BYTES", 1024):
                tail = TailCache().read(path, "codex")
            history = {"status": "interrupted", "started_at": 100, "completed_at": 110}
            self.assertEqual(_codex_status(history, tail, 22)[0], "unknown")

    def test_long_round_terminal_outside_start_window_never_joins_old_interruption(self):
        # Exercise the real 8 MiB cap: one long tool response pushes the newer
        # task_started beyond the scan, while its end event remains in the tail.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "long-terminal.jsonl"
            output = json.dumps({"type": "response_item", "timestamp": 210,
                "payload": {"type": "function_call_output", "output": "x" * (9 * 1024 * 1024)}}) + "\n"
            prefix = json.dumps(event("task_started", 200, turn_id="new")) + "\n"
            for terminal in ("task_complete", "turn_aborted"):
                for terminal_turn in ("new", None):
                    with self.subTest(terminal=terminal, turn=terminal_turn):
                        path.write_text(prefix + output + json.dumps(event(terminal, 220, turn_id=terminal_turn)) + "\n")
                        tail = TailCache().read(path, "codex")
                        self.assertTrue(tail["lifecycle_scan_incomplete"])
                        self.assertNotIn("started_at", tail)
                        history = {"status": "interrupted", "started_at": 100,
                                   "completed_at": 110, "turn_id": "old"}
                        chosen, _ = _codex_lifecycle(history, tail)
                        self.assertEqual(chosen, {"status": "unknown"})
                        self.assertEqual(_codex_status(history, tail, 22)[0], "unknown")

    def test_truncated_terminal_with_verified_matching_turn_keeps_history_start(self):
        history = {"status": "inProgress", "turn_id": "same", "started_at": 200}
        tail = {"status": "completed", "turn_id": "same", "ended_at": 220,
                "state_at": 220, "event": "task_complete", "lifecycle_scan_incomplete": True}
        chosen, _ = _codex_lifecycle(history, tail)
        self.assertEqual((chosen["status"], chosen["started_at"]), ("completed", 200))

    def test_missing_sources_create_no_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            collector = SessionCollector(root / "codex", root / "claude")
            with patch("collector.codex_adapter._processes", return_value={}):
                self.assertEqual(collector.collect(), [])
            self.assertEqual(list(root.iterdir()), [])


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex = self.root / "codex"
        self.codex.mkdir()
        self.claude = self.root / "claude"
        self.claude.mkdir()
        self.collector = SessionCollector(self.codex, self.claude)
        self.db = self.codex / "state_5.sqlite"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE threads(id TEXT,name TEXT,title TEXT,cwd TEXT,rollout_path TEXT,"
                        "updated_at INTEGER,archived INTEGER)")
        history = self.codex / "thread_history_1.sqlite"
        with sqlite3.connect(history) as con:
            con.execute("CREATE TABLE thread_turns(thread_id TEXT,status TEXT,started_at INTEGER,"
                        "completed_at INTEGER,rollout_ordinal INTEGER)")

    def tearDown(self):
        self.tmp.cleanup()

    def add_thread(self, sid="abc", status="inProgress", updated=100):
        path = self.codex / "sessions" / (sid + ".jsonl")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(event("task_started", updated)) + "\n")
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,0)",
                        (sid, "真实任务名", "首条长提示", "/projects/测试", str(path), updated))
        with sqlite3.connect(self.codex / "thread_history_1.sqlite") as con:
            con.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?)", (sid, status, updated, None, 1))

    def test_reads_real_name_and_current_owner_without_body(self):
        self.add_thread()
        with patch("collector.codex_adapter._codex_owners", return_value={"abc": 8}):
            rows = self.collector._codex({8: {"app": "codex"}})
        self.assertEqual(rows[0]["title"], "真实任务名")
        self.assertEqual(rows[0]["project"], "/projects/测试")
        self.assertEqual(rows[0]["status"], "running")
        self.assertEqual(rows[0]["target"], "codex://threads/abc")
        self.assertEqual(rows[0]["started_at"], 100)
        self.assertEqual(rows[0]["timing_basis"], "turn")

    def test_database_lock_returns_cached_unknown_then_recovers(self):
        self.add_thread()
        with patch("collector.codex_adapter._codex_owners", return_value={"abc": 8}):
            self.collector._codex({})
            locker = sqlite3.connect(self.db)
            locker.execute("BEGIN EXCLUSIVE")
            try:
                rows = self.collector._codex({})
                self.assertEqual(rows[0]["status"], "unknown")
                self.assertIn("暂时不可读", rows[0]["evidence"])
            finally:
                locker.rollback()
                locker.close()
            self.assertEqual(self.collector._codex({})[0]["status"], "running")

    def test_readonly_database_refuses_writes(self):
        with _readonly(self.db) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM threads")

    def test_missing_readonly_database_cannot_be_created(self):
        missing = self.codex / "missing.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            with _readonly(missing): pass
        self.assertFalse(missing.exists())

    def test_coherent_new_round_clock_does_not_mix_old_history(self):
        self.add_thread(status="interrupted", updated=100)
        with sqlite3.connect(self.codex / "thread_history_1.sqlite") as con:
            con.execute("ALTER TABLE thread_turns ADD COLUMN turn_id TEXT")
            con.execute("UPDATE thread_turns SET turn_id='old', completed_at=110")
        path = self.codex / "sessions" / "abc.jsonl"
        path.write_text(json.dumps(event("task_started", 200, turn_id="new")) + "\n" +
            json.dumps({"type": "response_item", "timestamp": 210,
                "payload": {"type": "function_call_output", "output": "x" * 400_000}}) + "\n")
        with patch("collector.codex_adapter._codex_owners", return_value={"abc": 8}):
            rows = self.collector._codex({8: {"app": "codex"}})
        self.assertEqual(rows[0]["status"], "running")
        self.assertEqual(rows[0]["started_at"], 200)
        self.assertNotIn("ended_at", rows[0])

    def test_limits_codex_sessions(self):
        for i in range(60):
            self.add_thread(str(i), "completed", updated=i)
        with patch("collector.codex_adapter._codex_owners", return_value={}):
            rows = self.collector._codex({})
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0]["id"], "codex:59")

    def test_subagents_are_filtered_before_limit_and_root_is_preserved(self):
        for i in range(60):
            self.add_thread(str(i), "completed", updated=i)
        with sqlite3.connect(self.db) as con:
            con.execute("ALTER TABLE threads ADD COLUMN agent_path TEXT")
            con.execute("UPDATE threads SET agent_path='/root/worker' WHERE updated_at>=20")
            con.execute("UPDATE threads SET agent_path='/root' WHERE id='19'")
            con.execute("UPDATE threads SET agent_path='' WHERE id='18'")
        with patch("collector.codex_adapter._codex_owners", return_value={}):
            rows = self.collector._codex({})
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[0]["id"], "codex:19")
        self.assertEqual(rows[1]["id"], "codex:18")

    def test_state_database_choice_is_numeric(self):
        for number in (9, 10):
            with sqlite3.connect(self.codex / ("state_" + str(number) + ".sqlite")) as con:
                con.execute("CREATE TABLE threads(id TEXT,name TEXT,title TEXT,cwd TEXT,"
                            "rollout_path TEXT,updated_at INTEGER,archived INTEGER)")
                con.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,0)",
                            ("n" + str(number), "迁移" + str(number) + "号任务", "首条长提示",
                             "/projects/测试", "", number))
        with patch("collector.codex_adapter._codex_owners", return_value={}):
            rows = self.collector._codex({})
        self.assertEqual([row["id"] for row in rows], ["codex:n10"])
        self.assertEqual(rows[0]["title"], "迁移10号任务")

    def test_newest_state_database_without_threads_falls_back(self):
        (self.codex / "state_11.sqlite").write_bytes(b"not a real database at all")
        with sqlite3.connect(self.codex / "state_10.sqlite") as con:
            con.execute("CREATE TABLE migrated(id TEXT)")
        with sqlite3.connect(self.codex / "state_9.sqlite") as con:
            con.execute("CREATE TABLE threads(id TEXT,name TEXT,title TEXT,cwd TEXT,"
                        "rollout_path TEXT,updated_at INTEGER,archived INTEGER)")
            con.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,0)",
                        ("n9", "九号会话", "首条长提示", "/projects/测试", "", 100))
        with patch("collector.codex_adapter._codex_owners", return_value={}):
            rows = self.collector._codex({})
        self.assertEqual([row["id"] for row in rows], ["codex:n9"])
        self.assertEqual(rows[0]["title"], "九号会话")
        self.assertTrue(all("暂时不可读" not in error for error in self.collector.last_errors))

    def rollout(self, thread_id, cwd, thread_source="user", mtime=None):
        day = time.strftime("%Y/%m/%d", time.localtime(mtime or time.time()))
        directory = self.codex / "sessions" / day
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("rollout-%s.jsonl" % thread_id)
        head = {"type": "session_meta", "payload": {"id": thread_id, "session_id": thread_id,
                "cwd": cwd, "originator": "Codex Desktop", "thread_source": thread_source}}
        path.write_text(json.dumps(head) + "\n")
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def test_migration_without_threads_table_uses_rollout_fallback(self):
        # Codex 迁移期：state 库存在但无 threads 表。rollout（thread_source=user）
        # 必须兜底出真实行，subagent/guardian_review 排除，时间线来自 thread_turns。
        (self.codex / "state_5.sqlite").unlink()
        now = time.time()
        self.rollout("01a0fdcd-6806-7470-abfb-609fa29a6af0", "/projects/迁移期项目")
        self.rollout("01a0fdce-0000-7470-abfb-609fa29a6af0", "/x", thread_source="subagent")
        self.rollout("01a0fdcf-1111-7470-abfb-609fa29a6af0", "/x", thread_source="guardian_review")
        with sqlite3.connect(self.codex / "thread_history_1.sqlite") as con:
            con.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?)",
                        ("01a0fdcd-6806-7470-abfb-609fa29a6af0", "completed", now - 300, now - 60, 1))
        with patch("collector.codex_adapter._codex_owners", return_value={}):
            rows = self.collector._codex({})
        self.assertEqual([r["id"] for r in rows], ["codex:01a0fdcd-6806-7470-abfb-609fa29a6af0"])
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["project"], "/projects/迁移期项目")
        self.assertEqual(rows[0]["started_at"], now - 300)
        self.assertEqual(rows[0]["ended_at"], now - 60)
        self.assertIn("Codex 会话", rows[0]["title"])
        self.assertTrue(all("暂时不可读" not in e for e in self.collector.last_errors))

    def test_migration_and_empty_fallback_degrades_previous_rows(self):
        # 兜底也为空（无 rollout）时保持既有降级：上次行以 unknown 展示并报错。
        (self.codex / "state_5.sqlite").unlink()
        self.rollout("01a0fdcd-6806-7470-abfb-609fa29a6af0", "/projects/示例")
        with patch("collector.codex_adapter._codex_owners", return_value={"abc": 8}):
            first = self.collector._codex({8: {"app": "codex"}})
        self.assertEqual(first[0]["status"], "unknown")
        for child in (self.codex / "sessions").rglob("*.jsonl"):
            child.unlink()
        with patch("collector.codex_adapter._codex_owners", return_value={"abc": 8}):
            rows = self.collector._codex({8: {"app": "codex"}})
        self.assertEqual([r["id"] for r in rows], [first[0]["id"]])
        self.assertTrue(any("Codex 会话数据库暂时不可读" in e for e in self.collector.last_errors))

    def test_busy_state_database_retries_then_reads_without_degrade(self):
        # 持锁写事务触发一次 SQLITE_BUSY：有限重试后必须读到真实行，
        # 且不产生「暂时不可读」降级错误。
        self.add_thread()
        holder = sqlite3.connect(self.db)
        holder.execute("BEGIN EXCLUSIVE")

        def unlock_and_wait(_):
            holder.rollback()
            holder.close()

        with patch("collector.codex_adapter.time.sleep", side_effect=unlock_and_wait), \
                patch("collector.codex_adapter._codex_owners", return_value={"abc": 8}):
            rows = self.collector._codex({8: {"app": "codex"}})
        self.assertEqual([row["id"] for row in rows], ["codex:abc"])
        self.assertEqual(rows[0]["status"], "running")
        self.assertTrue(all("暂时不可读" not in e for e in self.collector.last_errors))

    def test_unreadable_state_databases_degrade_cached_rows(self):
        self.add_thread()
        with patch("collector.codex_adapter._codex_owners", return_value={"abc": 8}):
            first = self.collector._codex({8: {"app": "codex"}})
            self.assertEqual(first[0]["status"], "running")
            self.db.write_bytes(b"not a real database at all")
            rows = self.collector._codex({8: {"app": "codex"}})
        self.assertEqual([row["id"] for row in rows], [first[0]["id"]])
        self.assertEqual(rows[0]["title"], first[0]["title"])
        self.assertEqual(rows[0]["status"], "unknown")
        self.assertIn("会话数据库暂时不可读", rows[0]["evidence"])
        self.assertTrue(any("Codex 会话数据库暂时不可读" in e for e in self.collector.last_errors))

    def test_lock_ownership_retries_transient_lsof_failure(self):
        self.add_thread()
        from collector import codex_adapter
        lock_path = str(self.codex / "thread-writer-locks" / "abc.lock")
        calls = []
        def flaky_lsof(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd=args, timeout=4)
            return subprocess.CompletedProcess(args, 0,
                stdout="p8\nn/private/tmp/never\nn" + lock_path + "\n")
        with patch("collector.codex_adapter.time.sleep"), \
             patch("collector.codex_adapter.subprocess.run", side_effect=flaky_lsof):
            owners = codex_adapter._codex_owners({8: {"app": "codex"}}, self.codex)
        self.assertEqual(owners, {"abc": 8})
        self.assertEqual(len(calls), 2)

    def test_first_unreadable_state_database_returns_nothing(self):
        self.db.write_bytes(b"not a real database at all")
        self.assertEqual(self.collector._codex({}), [])
        self.assertTrue(any("Codex 会话数据库暂时不可读" in e for e in self.collector.last_errors))

    def test_first_claude_scan_runs_when_monotonic_near_zero(self):
        projects = self.claude / "projects" / "-example"
        projects.mkdir(parents=True)
        path = projects / "abc.jsonl"
        path.write_text(json.dumps({"type": "assistant", "timestamp": 120, "cwd": "/example",
                                   "message": {"stop_reason": "end_turn"}}) + "\n")
        with patch("collector.codex_adapter.time.monotonic", return_value=0.01):
            rows = self.collector._claude({})
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["project"], "/example")

    def test_claude_descriptor_needs_matching_process_start(self):
        sessions = self.claude / "sessions"
        sessions.mkdir()
        (sessions / "12.json").write_text(json.dumps({"pid": 12, "sessionId": "abc", "status": "busy",
                                                    "procStart": "START", "cwd": "/example", "name": "我的任务"}))
        self.assertEqual(self.collector._claude({12: {"app": "claude", "started": "OTHER"}}), [])
        rows = self.collector._claude({12: {"app": "claude", "started": "START"}})
        self.assertEqual(rows[0]["status"], "running")
        self.assertEqual(rows[0]["title"], "我的任务")
        self.assertEqual(rows[0]["app_id"], "claude-code")
        self.assertEqual(rows[0]["id"], "claude:abc")


if __name__ == "__main__":
    unittest.main()
