import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'collector'))
from extended_adapters import ExtendedCollector, _connect

SID = 'e156e9a9-02c6-4e6d-b193-5fd2393194f8'
OTHER = '40bf7ff9-19ae-429c-a716-22259c11b069'
AGENT = '01a0b9a2-88f2-7bf3-9050-89d77b37f5a8'
NOW = 1790102000.0


class ExtendedAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.processes = {42: '/Applications/Qoder CN.app/Contents/MacOS/Qoder CN',
                          43: '/Applications/Grok.app/Contents/MacOS/grok-app'}
        self.reader = ExtendedCollector(self.home, lambda: NOW, lambda: self.processes)

    def qoder(self):
        root = self.home / 'Library/Application Support/com.qodercn.app.stable'
        root.mkdir(parents=True)
        with sqlite3.connect(root / 'main.sqlite') as c:
            c.executescript('''CREATE TABLE chat_sessions(session_id,title,cwd,updated_at,archived,
                deleted_at,session_kind,owner_session_id);
                CREATE TABLE chat_session_messages(session_id,turn_id,status,created_at,updated_at,
                payload_json,sequence);''')
            c.execute('INSERT INTO chat_sessions VALUES(?,?,?,?,0,NULL,\'standard\',NULL)',
                      (SID, '联系人项目', '/project', NOW * 1000))
            c.execute('INSERT INTO chat_session_messages VALUES(?,?,?,?,?,?,?)',
                      (SID, 'turn1', 'completed', (NOW - 100) * 1000, (NOW - 100) * 1000,
                       json.dumps({'role': 'user', 'text': 'PRIVATE CHAT TEXT'}), 1))
        with sqlite3.connect(root / 'chat-session-turn-payload-buffer.sqlite') as c:
            c.executescript('''CREATE TABLE active_turn_projection_items(session_id,turn_id,
                turn_started_at,created_at);
                CREATE TABLE pending_turn_projections(session_id,turn_id,message_status,created_at,
                updated_at,message_json);''')
            c.execute('INSERT INTO active_turn_projection_items VALUES(?,?,?,?)',
                      (SID, 'turn1', NOW - 100, (NOW - 2) * 1000))
        return root

    def grok(self, events):
        root = self.home / 'Library/Application Support/com.grokapp.grok-app'
        root.mkdir(parents=True)
        (root / 'sessions_index.json').write_text(json.dumps([{'id': SID, 'title': 'Grok 项目',
            'projectId': 'project', 'agentSessionId': AGENT, 'updatedAt': NOW}]))
        (root / 'projects.json').write_text(json.dumps([{'id': 'project', 'path': '/project'}]))
        path = self.home / '.grok/sessions' / quote('/project', safe='') / AGENT / 'events.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text('\n'.join(json.dumps(e) for e in events))
        return root, path

    def test_qoder_active_buffer_has_title_pid_and_true_turn_clock(self):
        self.qoder()
        row = self.reader.collect()[0]
        self.assertEqual((row['title'], row['status'], row['pid']), ('联系人项目', 'running', 42))
        self.assertEqual(row['started_at'], NOW - 100)
        self.assertEqual(row['timing_basis'], 'turn')
        self.assertNotIn('PRIVATE', json.dumps(row))

    def test_qoder_each_active_task_is_returned_and_subagents_are_excluded(self):
        root = self.qoder()
        with sqlite3.connect(root / 'main.sqlite') as c:
            c.execute('INSERT INTO chat_sessions VALUES(?,?,?,?,0,NULL,\'standard\',NULL)',
                      (OTHER, '另一个项目', '/other', NOW * 1000))
            c.execute('INSERT INTO chat_sessions VALUES(?,?,?,?,0,NULL,\'sideChat\',?)',
                      (AGENT, '子任务', '/other', NOW * 1000, SID))
        with sqlite3.connect(root / 'chat-session-turn-payload-buffer.sqlite') as c:
            c.execute('INSERT INTO active_turn_projection_items VALUES(?,?,?,?)', (OTHER, 'turn2', NOW - 30, NOW * 1000))
        rows = self.reader.collect()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['status'] == 'running' for r in rows))

    def test_qoder_dead_process_or_stale_activity_is_not_running(self):
        root = self.qoder()
        self.assertEqual(self.reader.collect()[0]['status'], 'running')
        self.processes.clear()
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')
        self.processes[42] = '/Applications/Qoder CN.app/Contents/MacOS/Qoder CN'
        with sqlite3.connect(root / 'chat-session-turn-payload-buffer.sqlite') as c:
            c.execute('UPDATE active_turn_projection_items SET created_at=?', ((NOW - 400) * 1000,))
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')

    def test_qoder_completion_overrides_buffer_for_same_turn(self):
        root = self.qoder()
        with sqlite3.connect(root / 'main.sqlite') as c:
            c.execute('INSERT INTO chat_session_messages VALUES(?,?,?,?,?,?,?)',
                      (SID, 'turn1', 'completed', (NOW - 100) * 1000, NOW * 1000,
                       json.dumps({'role': 'assistant', 'turnStartedAt': NOW - 100, 'completedAt': NOW}), 2))
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['ended_at'], NOW)

    def test_qoder_pending_completion_is_used_before_main_projection(self):
        root = self.qoder()
        with sqlite3.connect(root / 'chat-session-turn-payload-buffer.sqlite') as c:
            c.execute('INSERT INTO pending_turn_projections VALUES(?,?,?,?,?,?)',
                      (SID, 'turn1', 'completed', NOW * 1000, NOW * 1000,
                       json.dumps({'role': 'assistant', 'turnStartedAt': NOW - 100, 'completedAt': NOW})))
        self.assertEqual(self.reader.collect()[0]['status'], 'completed')

    def test_qoder_readonly_and_corruption_degrades_cached_running(self):
        root = self.qoder()
        self.assertEqual(self.reader.collect()[0]['status'], 'running')
        c = _connect(root / 'main.sqlite')
        with self.assertRaises(sqlite3.OperationalError):
            c.execute('DELETE FROM chat_sessions')
        c.close()
        (root / 'main.sqlite').write_bytes(b'broken database')
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')
        self.assertTrue(self.reader.last_errors)

    def test_qoder_failed_projection_is_not_blocked_from_recovery(self):
        root = self.qoder()
        with sqlite3.connect(root / 'main.sqlite') as c:
            c.execute('INSERT INTO chat_session_messages VALUES(?,?,?,?,?,?,?)',
                      (SID, 'turn1', 'failed', (NOW - 100) * 1000, NOW * 1000,
                       json.dumps({'role': 'assistant', 'turnStartedAt': NOW - 100, 'completedAt': NOW}), 2))
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertNotIn('未记录触发者', row['status_reason'])
        self.assertIn('应用记录错误终止', row['status_reason'])
        self.assertNotIn('user_stopped', row)

    def test_qoder_canceled_projection_marks_user_stopped(self):
        root = self.qoder()
        with sqlite3.connect(root / 'main.sqlite') as c:
            c.execute('INSERT INTO chat_session_messages VALUES(?,?,?,?,?,?,?)',
                      (SID, 'turn1', 'canceled', (NOW - 100) * 1000, NOW * 1000,
                       json.dumps({'role': 'assistant', 'turnStartedAt': NOW - 100, 'completedAt': NOW}), 2))
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertTrue(row.get('user_stopped'))
        self.assertIn('用户主动取消', row['status_reason'])

    def test_grok_failed_crash_is_not_blocked_from_recovery(self):
        self.grok([{'ts': NOW - 90, 'type': 'turn_started'},
                   {'ts': NOW - 20, 'type': 'turn_ended', 'outcome': 'failed'}])
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertNotIn('触发者待确认', row['status_reason'])
        self.assertIn('应用记录错误终止', row['status_reason'])

    def test_grok_user_cancel_marks_user_stopped(self):
        self.grok([{'ts': NOW - 90, 'type': 'turn_started'},
                   {'ts': NOW - 20, 'type': 'turn_ended', 'outcome': 'cancelled'}])
        row = self.reader.collect()[0]
        self.assertTrue(row.get('user_stopped'))
        self.assertIn('用户主动取消', row['status_reason'])

    def test_grok_real_start_and_end_are_used_not_file_mtime(self):
        self.grok([{'ts': NOW - 90, 'type': 'turn_started'},
                   {'ts': NOW - 20, 'type': 'turn_ended', 'outcome': 'completed'}])
        row = self.reader.collect()[0]
        self.assertEqual((row['status'], row['started_at'], row['ended_at']), ('completed', NOW - 90, NOW - 20))

    def test_grok_stale_or_dead_app_cannot_look_active(self):
        self.grok([{'ts': NOW - 500, 'type': 'turn_started'}])
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')
        self.processes.clear()
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')

    def test_grok_completed_turn_not_revived_by_trailing_phase(self):
        self.grok([{'ts': NOW - 90, 'type': 'turn_started'},
                   {'ts': NOW - 20, 'type': 'turn_ended', 'outcome': 'completed'},
                   {'ts': NOW - 19, 'type': 'phase_changed', 'phase': 'idle'}])
        self.assertEqual(self.reader.collect()[0]['status'], 'completed')

    def test_grok_new_turn_replaces_previous_end_and_wait_is_explicit(self):
        self.grok([{'ts': NOW - 90, 'type': 'turn_ended', 'outcome': 'completed'},
                   {'ts': NOW - 20, 'type': 'turn_started'},
                   {'ts': NOW - 5, 'type': 'permission_requested'}])
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'waiting')
        self.assertNotIn('ended_at', row)

    def test_grok_invalid_session_path_is_not_read(self):
        root, path = self.grok([])
        data = json.loads((root / 'sessions_index.json').read_text())
        data[0]['agentSessionId'] = '../../private'
        (root / 'sessions_index.json').write_text(json.dumps(data))
        self.assertEqual(self.reader.collect(), [])

    def test_missing_apps_create_nothing(self):
        self.assertEqual(self.reader.collect(), [])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_grok_new_round_does_not_inherit_previous_user_stop(self):
        self.grok([{'ts': NOW - 120, 'type': 'turn_started'},
                   {'ts': NOW - 100, 'type': 'turn_ended', 'outcome': 'canceled'},
                   {'ts': NOW - 10, 'type': 'turn_started'},
                   {'ts': NOW - 1, 'type': 'turn_ended', 'outcome': 'completed'}])
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['started_at'], NOW - 10)
        self.assertNotIn('user_stopped', row)

    def test_qoder_new_live_turn_does_not_inherit_previous_cancel(self):
        root = self.qoder()
        with sqlite3.connect(root / 'main.sqlite') as c:
            c.execute('INSERT INTO chat_session_messages VALUES(?,?,?,?,?,?,?)',
                      (SID, 'old', 'canceled', (NOW - 200) * 1000, (NOW - 150) * 1000,
                       json.dumps({'role': 'assistant', 'turnStartedAt': NOW - 200, 'completedAt': NOW - 150}), 2))
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'running')
        self.assertNotIn('user_stopped', row)

    def test_grok_invalid_index_records_do_not_displace_real_sessions(self):
        root, _ = self.grok([{'ts': NOW - 10, 'type': 'turn_started'}])
        path = root / 'sessions_index.json'
        sessions = json.loads(path.read_text())
        sessions += [{'id': str(i), 'projectId': 'project', 'updatedAt': NOW + 1} for i in range(45)]
        path.write_text(json.dumps(sessions))
        self.assertEqual([r['id'] for r in self.reader.collect()], ['grok:' + SID])


if __name__ == '__main__':
    unittest.main()
