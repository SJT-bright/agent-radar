"""Exact Codex sender preflight and routing regressions.

Everything is synthetic: temporary SQLite/log files and in-memory AX/URL nodes.
No native MacBackend is constructed and no external application is controlled.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from collector.codex_adapter import SessionCollector
from supervisor.bridge import BUNDLES, Blocked, Journal, MacBackend, perform


TARGET = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"


def event(kind, stamp, **fields):
    return {"type": "event_msg", "timestamp": stamp,
            "payload": {"type": kind, **fields}}


def write_events(path, events):
    path.write_text("".join(json.dumps(item) + "\n" for item in events))


def request(start=200, kind="followup"):
    return {"app_id": "codex", "id": "codex:" + TARGET, "title": "Synthetic target",
        "project": "/synthetic-audit/target", "source": "local-session",
        "target": "codex://threads/" + TARGET, "started_at": start,
        "mode": "send", "key": "codex:" + TARGET + ":" + str(int(start * 1000)),
        "automation_mode": "rage", "kind": kind, "text": "Synthetic fixture",
        "overwrite_draft": True}


def guard_backend(current_request):
    backend = object.__new__(MacBackend)
    backend.request = current_request
    backend.title = current_request.get("navigation_title") or current_request["title"]
    backend.ax = SimpleNamespace(trusted=lambda: True)
    backend.Quartz = SimpleNamespace(kCGSessionOnConsoleKey="on",
        CGSessionCopyCurrentDictionary=lambda: {"on": True})
    backend.winops = SimpleNamespace(user_active=lambda _: False, frontmost_pid=lambda: 999)
    backend.window = None
    backend.did_write = False
    backend.stage = "preflight"
    return backend


class CodexPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-preflight-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.claude = self.root / "claude"
        (self.codex / "sessions").mkdir(parents=True)
        self.db = self.codex / "state_5.sqlite"
        self.history = self.codex / "thread_history_1.sqlite"
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE threads (id, name, title, cwd, rollout_path, updated_at, archived, agent_path, thread_source)")
        with sqlite3.connect(self.history) as db:
            db.execute("CREATE TABLE thread_turns (thread_id, status, started_at, completed_at, rollout_ordinal, turn_id)")
        self.collector = SessionCollector(self.codex, self.claude)
        self.add_thread()
        self.path = self.codex / "sessions" / (TARGET + ".jsonl")
        for symbol, value in (("_processes", {}), ("_codex_owners", {})):
            patcher = patch("collector.codex_adapter." + symbol, return_value=value)
            patcher.start(); self.addCleanup(patcher.stop)

    def add_thread(self, sid=TARGET, updated=220, name=None):
        path = self.codex / "sessions" / (sid + ".jsonl")
        write_events(path, [event("task_started", 200, turn_id="new"),
                            event("task_complete", 220, turn_id="new")])
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,0,NULL,'user')",
                (sid, name or ("Synthetic target" if sid == TARGET else "Synthetic " + sid),
                 None, "/synthetic-audit/target", str(path), updated))
        # A stale prior-round projection forces lifecycle coherence to come from the log.
        with sqlite3.connect(self.history) as db:
            db.execute("INSERT INTO thread_turns VALUES (?, 'completed', 100, 110, 1, 'old')", (sid,))
        return path

    def long_log(self):
        with self.path.open("w") as stream:
            stream.write(json.dumps(event("task_started", 200, turn_id="new")) + "\n")
            stream.write(json.dumps({"type": "response_item", "timestamp": 210,
                "payload": {"type": "function_call_output", "output": "x" * (20 * 1024 * 1024)}}) + "\n")
            stream.write(json.dumps(event("task_complete", 220, turn_id="new")) + "\n")

    def module(self, collect_session=None):
        return patch.dict("sys.modules", {"codex_adapter": SimpleNamespace(
            collect_session=collect_session or self.collector.collect_session)})

    def assert_guard_blocked(self, code, req=None, collect_session=None):
        backend = guard_backend(req or request())
        with self.module(collect_session), self.assertRaisesRegex(Blocked, "^" + code + "$"):
            backend.guard(fresh=True)
        self.assertFalse(backend.did_write)

    def test_cold_20mib_preflight_recovers_start_and_complete_without_normal_discovery(self):
        self.long_log()
        for _ in range(2):
            # Each instance is cold, equivalent to a newly launched bridge process.
            cold = SessionCollector(self.codex, self.claude)
            with patch.object(cold, "collect", side_effect=AssertionError("full discovery forbidden")), \
                 patch.object(cold, "_claude", side_effect=AssertionError("Claude forbidden")), \
                 patch.object(cold.tails, "read", wraps=cold.tails.read) as reads:
                row = cold.collect_session("codex:" + TARGET)
            self.assertEqual((row["status"], row["started_at"], row["ended_at"]),
                             ("completed", 200, 220))
            self.assertFalse(row["lifecycle_pending"])
            self.assertGreaterEqual(reads.call_count, 3)
            self.assertLessEqual(reads.call_count, 16)
            self.assertTrue(all(call.args == (self.path, "codex") for call in reads.call_args_list))
            backend = guard_backend(request())
            with self.module(cold.collect_session):
                backend.guard(fresh=True)

    def test_complete_synthetic_perform_uses_realtime_exact_session_preflight(self):
        self.long_log()
        backend = guard_backend(request())
        value = [""]
        backend.route = Mock()
        backend.snapshot = lambda: dict(identity=True, busy=False, input_count=1,
            value=value[0], send_exists=True, send_enabled=True)
        def write(text):
            backend.did_write = True
            value[0] = text
        backend.write = Mock(side_effect=write)
        backend.send = Mock()
        journal = Journal(self.root / "journal")
        self.addCleanup(journal.lock.close)
        with self.module(), patch.object(self.collector, "collect", side_effect=AssertionError("full discovery forbidden")):
            result = perform(backend.request, backend, journal)
        self.assertEqual(result["code"], "sent_pending_confirmation")
        self.assertTrue(result["attempted"])
        backend.route.assert_called_once()
        backend.write.assert_called_once_with("Synthetic fixture")
        backend.send.assert_called_once()
        self.assertEqual(len(journal.rows), 1)
        with self.module(), self.assertRaisesRegex(Blocked, "already_attempted"):
            perform(backend.request, backend, journal)
        self.assertEqual(backend.send.call_count, 1)

    def test_exact_target_below_limit_50_never_reads_other_logs_or_claude(self):
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE threads SET updated_at=1 WHERE id=?", (TARGET,))
        for index in range(55):
            self.add_thread("peer-%04d" % index, updated=300 + index)
        db_before, history_before = self.db.read_bytes(), self.history.read_bytes()
        original_read = self.collector.tails.read
        def read_only_target(path, app):
            self.assertEqual((path, app), (self.path, "codex"))
            return original_read(path, app)
        with patch.object(self.collector.tails, "read", side_effect=read_only_target) as reads, \
             patch.object(self.collector, "_claude", side_effect=AssertionError("Claude forbidden")), \
             patch.object(self.collector, "_codex_rollout_fallback", side_effect=AssertionError("fallback scan forbidden")):
            row = self.collector.collect_session("codex:" + TARGET)
        self.assertEqual(row["id"], "codex:" + TARGET)
        reads.assert_called_once_with(self.path, "codex")
        self.assertEqual(self.db.read_bytes(), db_before)
        self.assertEqual(self.history.read_bytes(), history_before)

    def test_read_budget_exhaustion_preserves_unknown_and_blocks_guard(self):
        self.long_log()
        row = self.collector.collect_session("codex:" + TARGET, max_reads=1)
        self.assertEqual(row["status"], "unknown")
        self.assertTrue(row["lifecycle_pending"])
        self.assertNotIn("started_at", row)
        cold = SessionCollector(self.codex, self.claude)
        self.assert_guard_blocked("state_unverified", collect_session=lambda sid: cold.collect_session(sid, max_reads=1))

    def test_time_budget_exhaustion_does_not_spend_a_second_backfill_read(self):
        self.long_log()
        with patch("collector.codex_adapter.time.monotonic", side_effect=[100, 103]), \
             patch.object(self.collector.tails, "read", wraps=self.collector.tails.read) as reads:
            row = self.collector.collect_session("codex:" + TARGET, max_reads=16, budget=2)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(row["status"], "unknown")
        self.assertTrue(row["lifecycle_pending"])

    def test_database_is_refreshed_between_preheat_reads(self):
        self.long_log()
        original_read = self.collector.tails.read
        count = [0]
        def archive_after_first_read(path, app):
            row = original_read(path, app)
            count[0] += 1
            if count[0] == 1:
                with sqlite3.connect(self.db) as db:
                    db.execute("UPDATE threads SET archived=1 WHERE id=?", (TARGET,))
            return row
        with patch.object(self.collector.tails, "read", side_effect=archive_after_first_read):
            self.assertIsNone(self.collector.collect_session("codex:" + TARGET))
        self.assertEqual(count[0], 1)

    def test_same_length_rewrite_invalidates_warm_lifecycle_and_old_request(self):
        before = self.collector.collect_session("codex:" + TARGET)
        self.assertEqual(before["started_at"], 200)
        size, mtime = self.path.stat().st_size, self.path.stat().st_mtime_ns
        write_events(self.path, [event("task_started", 300, turn_id="new"),
                                 event("task_complete", 320, turn_id="new")])
        self.assertEqual(self.path.stat().st_size, size)
        os.utime(self.path, ns=(mtime + 1000000, mtime + 1000000))
        current = self.collector.collect_session("codex:" + TARGET)
        self.assertEqual((current["status"], current["started_at"]), ("completed", 300))
        self.assert_guard_blocked("state_changed")

    def test_same_length_file_replacement_with_same_mtime_invalidates_inode_cache(self):
        self.collector.collect_session("codex:" + TARGET)
        old = self.path.stat()
        replacement = self.path.with_suffix(".replacement")
        write_events(replacement, [event("task_started", 300, turn_id="new"),
                                   event("task_complete", 320, turn_id="new")])
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(self.path)
        self.assertNotEqual(self.path.stat().st_ino, old.st_ino)
        self.assertEqual(self.path.stat().st_size, old.st_size)
        self.assertEqual(self.path.stat().st_mtime_ns, old.st_mtime_ns)
        self.assert_guard_blocked("state_changed")
        self.assertEqual(self.collector.collect_session("codex:" + TARGET)["started_at"], 300)

    def test_append_new_failed_round_refuses_old_request_and_verifies_new_interrupt(self):
        self.collector.collect_session("codex:" + TARGET)
        with self.path.open("a") as stream:
            stream.write(json.dumps(event("task_started", 300, turn_id="newer")) + "\n")
            stream.write(json.dumps(event("task_failed", 310, turn_id="newer", error={"status": 503})) + "\n")
        self.assert_guard_blocked("state_changed")
        row = self.collector.collect_session("codex:" + TARGET)
        self.assertEqual((row["status"], row["started_at"], row["ended_at"]), ("interrupted", 300, 310))
        self.assertIn("503", row["status_reason"])
        backend = guard_backend(request(start=300, kind="interrupt"))
        with self.module():
            backend.guard(fresh=True)

    def test_waiting_and_uncertain_abort_are_not_automatic_recovery(self):
        for kind, expected in (("request_user_input", "state_changed"),
                               ("turn_aborted", "manual_resolution")):
            with self.subTest(kind=kind):
                write_events(self.path, [event("task_started", 200, turn_id="new"), event(kind, 220, turn_id="new")])
                self.collector = SessionCollector(self.codex, self.claude)
                self.assert_guard_blocked(expected, req=request(kind="interrupt"))

    def test_no_owner_unknown_has_no_pending_preheat_marker(self):
        write_events(self.path, [event("task_started", 200, turn_id="new")])
        row = self.collector.collect_session("codex:" + TARGET)
        self.assertEqual(row["status"], "unknown")
        self.assertFalse(row["lifecycle_pending"])
        self.assert_guard_blocked("state_changed")

    def test_missing_target_does_not_reuse_prior_rows_or_log_fallback(self):
        self.collector.collect_session("codex:" + TARGET)
        with sqlite3.connect(self.db) as db:
            db.execute("DELETE FROM threads WHERE id=?", (TARGET,))
        with patch.object(self.collector.tails, "read", side_effect=AssertionError("old log forbidden")), \
             patch.object(self.collector, "_codex_rollout_fallback", side_effect=AssertionError("fallback forbidden")):
            self.assertIsNone(self.collector.collect_session("codex:" + TARGET))
            self.assert_guard_blocked("state_changed")

    def test_archived_target_does_not_reuse_warm_lifecycle(self):
        self.collector.collect_session("codex:" + TARGET)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE threads SET archived=1 WHERE id=?", (TARGET,))
        with patch.object(self.collector.tails, "read", side_effect=AssertionError("archived log forbidden")):
            self.assertIsNone(self.collector.collect_session("codex:" + TARGET))
            self.assert_guard_blocked("state_changed")

    def test_failed_state_database_does_not_return_warm_row_or_scan_rollouts(self):
        self.collector.collect_session("codex:" + TARGET)
        with patch("collector.codex_adapter.sqlite3.connect", side_effect=sqlite3.DatabaseError("synthetic failure")), \
             patch.object(self.collector.tails, "read", side_effect=AssertionError("stale log forbidden")), \
             patch.object(self.collector, "_codex_rollout_fallback", side_effect=AssertionError("fallback forbidden")):
            self.assertIsNone(self.collector.collect_session("codex:" + TARGET))
            self.assert_guard_blocked("state_changed")
        self.assertTrue(self.collector.last_errors)

    def test_missing_database_is_not_recreated_and_does_not_reuse_warm_row(self):
        self.collector.collect_session("codex:" + TARGET)
        self.db.unlink()
        self.assertIsNone(self.collector.collect_session("codex:" + TARGET))
        self.assertFalse(self.db.exists())

    def test_existing_newest_schema_missing_target_does_not_use_older_database(self):
        older = self.codex / "state_4.sqlite"
        with sqlite3.connect(older) as db:
            db.execute("CREATE TABLE threads (id, name, title, cwd, rollout_path, updated_at, archived)")
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,0)",
                (TARGET, "Synthetic target", None, "/synthetic-audit/target", str(self.path), 220))
        with sqlite3.connect(self.db) as db:
            db.execute("DELETE FROM threads")
        self.assertIsNone(self.collector.collect_session("codex:" + TARGET))

    def test_agent_and_guardian_rows_are_excluded_before_log_access(self):
        for field, value in (("agent_path", "/root/helper"), ("thread_source", "subagent"),
                             ("thread_source", "guardian_review")):
            with self.subTest(field=field, value=value):
                with sqlite3.connect(self.db) as db:
                    db.execute("UPDATE threads SET agent_path=NULL, thread_source='user'")
                    db.execute("UPDATE threads SET " + field + "=?", (value,))
                with patch.object(self.collector.tails, "read", side_effect=AssertionError("worker log forbidden")):
                    self.assertIsNone(self.collector.collect_session("codex:" + TARGET))

    def test_invalid_session_id_does_not_start_process_or_database_read(self):
        for sid in ("claude:" + TARGET, "codex:../escape", "codex:tiny", "", "codex:a' OR 1=1"):
            with self.subTest(sid=sid), \
                 patch("collector.codex_adapter._processes", side_effect=AssertionError("invalid ID process scan")), \
                 patch.object(self.collector, "_codex", side_effect=AssertionError("invalid ID DB scan")):
                self.assertIsNone(self.collector.collect_session(sid))

    def test_archived_same_title_and_title_outside_limit_50_disable_title_fallback(self):
        for archived in (0, 1):
            with self.subTest(archived=archived):
                with sqlite3.connect(self.db) as db:
                    db.execute("DELETE FROM threads WHERE id != ?", (TARGET,))
                self.add_thread(OTHER, updated=1, name="Synthetic target")
                with sqlite3.connect(self.db) as db:
                    db.execute("UPDATE threads SET archived=? WHERE id=?", (archived, OTHER))
                    # More recent unrelated rows put the duplicate outside discovery LIMIT 50.
                    db.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,0,NULL,'user')",
                        [("outside-peer-%04d" % i, "Unrelated %d" % i, None, "/unrelated",
                          str(self.codex / "sessions" / "never-read.jsonl"), 500 + i) for i in range(55)])
                with patch.object(self.collector.tails, "read", wraps=self.collector.tails.read) as reads:
                    row = self.collector.collect_session("codex:" + TARGET)
                self.assertEqual(row["status"], "completed")
                self.assertFalse(row["title_unique"])
                self.assertTrue(all(call.args == (self.path, "codex") for call in reads.call_args_list))

    def test_title_normalization_matches_collapsed_whitespace_and_display_truncation(self):
        self.add_thread(OTHER, name="  Synthetic   target\n")
        self.assertFalse(self.collector.collect_session("codex:" + TARGET)["title_unique"])
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE threads SET name=? WHERE id=?", ("x" * 180 + "A", TARGET))
            db.execute("UPDATE threads SET name=? WHERE id=?", ("x" * 180 + "B", OTHER))
        self.assertFalse(self.collector.collect_session("codex:" + TARGET)["title_unique"])

    def test_unique_title_scan_at_capacity_accepts_but_excess_is_ambiguous(self):
        with sqlite3.connect(self.db) as db:
            db.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,0,NULL,'user')",
                [("capacity-peer-%04d" % i, "Unique peer %d" % i, None, "/unrelated",
                  str(self.codex / "sessions" / "never-read.jsonl"), 500 + i) for i in range(2047)])
        self.assertTrue(self.collector.collect_session("codex:" + TARGET)["title_unique"])
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO threads VALUES (?, ?, NULL, '/unrelated', '', 500, 0, NULL, 'user')",
                       ("capacity-peer-over", "Unique extra peer"))
        self.assertFalse(self.collector.collect_session("codex:" + TARGET)["title_unique"])

    def test_title_change_after_initial_preflight_rejects_old_title(self):
        self.collector.collect_session("codex:" + TARGET)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE threads SET name='Renamed target' WHERE id=?", (TARGET,))
        self.assert_guard_blocked("target_unverified")


def route_backend(open_succeeds=True, visible_ids=(TARGET,), include_window=False, title_unique=True):
    backend = guard_backend(request())
    layout = {"role": "AXGroup", "rect": (200, 550, 950, 790), "AXDOMClassList": ["_ComposerLayoutBody_fixture"]}
    editor = {"role": "AXTextArea", "rect": (220, 600, 900, 720), "AXDOMClassList": ["ProseMirror"],
        "AXParent": layout, "placeholder": "Work with Codex", "AXValue": ""}
    button = {"role": "AXButton", "rect": (880, 730, 940, 780), "label": "Send", "AXParent": layout}
    header = {"role": "AXHeading", "rect": (300, 40, 700, 65), "label": "Synthetic target", "AXValue": 1}
    nodes = [(n, n["role"]) for n in (header, layout, editor, button)]
    nodes.extend(({"role": "AXWindow" if include_window else "AXWebArea", "AXURL": "codex://threads/" + sid},
                  "AXWindow" if include_window else "AXWebArea") for sid in visible_ids)
    evidence = {"writes": [], "presses": [], "urls": []}
    backend.ax = SimpleNamespace(role=lambda n: n.get("role", ""), rect_of=lambda n: n.get("rect"),
        element_name=lambda n: n.get("label", ""), get_attr=lambda n, key, default=None: n.get(key, default),
        children=lambda n: n.get("children", []), role_description=lambda n: n.get("description", ""),
        placeholder=lambda n: n.get("placeholder", ""), enabled=lambda n: True,
        get=lambda n, key: (0, n.get(key)), trusted=lambda: True,
        press=lambda n: evidence["presses"].append(n["label"]) or True)
    backend.window = SimpleNamespace(rect=(0, 0, 1000, 800), pid=999)
    backend.bundle = BUNDLES["codex"]
    backend.title = "Synthetic target"
    backend.codex_title_unique = title_unique
    def open_url(url):
        evidence["urls"].append(url)
        return open_succeeds
    workspace = SimpleNamespace(URLForApplicationToOpenURL_=lambda _: "synthetic-handler", openURL_=open_url)
    backend.AppKit = SimpleNamespace(NSURL=SimpleNamespace(URLWithString_=lambda url: url),
        NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace),
        NSBundle=SimpleNamespace(bundleWithURL_=lambda _: SimpleNamespace(bundleIdentifier=lambda: backend.bundle)))
    backend.bind_window = lambda: "synthetic-app"
    backend.focus_window = lambda _: None
    backend.nodes = lambda: nodes
    backend.sidebar_candidates = lambda _: []
    def write(text):
        backend.did_write = True
        editor["AXValue"] = text
        evidence["writes"].append(text)
    backend.write = write
    return backend, nodes, evidence


class CodexRouteBoundaryTests(unittest.TestCase):
    def result(self, backend):
        with tempfile.TemporaryDirectory(prefix="codex-route-synthetic-") as directory:
            journal = Journal(Path(directory))
            try:
                row = dict(backend.request, status="completed", status_reason="",
                           title_unique=backend.codex_title_unique)
                with patch.dict("sys.modules", {"codex_adapter": SimpleNamespace(collect_session=lambda _: row)}):
                    try:
                        result = perform(backend.request, backend, journal)
                    except Blocked as blocked:
                        result = {"code": str(blocked), "attempted": backend.did_write}
                return result, len(journal.rows)
            finally:
                journal.lock.close()

    def test_false_url_dispatch_refuses_before_input_with_or_without_visible_matching_id(self):
        for ids in ((TARGET,), ()):
            with self.subTest(ids=ids):
                backend, _, evidence = route_backend(open_succeeds=False, visible_ids=ids)
                result, records = self.result(backend)
                self.assertEqual(result, {"code": "target_unverified", "attempted": False})
                self.assertEqual((evidence["writes"], evidence["presses"], records), ([], [], 0))

    def test_conflicting_visible_web_or_window_uuid_is_not_current_identity(self):
        for ids, window in (((OTHER,), False), ((TARGET, OTHER), False), ((OTHER,), True)):
            with self.subTest(ids=ids, window=window):
                backend, nodes, _ = route_backend(visible_ids=ids, include_window=window)
                self.assertFalse(backend.current_identity(nodes))

    def test_matching_uuid_or_title_only_does_not_break_known_success_path(self):
        for ids in ((TARGET,), ()):
            with self.subTest(ids=ids):
                backend, nodes, evidence = route_backend(visible_ids=ids)
                self.assertTrue(backend.current_identity(nodes))
                result, records = self.result(backend)
                self.assertEqual(result["code"], "sent_pending_confirmation")
                self.assertTrue(result["attempted"])
                self.assertEqual(evidence["presses"], ["Send"])
                self.assertEqual(len(evidence["writes"]), 1)
                self.assertEqual(records, 1)

    def test_conflicting_identity_rejects_complete_route_without_input(self):
        backend, _, evidence = route_backend(visible_ids=(OTHER,))
        # Exercise navigation once with an already-exhausted synthetic deadline.
        # No real time wait or UI operation is needed to prove the rejection.
        with patch("supervisor.bridge.time.monotonic", side_effect=[100, 102]):
            result, records = self.result(backend)
        self.assertEqual(result["code"], "sidebar_target_missing")
        self.assertFalse(result["attempted"])
        self.assertEqual((evidence["writes"], evidence["presses"], records), ([], [], 0))

    def test_no_visible_uuid_requires_unique_indexed_title(self):
        backend, nodes, _ = route_backend(visible_ids=(), title_unique=False)
        self.assertFalse(backend.current_identity(nodes))
        # An exact renderer UUID is sufficient even when the title is shared.
        backend, nodes, _ = route_backend(visible_ids=(TARGET,), title_unique=False)
        self.assertTrue(backend.current_identity(nodes))

    def test_ambiguous_title_without_visible_uuid_rejects_complete_route(self):
        backend, _, evidence = route_backend(visible_ids=(), title_unique=False)
        with patch("supervisor.bridge.time.monotonic", side_effect=[100, 102]):
            result, records = self.result(backend)
        self.assertFalse(result["attempted"])
        self.assertEqual((evidence["writes"], evidence["presses"], records), ([], [], 0))

    def test_sidebar_links_do_not_claim_current_conversation_uuid(self):
        backend, nodes, _ = route_backend(visible_ids=(), title_unique=True)
        nodes.append(({"AXURL": "codex://threads/" + OTHER}, "AXLink"))
        self.assertTrue(backend.current_identity(nodes))

    def test_inspect_uses_index_identity_without_terminal_guard_or_any_mutation(self):
        for unique, ids, expected in ((True, (), "already_running"),
                                      (False, (), "target_unverified"),
                                      (False, (TARGET,), "already_running"),
                                      (True, (OTHER,), "target_unverified")):
            with self.subTest(unique=unique, ids=ids):
                backend, nodes, evidence = route_backend(visible_ids=ids, title_unique=unique)
                # An enabled stop in the primary composer proves this is a running UI.
                layout = next(n for n, role in nodes if role == "AXTextArea")["AXParent"]
                nodes.append(({"role": "AXButton", "rect": (820, 730, 880, 780),
                    "label": "Stop", "AXParent": layout}, "AXButton"))
                row = dict(backend.request, status="running", title_unique=unique)
                with patch.dict("sys.modules", {"codex_adapter": SimpleNamespace(collect_session=lambda _: row)}), \
                     patch.object(backend, "guard", side_effect=AssertionError("inspect must not terminal-guard")):
                    result = backend.inspect()
                self.assertEqual(result["code"], expected)
                self.assertFalse(result["attempted"])
                self.assertEqual((evidence["writes"], evidence["presses"], evidence["urls"]), ([], [], []))


if __name__ == "__main__":
    unittest.main()
