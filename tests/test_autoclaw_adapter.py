import json
from pathlib import Path
import tempfile
import unittest

from collector.autoclaw_adapter import AutoClawCollector

NOW = 1790000000.0


class AutoClawTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.directory = self.home / '.openclaw-autoclaw/agents/main/sessions'
        self.directory.mkdir(parents=True)
        self.now = NOW
        self.live = True
        self.reader = AutoClawCollector(self.home, lambda: self.now, lambda pid: pid == 12 and self.live)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, status='running', age=10, end=None, aborted=False):
        entry = dict(sessionId='demo', status=status, updatedAt=(NOW - age) * 1000,
                     startedAt=(NOW - 60) * 1000, abortedLastRun=aborted)
        if end:
            entry['endedAt'] = end * 1000
        (self.directory / 'sessions.json').write_text(json.dumps({'agent:main:demo': entry}))
        self.write_events([('user', NOW-60, None), ('assistant', NOW-age, 'toolUse')])
        return entry

    def write_events(self, events):
        header = dict(type='session', cwd='/demo')
        rows = [header]
        for role, stamp, reason in events:
            content = ('<system-reminder>private policy</system-reminder>\n'
                       '<<<AUTOCLAW_USER_AUTHORED_REQUEST_START>>>\n真实任务名称\n'
                       '<<<AUTOCLAW_USER_AUTHORED_REQUEST_END>>>') if role == 'user' else 'private output'
            rows.append(dict(type='message', timestamp=stamp, message=dict(role=role, stopReason=reason, content=content)))
        (self.directory / 'demo.jsonl').write_text('\n'.join(json.dumps(r) for r in rows)+'\n')

    def lock(self, created=NOW-60):
        (self.directory / 'demo.jsonl.lock').write_text(json.dumps({'pid': 12, 'createdAt': created * 1000}))

    def test_running_matches_turn_lock_and_ignores_other_openclaw_home(self):
        self.fixture(); self.lock()
        unrelated = self.home / '.openclaw/agents/main/sessions'
        unrelated.mkdir(parents=True)
        (unrelated / 'sessions.json').write_text('{broken unrelated content')
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['title'], '真实任务名称')
        self.assertEqual(row['started_at'], NOW-60)
        self.assertNotIn('pid', row)  # gateway PID must not be used as a GUI PID
        self.assertNotIn('private', str(self.reader.cache))

    def test_confirmed_failures_and_timeout_are_not_idle(self):
        for status in ['failed', 'killed', 'timeout']:
            with self.subTest(status=status):
                self.fixture(status, end=NOW-10)
                self.assertEqual(self.reader.collect()[0]['status'], 'interrupted')
                self.assertEqual(self.reader.collect()[0]['ended_at'], NOW-10)

    def test_dead_lock_is_interrupted_missing_lock_is_only_suspected(self):
        self.fixture(age=30); self.lock(); self.live = False
        self.assertEqual(self.reader.collect()[0]['status'], 'interrupted')
        (self.directory / 'demo.jsonl.lock').unlink()
        self.assertEqual(self.reader.collect()[0]['status'], 'stalled')

    def test_long_tool_wait_is_suspected_and_recovers_on_real_event(self):
        self.fixture(age=30); self.lock()
        self.now = NOW + 400
        self.assertEqual(self.reader.collect()[0]['status'], 'stalled')
        self.write_events([('user', NOW-60, None), ('assistant', self.now, 'toolUse')])
        self.assertEqual(self.reader.collect()[0]['status'], 'running')

    def test_unavailable_process_probe_cannot_confirm_interruption(self):
        self.fixture(age=30); self.lock()
        self.reader.pid_checker = lambda pid: None
        self.assertEqual(self.reader.collect()[0]['status'], 'stalled')

    def test_new_user_turn_supersedes_previous_failure(self):
        self.fixture('failed', age=30, end=NOW-30); self.lock(created=NOW-5)
        self.write_events([('user', NOW-5, None)])
        self.assertEqual(self.reader.collect()[0]['status'], 'running')

    def test_lock_renewal_never_resets_clock_or_fakes_progress(self):
        self.fixture(age=30); self.lock()
        first = self.reader.collect()[0]
        self.now = NOW + 600
        self.lock(created=NOW+599)
        renewed = self.reader.collect()[0]
        self.assertEqual(renewed['started_at'], first['started_at'])
        self.assertEqual(renewed['status'], 'stalled')
        self.assertEqual(renewed['last_activity_at'], first['last_activity_at'])

    def test_completed_clock_freezes_and_new_turn_replaces_old_end(self):
        self.fixture()
        self.write_events([('user', NOW-60, None), ('assistant', NOW-5, 'stop')])
        before = self.reader.collect()[0]
        self.now += 120
        after = self.reader.collect()[0]
        self.assertEqual(before['status'], 'completed')
        self.assertEqual(before['ended_at'], after['ended_at'])
        self.write_events([('assistant', NOW-5, 'stop'), ('user', NOW+100, None)])
        self.lock(created=NOW+100)
        new = self.reader.collect()[0]
        self.assertEqual(new['status'], 'running')
        self.assertEqual(new['started_at'], NOW+100)
        self.assertNotIn('ended_at', new)

    def test_unreadable_index_retains_identity_but_never_running(self):
        self.fixture(); self.lock(); self.reader.collect()
        (self.directory / 'sessions.json').write_text('{broken')
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'unknown')
        self.assertEqual(row['title'], '真实任务名称')
        self.assertTrue(self.reader.last_errors)
        self.fixture(); self.lock()
        self.assertEqual(self.reader.collect()[0]['status'], 'running')

    def test_path_escape_and_oversized_lock_do_not_get_read(self):
        entry = self.fixture()
        entry['sessionId'] = '../outside'
        (self.directory / 'sessions.json').write_text(json.dumps({'x':entry}))
        self.assertEqual(self.reader.collect(), [])
        self.fixture(age=40)
        (self.directory / 'demo.jsonl.lock').write_text('x'*5000)
        self.assertEqual(self.reader.collect()[0]['status'], 'stalled')

    def test_copying_old_log_does_not_create_new_activity(self):
        self.fixture(age=30); self.lock()
        self.now = NOW + 1000
        path = self.directory / 'demo.jsonl'
        path.write_bytes(path.read_bytes())
        self.assertEqual(self.reader.collect()[0]['status'], 'stalled')

    def test_long_turn_start_is_recovered_behind_the_tail_window(self):
        self.fixture(); self.lock()
        path = self.directory / 'demo.jsonl'
        with path.open('a') as stream:
            stream.write(json.dumps(dict(type='message', timestamp=NOW-1,
                message=dict(role='toolResult', content='p' * (2 * 1024 * 1024)))) + '\n')
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['started_at'], NOW - 60)

    def test_terminal_error_reason_is_classified_without_leaking_raw_text(self):
        self.fixture()
        path = self.directory / 'demo.jsonl'
        with path.open('a') as stream:
            stream.write(json.dumps(dict(type='message', timestamp=NOW,
                message=dict(role='assistant', stopReason='error', errorMessage='getaddrinfo ENOTFOUND private-server token=secret'))) + '\n')
        row = self.reader.collect()[0]
        self.assertEqual(row['status'], 'interrupted')
        self.assertIn('DNS', row['status_reason'])
        self.assertNotIn('private-server', str(self.reader.cache))
        self.assertNotIn('secret', str(self.reader.cache))

    def test_user_stop_applies_only_to_its_own_turn(self):
        entry = self.fixture('killed', end=NOW - 5)
        entry['autoclawUserStoppedAt'] = (NOW - 5) * 1000
        (self.directory / 'sessions.json').write_text(json.dumps({'agent:main:demo': entry}))
        row = self.reader.collect()[0]
        self.assertTrue(row['user_stopped'])
        self.assertIn('用户主动停止', row['status_reason'])
        self.write_events([('user', NOW, None)])
        self.lock(created=NOW)
        row = self.reader.collect()[0]
        self.assertFalse(row['user_stopped'])

    def test_exact_navigation_key_maps_short_title_but_subagents_do_not(self):
        self.fixture()
        self.reader.title_reader.read = lambda: {'agent:main:demo': '界面短标题'}
        row = self.reader.collect()[0]
        self.assertEqual(row['title'], '界面短标题')
        self.assertEqual(row['navigation_title'], '界面短标题')
        self.assertEqual(row['navigation_key'], 'agent:main:demo')

    def test_internal_workers_do_not_displace_desktop_sessions(self):
        entry = self.fixture()
        data = {'agent:main:demo': entry}
        for kind in ('subagent', 'cron', 'evolution-check'):
            for i in range(45):
                data['agent:main:' + kind + ':' + str(i)] = dict(entry, updatedAt=(NOW + i) * 1000)
        (self.directory / 'sessions.json').write_text(json.dumps(data))
        rows = self.reader.collect()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['navigation_key'], 'agent:main:demo')

if __name__ == '__main__':
    unittest.main()
