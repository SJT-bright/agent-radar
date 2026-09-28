"""Contract checks for the JSON-line pipeline consumed by the native window."""
import importlib.util
import json
import math
from pathlib import Path
import sys
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


if __name__ == "__main__":
    unittest.main()
