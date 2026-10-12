import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from collector import hook_events
from collector.codex_adapter import SessionCollector


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "record_claude_event.py"
    spec = importlib.util.spec_from_file_location("record_claude_event", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


record_claude_event = _load_script()


def _start(turn="turn-1", stamp=100, session="abc", cwd="/example"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt_id": turn,
            "cwd": cwd, "timestamp": stamp, "prompt": "SECRET_PROMPT"}


class HookEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "claude-lifecycle.jsonl"
        self.now = 1_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, payload, now=None, max_events=hook_events.MAX_EVENTS):
        return hook_events.record(payload, self.path, now=self.now if now is None else now,
                                  max_events=max_events)

    def test_metadata_only_round_trip_drops_prompt_and_tool_input(self):
        payload = _start()
        payload.update(tool_input={"command": "SECRET_TOOL"}, tool_name="Bash",
                       transcript_path="/secret/transcript.jsonl",
                       last_assistant_message="SECRET_ANSWER")
        self.assertTrue(self.record(payload)["stored"])
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", text)
        self.assertNotIn("transcript", text)
        self.assertNotIn("tool", text)
        stored = json.loads(text)
        self.assertEqual(set(stored), {"session_id", "turn_id", "event", "timestamp", "cwd"})
        self.assertEqual(stored["event"], "turn_started")
        self.assertEqual(stored["turn_id"], "turn-1")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_exact_duplicate_and_second_start_of_the_same_turn_are_rejected(self):
        self.assertTrue(self.record(_start())["stored"])
        again = self.record(_start())
        self.assertFalse(again["stored"])
        self.assertEqual(again["reason"], "duplicate")
        shifted = self.record(_start(stamp=110))
        self.assertFalse(shifted["stored"])
        self.assertEqual(shifted["reason"], "duplicate")
        self.assertEqual(self.path.read_text(encoding="utf-8").count("\n"), 1)

    def test_future_timestamp_is_rejected_and_small_skew_is_kept(self):
        self.assertEqual(self.record(_start(stamp=self.now + 3), now=self.now)["reason"], "future")
        self.assertFalse(self.path.exists())
        self.assertTrue(self.record(_start(stamp=self.now + 1), now=self.now)["stored"])

    def test_old_turn_late_event_and_event_after_terminal_are_rejected(self):
        self.assertTrue(self.record(_start("turn-a", 100))["stored"])
        self.assertTrue(self.record({"hook_event_name": "Stop", "session_id": "abc", "prompt_id": "turn-a",
                                     "cwd": "/example", "timestamp": 120})["stored"])
        self.assertTrue(self.record(_start("turn-b", 200))["stored"])
        late = self.record({"hook_event_name": "PermissionRequest", "session_id": "abc",
                            "prompt_id": "turn-a", "cwd": "/example", "timestamp": 110,
                            "tool_input": {"command": "SECRET_TOOL"}})
        self.assertFalse(late["stored"])
        self.assertEqual(late["reason"], "late_turn")
        after = self.record({"hook_event_name": "PermissionRequest", "session_id": "abc",
                             "prompt_id": "turn-a", "cwd": "/example", "timestamp": 130,
                             "tool_name": "Bash"})
        self.assertFalse(after["stored"])
        self.assertEqual(after["reason"], "late_turn")
        state = hook_events.session_state(hook_events.load(self.path, now=self.now), "abc")
        self.assertEqual((state["turn_id"], state["status"]), ("turn-b", "running"))
        self.assertNotIn("SECRET_TOOL", self.path.read_text(encoding="utf-8"))

    def test_permission_wait_and_user_stop_stay_distinct(self):
        self.record(_start("perm", 100, session="perm"))
        self.record({"hook_event_name": "PermissionRequest", "session_id": "perm", "prompt_id": "perm",
                     "cwd": "/example", "timestamp": 110, "tool_input": {"command": "SECRET_TOOL"}})
        self.record(_start("stop", 100, session="stop"))
        self.record({"event": "user_stopped", "session_id": "stop", "turn_id": "stop",
                     "cwd": "/example", "timestamp": 110})
        grouped = hook_events.load(self.path, now=self.now)
        waiting = hook_events.session_state(grouped, "perm")
        stopped = hook_events.session_state(grouped, "stop")
        self.assertEqual(waiting["status"], "waiting")
        self.assertFalse(waiting["user_stopped"])
        self.assertEqual(stopped["status"], "interrupted")
        self.assertTrue(stopped["user_stopped"])
        self.assertNotEqual(waiting["event"], stopped["event"])

    def test_user_stop_rejects_a_later_normal_stop(self):
        self.record(_start())
        self.assertTrue(self.record({"event": "user_stopped", "session_id": "abc", "turn_id": "turn-1",
                                     "cwd": "/example", "timestamp": 150})["stored"])
        later = self.record({"hook_event_name": "Stop", "session_id": "abc", "prompt_id": "turn-1",
                             "cwd": "/example", "timestamp": 180})
        self.assertFalse(later["stored"])
        self.assertEqual(later["reason"], "late_turn")
        state = hook_events.session_state(hook_events.load(self.path, now=self.now), "abc")
        self.assertTrue(state["user_stopped"])
        self.assertEqual(state["event"], "user_stopped")

    def test_stop_hook_active_is_not_completion_and_missing_turn_is_rejected(self):
        self.record(_start())
        continued = self.record({"hook_event_name": "Stop", "session_id": "abc", "prompt_id": "turn-1",
                                 "cwd": "/example", "timestamp": 150, "stop_hook_active": True,
                                 "last_assistant_message": "SECRET_ANSWER"})
        self.assertFalse(continued["stored"])
        self.assertEqual(continued["reason"], "continued")
        missing = self.record({"hook_event_name": "Stop", "session_id": "abc", "cwd": "/example",
                               "timestamp": 160})
        self.assertEqual(missing["reason"], "turn_unknown")
        state = hook_events.session_state(hook_events.load(self.path, now=self.now), "abc")
        self.assertEqual(state["status"], "running")
        self.assertNotIn("SECRET_ANSWER", self.path.read_text(encoding="utf-8"))

    def test_first_event_without_a_turn_start_does_not_leave_a_file(self):
        result = self.record({"hook_event_name": "PermissionRequest", "session_id": "abc",
                              "prompt_id": "turn-1", "cwd": "/example", "timestamp": 100,
                              "tool_input": {"command": "SECRET_TOOL"}})
        self.assertFalse(result["stored"])
        self.assertEqual(result["reason"], "late_turn")
        self.assertFalse(self.path.exists())

    def test_unsupported_hook_session_and_cwd_are_rejected(self):
        self.assertEqual(self.record({"hook_event_name": "PreToolUse", "session_id": "abc",
                                      "prompt_id": "turn-1", "cwd": "/example", "timestamp": 100,
                                      "tool_input": {"command": "SECRET_TOOL"}})["reason"], "unsupported")
        self.assertEqual(self.record({"hook_event_name": "UserPromptSubmit", "session_id": "../abc",
                                      "prompt_id": "turn-1", "cwd": "/example", "timestamp": 100})["reason"],
                         "invalid")
        self.assertEqual(self.record(_start(cwd="/example\nSECRET"))["reason"], "invalid")
        self.assertFalse(self.path.exists())

    def test_iso_and_millisecond_timestamps_and_unicode_cwd(self):
        payload = _start(cwd="/项目/示例")
        payload["timestamp"] = "2026-10-12T00:00:00Z"
        self.assertTrue(self.record(payload, now=1_800_000_000)["stored"])
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["cwd"], "/项目/示例")
        self.assertGreater(stored["timestamp"], 1_700_000_000)
        other = Path(self.tmp.name) / "ms.jsonl"
        result = hook_events.record(
            {"hook_event_name": "UserPromptSubmit", "session_id": "abc", "prompt_id": "turn-1",
             "cwd": "/example", "timestamp": 1_700_000_000_000},
            other, now=1_800_000_000)
        self.assertTrue(result["stored"])
        self.assertEqual(json.loads(other.read_text())["timestamp"], 1_700_000_000)

    def test_load_skips_extra_fields_long_lines_and_reads_only_the_tail(self):
        valid = {"session_id": "abc", "turn_id": "turn-1", "event": "turn_started",
                 "timestamp": 10, "cwd": "/example"}
        poisoned = dict(valid, prompt="SECRET_PROMPT")
        long_line = b"x" * (hook_events.MAX_LINE + 8)
        self.path.write_bytes(long_line + b"\n" + json.dumps(poisoned).encode() + b"\n"
                              + json.dumps(valid).encode() + b"\n")
        grouped = hook_events.load(self.path, now=self.now)
        self.assertEqual(grouped["abc"], [valid])
        old = {"session_id": "old", "turn_id": "t", "event": "turn_started", "timestamp": 10, "cwd": "/example"}
        new = {"session_id": "new", "turn_id": "t", "event": "turn_started", "timestamp": 20, "cwd": "/example"}
        old_line = (json.dumps(old) + "\n").encode()
        new_line = (json.dumps(new) + "\n").encode()
        bounded = Path(self.tmp.name) / "bounded.jsonl"
        bounded.write_bytes(old_line * 4 + new_line)
        original = hook_events.MAX_FILE_BYTES
        hook_events.MAX_FILE_BYTES = len(new_line) + 3
        try:
            loaded = hook_events.load(bounded, now=self.now)
        finally:
            hook_events.MAX_FILE_BYTES = original
        self.assertEqual(set(loaded), {"new"})

    def test_compaction_keeps_a_later_turn_anchor(self):
        self.assertTrue(self.record(_start("turn-a", 100), max_events=2)["stored"])
        self.assertTrue(self.record({"hook_event_name": "Stop", "session_id": "abc", "prompt_id": "turn-a",
                                     "cwd": "/example", "timestamp": 120}, max_events=2)["stored"])
        self.assertTrue(self.record(_start("turn-b", 200), max_events=2)["stored"])
        state = hook_events.session_state(hook_events.load(self.path, now=self.now), "abc")
        self.assertEqual((state["turn_id"], state["status"]), ("turn-b", "running"))
        self.assertNotIn("turn-a", self.path.read_text(encoding="utf-8"))

    def test_symlink_is_not_followed(self):
        target = Path(self.tmp.name) / "real.jsonl"
        target.write_text("keep\n", encoding="utf-8")
        link = Path(self.tmp.name) / "link.jsonl"
        link.symlink_to(target)
        result = hook_events.record(_start(), link, now=self.now)
        self.assertFalse(result["stored"])
        self.assertEqual(result["reason"], "symlink")
        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    def test_script_is_silent_and_does_not_install_settings(self):
        stdout = io.StringIO()
        code = record_claude_event.main(["--print-config"], io.BytesIO(), self.path, self.now, stdout)
        self.assertEqual(code, 0)
        self.assertFalse(self.path.exists())
        text = stdout.getvalue()
        self.assertIn("不会修改", text)
        self.assertIn("UserPromptSubmit", text)
        self.assertIn("PermissionRequest", text)
        self.assertIn("StopFailure", text)
        self.assertIn("user_stopped", text)
        self.assertIn("一律丢弃", text)
        self.assertNotIn("tool_input", text[text.index("{"):])
        self.assertNotIn("SECRET", text)
        payload = {"hook_event_name": "PermissionRequest", "session_id": "abc", "prompt_id": "turn-1",
                   "cwd": "/example", "timestamp": 120, "tool_input": {"command": "SECRET_TOOL"},
                   "prompt": "SECRET_PROMPT"}
        start = json.dumps(_start()).encode()
        quiet = io.StringIO()
        self.assertEqual(record_claude_event.main([], io.BytesIO(start), self.path, self.now, quiet), 0)
        self.assertEqual(quiet.getvalue(), "")
        quiet = io.StringIO()
        self.assertEqual(record_claude_event.main([], io.BytesIO(json.dumps(payload).encode()),
                                                   self.path, self.now, quiet), 0)
        self.assertEqual(quiet.getvalue(), "")
        stored = self.path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", stored)
        self.assertIn("permission_wait", stored)
        self.assertEqual(record_claude_event.main([], io.BytesIO(b"{" + b" " * (hook_events.MAX_STDIN + 4)),
                                                   self.path, self.now, io.StringIO()), 0)
        self.assertNotIn(" " * 1000, self.path.read_text(encoding="utf-8"))
        self.assertEqual(record_claude_event.main([], io.BytesIO(b""), self.path, self.now, io.StringIO()), 0)
        self.assertEqual(record_claude_event.main([], io.BytesIO(b"[1]"), self.path, self.now, io.StringIO()), 0)

    def test_script_event_reaches_the_claude_collector(self):
        root = Path(self.tmp.name)
        claude = root / "claude"
        sessions = claude / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "12.json").write_text(json.dumps({
            "pid": 12, "sessionId": "abc", "status": "busy", "procStart": "START",
            "cwd": "/example", "name": "我的任务", "updatedAt": 50,
        }))
        events = root / "events.jsonl"
        start = json.dumps(_start(stamp=100)).encode()
        permission = json.dumps({"hook_event_name": "PermissionRequest", "session_id": "abc",
                                 "prompt_id": "turn-1", "cwd": "/other", "timestamp": 140,
                                 "tool_input": {"command": "SECRET_TOOL"}}).encode()
        self.assertEqual(record_claude_event.main([], io.BytesIO(start), events, self.now, io.StringIO()), 0)
        self.assertEqual(record_claude_event.main([], io.BytesIO(permission), events, self.now, io.StringIO()), 0)
        collector = SessionCollector(root / "codex", claude, events)
        rows = collector._claude({12: {"app": "claude", "started": "START"}})
        self.assertEqual(rows[0]["status"], "waiting")
        self.assertEqual(rows[0]["project"], "/example")
        self.assertIn("权限", rows[0]["evidence"])
        self.assertNotIn("SECRET_TOOL", events.read_text(encoding="utf-8"))
        self.assertNotIn("SECRET_TOOL", json.dumps(rows))


if __name__ == "__main__":
    unittest.main()
