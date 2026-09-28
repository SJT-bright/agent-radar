import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'collector'))
from cline_adapter import ClineCollector, _connect

NOW = 1790142000.0
SID = 'session_1790141900000_test'


class ClineTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.root = self.home / '.cline/data/db'
        self.root.mkdir(parents=True)
        self.processes = {10: '/Applications/Cline.app/Contents/MacOS/cline-app',
                          20: '/Applications/Cline.app/Contents/MacOS/code-sidecar'}
        self.reader = ClineCollector(self.home, lambda: NOW, lambda: self.processes)
        with sqlite3.connect(self.root / 'sessions.db') as db:
            db.execute('''CREATE TABLE sessions(session_id,pid,source,status,ended_at,
                updated_at,workspace_root,cwd,metadata_json,is_subagent,parent_session_id)''')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (SID, 20, 'desktop', 'running', None, NOW, '/project', '/project',
                        json.dumps({'title': 'Cline 测试项目', 'systemPrompt': 'PRIVATE SYSTEM TEXT'}), 0, None))
        with sqlite3.connect(self.root / 'hub-events-hub-production.db') as db:
            db.execute('''CREATE TABLE hub_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event,session_id,envelope_json,created_at)''')

    def event(self, kind, timestamp, payload=None, sid=SID):
        with sqlite3.connect(self.root / 'hub-events-hub-production.db') as db:
            db.execute('INSERT INTO hub_events(event,session_id,envelope_json,created_at) VALUES(?,?,?,?)',
                       (kind, sid, json.dumps({'payload': payload or {}}), timestamp * 1000))

    def update(self, statement, args=()):
        with sqlite3.connect(self.root / 'sessions.db') as db:
            db.execute(statement, args)

    def test_live_turn_uses_events_and_worker_not_creation_time(self):
        self.event('run.started', NOW - 30)
        self.event('assistant.delta', NOW - 1, {'text': 'PRIVATE RESPONSE'})
        row = self.reader.collect()[0]
        self.assertEqual((row['title'], row['status'], row['pid']), ('Cline 测试项目', 'running', 10))
        self.assertEqual(row['started_at'], NOW - 30)
        self.assertNotIn('PRIVATE', json.dumps(row))

    def test_completion_is_terminal_even_after_trailing_activity(self):
        self.event('run.started', NOW - 30)
        self.event('run.completed', NOW - 10, {'reason': 'completed'})
        self.event('assistant.delta', NOW - 1)
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['ended_at'] - row['started_at'], 20)

    def test_new_turn_does_not_inherit_previous_completion(self):
        self.event('run.started', NOW - 90)
        self.event('run.completed', NOW - 80, {'reason': 'completed'})
        self.event('run.started', NOW - 5)
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'running')
        self.assertNotIn('ended_at', row)

    def test_failure_reason_sanitized_and_user_stop_distinguished(self):
        self.event('run.started', NOW - 30)
        self.event('run.failed', NOW - 2, {'error': {'message': 'request timeout PRIVATE'}})
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertIn('超时', row['status_reason'])
        self.assertNotIn('PRIVATE', json.dumps(row))
        self.event('run.cancelled', NOW - 1, {'reason': 'cancelled'})
        self.assertTrue(self.reader.collect()[0]['user_stopped'])

    def test_no_event_start_does_not_invent_start_or_running(self):
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'unknown')
        self.assertNotIn('started_at', row)

    def test_stale_or_wrong_worker_process_degrades(self):
        self.event('run.started', NOW - 500)
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')
        self.event('assistant.delta', NOW - 1)
        self.processes[20] = '/usr/bin/unrelated'
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')

    def test_waiting_requires_explicit_registry_state(self):
        self.event('run.started', NOW - 50)
        self.event('capability.requested', NOW - 1, {'capabilityName': 'model'})
        self.assertEqual(self.reader.collect()[0]['status'], 'running')
        self.update("UPDATE sessions SET status='waiting_for_approval'")
        self.assertEqual(self.reader.collect()[0]['status'], 'waiting')

    def test_closing_desktop_does_not_leave_old_current_card(self):
        self.event('run.started', NOW - 2)
        self.assertTrue(self.reader.collect())
        self.processes.pop(10)
        self.assertEqual(self.reader.collect(), [])

    def test_subagents_and_cli_sessions_are_excluded(self):
        self.update("INSERT INTO sessions SELECT 'child',pid,source,status,ended_at,updated_at,workspace_root,cwd,metadata_json,1,? FROM sessions", (SID,))
        self.update("INSERT INTO sessions SELECT 'cli',pid,'cli',status,ended_at,updated_at,workspace_root,cwd,metadata_json,0,NULL FROM sessions LIMIT 1")
        self.assertEqual([r['navigation_key'] for r in self.reader.collect()], [SID])

    def test_corrupt_source_degrades_cache_and_stays_readonly(self):
        db = _connect(self.root / 'sessions.db')
        with self.assertRaises(sqlite3.OperationalError):
            db.execute('DELETE FROM sessions')
        db.close()
        self.event('run.started', NOW - 2)
        self.assertEqual(self.reader.collect()[0]['status'], 'running')
        (self.root / 'hub-events-hub-production.db').write_bytes(b'broken database')
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')
        self.assertTrue(self.reader.last_errors)

    def test_late_session_failure_does_not_claim_previous_turn_ran_for_days(self):
        self.event('run.started', NOW - 86400)
        self.event('run.completed', NOW - 86390, {'reason': 'completed'})
        self.update("UPDATE sessions SET status='failed',ended_at=?", (NOW - 1,))
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertNotIn('started_at', row)

    def test_missing_event_database_is_unknown_not_current(self):
        (self.root / 'hub-events-hub-production.db').unlink()
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
