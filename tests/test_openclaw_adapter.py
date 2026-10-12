import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collector.openclaw_adapter import OpenClawCollector, parse_id

NOW = 1791808000.5
SID = '11111111-1111-4111-8111-111111111111'
RUN = '22222222-2222-4222-8222-222222222222'


class OpenClawAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db = self.home / '.openclaw/agents/main/agent/openclaw-agent.sqlite'
        self.db.parent.mkdir(parents=True)
        self.con = sqlite3.connect(self.db)
        self.con.executescript('''CREATE TABLE session_nodes (session_key TEXT,current_session_id TEXT,
            status TEXT,updated_at INTEGER,label TEXT,display_name TEXT,entry_json TEXT,
            entry_valid INTEGER,archived_at INTEGER);
            CREATE TABLE trajectory_runtime_events (session_id TEXT,seq INTEGER,run_id TEXT,
            event_json TEXT,created_at INTEGER);
            CREATE TABLE auth_profile_store (credential TEXT);
            INSERT INTO auth_profile_store VALUES ('NEVER_READ_CREDENTIAL');''')
        self.reader = OpenClawCollector(self.home, lambda: NOW)
        self.entry()

    def tearDown(self):
        self.con.close(); self.tmp.cleanup()

    def entry(self, sid=SID, key='agent:main:desktop', status='running', archived=None, **values):
        raw = dict(sessionId=sid, startedAt=(NOW - 60) * 1000)
        raw.update(values)
        self.con.execute('INSERT INTO session_nodes VALUES (?,?,?,?,?,?,?,?,?)',
                         (key, sid, status, (NOW - 5) * 1000, '目标任务', None, json.dumps(raw), 1, archived))
        self.con.commit()

    def event(self, kind='session.started', sid=SID, run=RUN, age=60, **data):
        event = dict(type=kind, workspaceDir='/demo', data=data, text='NEVER_READ_CHAT')
        seq = self.con.execute('SELECT count(*) FROM trajectory_runtime_events').fetchone()[0] + 1
        self.con.execute('INSERT INTO trajectory_runtime_events VALUES (?,?,?,?,?)',
                         (sid, seq, run, json.dumps(event), (NOW - age) * 1000))
        self.con.commit()

    def terminal(self, node='failed', **data):
        self.event()
        self.event('session.ended', age=5, **data)
        self.con.execute('UPDATE session_nodes SET status=?', (node,)); self.con.commit()

    def test_running_requires_matching_fresh_start(self):
        self.event(); row=self.reader.collect()[0]
        self.assertEqual(row['status'], 'running'); self.assertEqual(row['started_at'], NOW-60)
        self.assertEqual(row['navigation_key'], 'agent:main:desktop')

    def test_missing_events_is_unknown(self):
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')

    def test_timeout_is_recoverable_interrupt(self):
        self.terminal(timedOut=True, aborted=True)
        row=self.reader.collect()[0]
        self.assertEqual(row['status'], 'interrupted'); self.assertFalse(row['user_stopped'])
        self.assertIn('超时', row['status_reason'])

    def test_failure_is_recoverable_interrupt(self):
        self.terminal(status='error', stopReason='error')
        self.assertEqual(self.reader.collect()[0]['status'], 'interrupted')

    def test_provider_error_map_retains_only_classified_metadata(self):
        self.terminal(status='error')
        path=self.home/'Library/Logs/openclaw/gateway.log';path.parent.mkdir(parents=True)
        path.write_text('[agent/embedded] runId='+RUN+' isError=true error=Connection error SECRET_DETAIL\n')
        row=self.reader.collect()[0]
        self.assertIn('网络连接失败',row['status_reason'])
        self.assertNotIn('SECRET',json.dumps(row))
        self.assertNotIn('SECRET',str(self.reader._error_metadata))

    def test_external_abort_preserves_stop(self):
        self.terminal(externalAbort=True, aborted=True)
        self.assertTrue(self.reader.collect()[0]['user_stopped'])

    def test_unattributed_abort_is_not_an_auto_recovery(self):
        self.terminal(aborted=True)
        self.assertIn('触发者待确认', self.reader.collect()[0]['status_reason'])

    def test_success_requires_real_end(self):
        self.terminal(node='done', status='success', stopReason='end_turn')
        self.assertEqual(self.reader.collect()[0]['status'], 'completed')

    def test_running_index_conflicting_with_end_is_unknown(self):
        self.terminal(node='running', status='success', stopReason='stop')
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')

    def test_new_turn_invalidates_older_end(self):
        self.terminal(status='error')
        self.con.execute("UPDATE session_nodes SET entry_json=json_set(entry_json,'$.startedAt',?)", ((NOW-1)*1000,))
        self.con.commit(); self.assertEqual(self.reader.collect()[0]['status'], 'unknown')

    def test_wrong_last_run_invalidates_event(self):
        self.event()
        self.con.execute("UPDATE session_nodes SET entry_json=json_set(entry_json,'$.lastRunId','different')")
        self.con.commit(); self.assertEqual(self.reader.collect()[0]['status'], 'unknown')

    def test_sequence_numbers_restart_each_run_real_time_orders_rounds(self):
        self.event(run='old-run', age=120)
        self.con.execute('UPDATE trajectory_runtime_events SET seq=9999');self.con.commit()
        self.terminal(status='error')
        self.con.execute('UPDATE trajectory_runtime_events SET seq=1 WHERE run_id=?',(RUN,));self.con.commit()
        self.assertEqual(self.reader.collect()[0]['status'],'interrupted')

    def test_terminal_without_start_is_unknown(self):
        self.event('session.ended', status='error')
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')

    def test_future_start_is_unknown(self):
        self.event(age=-100)
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')

    def test_old_index_is_not_running_forever(self):
        self.event()
        self.reader.clock=lambda:NOW+1000
        self.assertEqual(self.reader.collect()[0]['status'], 'unknown')

    def test_other_profiles_and_auth_chat_are_not_read(self):
        self.event()
        foreign=self.home/'.openclaw-autoclaw/agents/main/sessions'
        foreign.mkdir(parents=True); (foreign/'sessions.json').write_text('broken')
        self.assertNotIn('NEVER_READ', json.dumps(self.reader.collect()))

    def test_target_does_not_depend_on_recent_list_limit(self):
        for i in range(60):
            self.entry(sid=f'{i:08x}-3333-4333-8333-333333333333', key=f'agent:main:other-{i}')
        self.event()
        rows=self.reader.collect('openclaw:main:'+SID)
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['id'],'openclaw:main:'+SID)

    def test_children_are_excluded_before_limit(self):
        for i in range(60):
            self.entry(sid=f'{i:08x}-3333-4333-8333-333333333333', key=f'agent:main:dashboard:{i}',spawnedBy='parent')
        self.event(); self.assertEqual(len(self.reader.collect()),1)

    def test_only_explicit_exact_lookup_can_include_child(self):
        self.con.execute("UPDATE session_nodes SET entry_json=json_set(entry_json,'$.spawnedBy','parent')")
        self.con.commit();self.event()
        self.assertEqual(self.reader.collect(),[])
        self.assertEqual(self.reader.collect(include_children=True),[])
        self.assertEqual(self.reader.collect('openclaw:main:'+SID),[])
        self.assertEqual(len(self.reader.collect('openclaw:main:'+SID,include_children=True)),1)

    def test_selected_child_is_visible_but_manual_only(self):
        self.con.execute("UPDATE session_nodes SET entry_json=json_set(entry_json,'$.spawnedBy','parent')")
        self.con.commit();self.event()
        path=self.home/'Library/Application Support/AgentRadar/selected-openclaw-sessions.json'
        path.parent.mkdir(parents=True);path.write_text(json.dumps({'version':1,'ids':['openclaw:main:'+SID]}))
        row=self.reader.collect()[0]
        self.assertTrue(row['manual_only'])
        self.assertEqual(row['id'],'openclaw:main:'+SID)

    def test_cron_and_external_runtime_are_not_recovery_targets(self):
        self.entry(key='agent:main:cron:one')
        self.entry(key='agent:main:codex',agentHarnessId='codex')
        self.assertEqual(len(self.reader.collect()),1)

    def test_duplicate_stable_id_refuses_precise_target(self):
        self.entry(key='agent:main:second')
        self.assertEqual(self.reader.collect('openclaw:main:'+SID),[])

    def test_archived_target_is_excluded(self):
        self.con.execute('UPDATE session_nodes SET archived_at=1'); self.con.commit()
        self.assertEqual(self.reader.collect(),[])

    def test_invalid_id_and_database_redirect_are_refused(self):
        self.assertIsNone(parse_id('openclaw:../../escape:'+SID))
        self.assertEqual(self.reader.collect('autoclaw:'+SID),[])
        self.con.close(); self.db.unlink(); outside=self.home/'outside.sqlite';outside.touch(); self.db.symlink_to(outside)
        self.assertEqual(self.reader.collect(),[])

    def test_scan_is_read_only(self):
        self.event(); before=self.db.read_bytes(); self.reader.collect()
        self.assertEqual(self.db.read_bytes(),before)
