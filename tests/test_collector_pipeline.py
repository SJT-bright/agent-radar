"""Contract checks for the JSON-line pipeline consumed by the native window."""
import importlib.util
import json
import math
from pathlib import Path
import sys
import threading
import time
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

COLLECTOR = Path(__file__).resolve().parents[1] / "collector"
sys.path.insert(0, str(COLLECTOR))
spec = importlib.util.spec_from_file_location("radar_collector_main", COLLECTOR / "main.py")
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def row(sid="codex:a", **values):
    record = {"id": sid, "app_id": "codex", "app_name": "Codex", "title": "测试任务",
              "project": "/test", "status": "running", "evidence": "会话事件与PID匹配",
              "updated_at": 100.0, "source": "local-session", "target": "", "pid": 12}
    record.update(values)
    return record


class PipelineTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(pipeline.autoclaw_adapter, "collect", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        extended = patch.object(pipeline.extended_adapters, "collect", return_value=[])
        extended.start()
        self.addCleanup(extended.stop)
        cline = patch.object(pipeline.cline_adapter, "collect", return_value=[])
        cline.start()
        self.addCleanup(cline.stop)

    def snapshot(self, first, second):
        with patch.object(pipeline.codex_adapter, "collect", return_value=first), \
             patch.object(pipeline.desktop_adapters, "collect", return_value=second):
            return pipeline.snapshot()

    def test_complete_snapshot_can_roundtrip_standard_json(self):
        snapshot = self.snapshot([row()], [row("workbuddy:b", app_id="workbuddy", app_name="WorkBuddy")])
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        result = json.loads(encoded)
        self.assertEqual(len(result["sessions"]), 2)
        self.assertIsInstance(result["errors"], list)
        self.assertTrue(math.isfinite(result["collected_at"]))
        fields = {"id", "app_id", "app_name", "title", "project", "status", "evidence",
                  "updated_at", "source", "target"}
        for item in result["sessions"]:
            self.assertTrue(fields <= set(item))

    def test_duplicate_id_has_single_latest_adapter_record(self):
        snapshot = self.snapshot([row(title="前一条"), None, {}], [row(title="后一条")])
        self.assertEqual(len(snapshot["sessions"]), 1)
        self.assertEqual(snapshot["sessions"][0]["title"], "后一条")

    def test_failed_adapter_does_not_discard_other_adapter_or_leak_error_details(self):
        with patch.object(pipeline.codex_adapter, "collect", side_effect=RuntimeError("private source text")), \
             patch.object(pipeline.desktop_adapters, "collect", return_value=[row("zcode:b", app_id="zcode")]):
            snapshot = pipeline.snapshot()
        self.assertEqual(snapshot["sessions"][0]["id"], "zcode:b")
        self.assertTrue(any("RuntimeError" in message for message in snapshot["errors"]))
        self.assertNotIn("private source text", json.dumps(snapshot))


class WatchPipelineTests(unittest.TestCase):
    def watcher(self, adapters):
        watcher = pipeline.WatchCollector(adapters)
        self.addCleanup(watcher.close)
        return watcher

    def test_slow_source_does_not_block_healthy_sources_or_start_overlapping_scans(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def slow():
            calls.append(1)
            entered.set()
            release.wait(timeout=2)
            return [row("slow:a")]
        self.addCleanup(release.set)
        watcher = self.watcher([("slow", SimpleNamespace(collect=slow)),
                                ("fast", SimpleNamespace(collect=lambda: [row("fast:a")]))])
        started = time.monotonic()
        first = watcher.snapshot(budget=0.03)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(entered.is_set())
        self.assertEqual([item["id"] for item in first["sessions"]], ["fast:a"])
        second = watcher.snapshot(budget=0.03)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["sessions"][0]["status"], "running")
        release.set()
        final = watcher.snapshot(budget=1)
        self.assertEqual({item["id"] for item in final["sessions"]}, {"slow:a", "fast:a"})

    def test_pending_source_keeps_identity_but_cannot_refresh_cached_trusted_status(self):
        release = threading.Event()
        calls = []
        original = row("slow:a", turn_id="round-a", started_at=100)
        def collect():
            calls.append(1)
            if len(calls) > 1:
                release.wait(timeout=2)
            return [original]
        self.addCleanup(release.set)
        watcher = self.watcher([("slow", SimpleNamespace(collect=collect))])
        first = watcher.snapshot(budget=1)
        self.assertEqual(first["sessions"][0]["status"], "running")
        pending = watcher.snapshot(budget=0.02)
        cached = pending["sessions"][0]
        self.assertEqual((cached["id"], cached["turn_id"], cached["status"]),
                         ("slow:a", "round-a", "unknown"))
        self.assertIn("待确认", cached["status_reason"])
        self.assertEqual(original["status"], "running")
        release.set()
        final = watcher.snapshot(budget=1)
        self.assertEqual(final["sessions"][0]["status"], "running")

    def test_failed_source_retains_unconfirmed_identity_without_private_exception_text(self):
        calls = []
        def collect():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("private database information")
            return [row(status="completed")]
        watcher = self.watcher([("source", SimpleNamespace(collect=collect))])
        watcher.snapshot(budget=1)
        failed = watcher.snapshot(budget=1)
        self.assertEqual(failed["sessions"][0]["status"], "unknown")
        self.assertIn("RuntimeError", failed["errors"][0])
        self.assertNotIn("private", json.dumps(failed))

    def test_broken_diagnostic_does_not_discard_successful_sessions(self):
        def diagnostics():
            raise ValueError("private error detail")
        watcher = self.watcher([("source", SimpleNamespace(collect=lambda: [row()],
                                                        diagnostics=diagnostics))])
        result = watcher.snapshot(budget=1)
        self.assertEqual(result["sessions"][0]["status"], "running")
        self.assertIn("ValueError", result["errors"][0])
        self.assertNotIn("private", json.dumps(result))

    def test_only_stalled_source_requests_restart_at_45_seconds_without_overlapping(self):
        release = threading.Event()
        calls, clock = [], [100.0]
        def slow():
            calls.append(1)
            release.wait(timeout=2)
            return []
        self.addCleanup(release.set)
        watcher = self.watcher([("slow", SimpleNamespace(collect=slow)),
                                ("fast", SimpleNamespace(collect=lambda: [row("fast:a")]))])
        watcher.last_rows["slow"] = [row("slow:a")]
        with patch.object(pipeline.time, "monotonic", side_effect=lambda: clock[0]):
            first = watcher.snapshot(budget=0.02)
            self.assertEqual(watcher.stalled_sources, [])
            clock[0] = 144.999
            watcher.snapshot(budget=0.02)
            self.assertEqual(watcher.stalled_sources, [])
            clock[0] = 145.0
            final = watcher.snapshot(budget=0.02)
        self.assertEqual(watcher.stalled_sources, ["slow"])
        self.assertEqual(len(calls), 1)
        rows = {item["id"]: item for item in final["sessions"]}
        self.assertEqual(rows["slow:a"]["status"], "unknown")
        self.assertEqual(rows["fast:a"]["status"], "running")
        self.assertTrue(any("待确认" in error and "重启本地采集器" in error for error in final["errors"]))

    def test_main_flushes_final_unknown_snapshot_before_controlled_collector_exit(self):
        class ExitProbe(BaseException):
            pass
        final = {"sessions": [row(status="unknown")], "errors": ["正在重启本地采集器"],
                 "collected_at": 100.0}
        watcher = SimpleNamespace(snapshot=lambda: final, stalled_sources=["slow"], close=lambda: None)
        output = io.StringIO()
        def forced_exit(code):
            self.assertEqual(code, pipeline.WATCH_RESTART_EXIT_CODE)
            self.assertEqual(json.loads(output.getvalue()), final)
            raise ExitProbe()
        with patch.object(pipeline, "WatchCollector", return_value=watcher), \
             patch.object(sys, "argv", ["collector", "--watch"]), \
             patch.object(pipeline.os, "_exit", side_effect=forced_exit), \
             contextlib.redirect_stdout(output), self.assertRaises(ExitProbe):
            pipeline.main()


if __name__ == "__main__":
    unittest.main()
