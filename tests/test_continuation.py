import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from supervisor.bridge import Blocked, Journal, MESSAGE, MacBackend, perform, validate_snapshot, grok_target, zcode_target, qoder_target, qoder_chat_url


class Backend:
    def __init__(self):
        self.value = ''
        self.identity = True
        self.busy = False
        self.writes = self.sends = self.guards = 0
        self.fail_guard = None
        self.change_after_write = False
        self.written = []

    def guard(self, **_):
        self.guards += 1
        if self.fail_guard:
            raise Blocked(self.fail_guard)

    def route(self):
        pass

    def snapshot(self):
        return dict(identity=self.identity, busy=self.busy, input_count=1,
                    value=self.value, send_exists=True, send_enabled=True)

    def write(self, message):
        self.writes += 1
        self.written.append(message)
        self.value = message
        if self.change_after_write:
            self.identity = False

    def send(self):
        self.sends += 1


class ContinuationTests(unittest.TestCase):
    def test_qoder_identity_binds_uuid_title_and_workspace(self):
        import sqlite3
        sid = '9a249604-577a-448a-8c1c-1414e4fd29f7'
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / 'main.sqlite'
            with sqlite3.connect(db_path) as db:
                db.execute('CREATE TABLE chat_sessions (session_id TEXT, title TEXT, cwd TEXT, archived INTEGER, deleted_at TEXT, session_kind TEXT, owner_session_id TEXT)')
                db.execute('INSERT INTO chat_sessions VALUES (?,?,?,0,NULL,\'standard\',NULL)',
                           (sid, '任务标题', '/work/项目'))
            request = dict(id='qoder-cn:' + sid, navigation_key=sid,
                           title='任务标题', navigation_title='任务标题', project='/work/项目')
            self.assertEqual(qoder_target(request, db_path), (sid, '任务标题', '项目', '/work/项目'))
            self.assertIn('/chat/' + sid + '?surface=conversation', qoder_chat_url(sid))
            for change in ({'id': 'qoder-cn:other'}, {'navigation_key': 'other'},
                           {'title': '别的标题'}, {'navigation_title': '别的标题'},
                           {'project': '/work/别的项目'}):
                with self.assertRaisesRegex(Blocked, 'target_unverified'):
                    qoder_target(dict(request, **change), db_path)
            with sqlite3.connect(db_path) as db:
                db.execute('UPDATE chat_sessions SET archived=1 WHERE session_id=?', (sid,))
            with self.assertRaisesRegex(Blocked, 'target_unverified'):
                qoder_target(request, db_path)

    def test_zcode_index_identity_requires_unique_title_and_project(self):
        import hashlib
        import sqlite3
        db_path = Path(tempfile.mkdtemp()) / 'tasks.sqlite'
        try:
            db = sqlite3.connect(db_path)
            db.execute('CREATE TABLE tasks (workspace_key TEXT, workspace_path TEXT, task_id TEXT, title TEXT, deleted INTEGER, archived INTEGER)')
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,0,0)', ('key', '/w/Project', 'id', 'Unique task'))
            db.commit()
            request = dict(id='zcode:' + hashlib.sha256(b'key').hexdigest()[:12] + ':id',
                           project='/w/Project', title='Unique task')
            self.assertEqual(zcode_target(request, db_path), ('Unique task', 'Project', '/w/Project'))
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,0,0)', ('another', '/x/Other', 'id2', 'Unique task'))
            db.commit()
            self.assertEqual(zcode_target(request, db_path), ('Unique task', 'Project', '/w/Project'))
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,0,0)', ('third', '/w/Project', 'id3', 'Unique task'))
            db.commit()
            with self.assertRaisesRegex(Blocked, 'target_unverified'):
                zcode_target(request, db_path)
            db.close()
        finally:
            db_path.unlink(missing_ok=True)
            db_path.parent.rmdir()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.temp.name))
        self.backend = Backend()
        self.request = dict(id='app:chat', key='app:chat:turn1', mode='send')

    def tearDown(self):
        self.journal.lock.close()
        self.temp.cleanup()

    def run_request(self):
        return perform(self.request, self.backend, self.journal)

    def run_code(self):
        # Mirrors main(): a Blocked failure becomes a bounded result dict.
        try:
            return perform(self.request, self.backend, self.journal)
        except Blocked as error:
            return {'code': str(error), 'attempted': self.backend.writes > 0}

    def test_exact_fixed_message_once(self):
        result = self.run_request()
        self.assertEqual(result['code'], 'sent_pending_confirmation')
        self.assertEqual(self.backend.value, '刚才中断了，请继续')
        self.assertEqual((self.backend.writes, self.backend.sends), (1, 1))
        self.assertGreaterEqual(self.backend.guards, 4)
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.run_request()

    def test_custom_text_replaces_message_for_write_and_readback(self):
        self.request['text'] = '  请从中断的那一步继续  '
        result = self.run_request()
        self.assertEqual(result['code'], 'sent_pending_confirmation')
        # Whitespace stripped, written verbatim, and it is the only write.
        self.assertEqual(self.backend.written, ['请从中断的那一步继续'])
        self.assertFalse(any(MESSAGE in w for w in self.backend.written))
        # A successful send proves readback compared against the custom text:
        # comparing against MESSAGE would have raised input_changed.

    def test_completed_notice_manual_continue_is_single_exact_send(self):
        self.request.update(kind='followup', manual_retry=True, automation_mode='collaboration',
                            started_at=123, text='请继续', overwrite_draft=True)
        self.backend.value = '旧草稿'
        self.assertEqual(self.run_request()['code'], 'sent_pending_confirmation')
        self.assertEqual(self.backend.written, ['请继续'])
        self.assertEqual(self.backend.sends, 1)
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.run_request()

    def test_missing_or_blank_text_falls_back_to_fixed_message(self):
        cases = [None, '', ' \t\n  ']
        for index, text in enumerate(cases):
            # Fresh session per case: the journal caps attempts per session id.
            self.request['id'] = self.request['key'] = 'app:chat:fall%d' % index
            if text is None:
                self.request.pop('text', None)
            else:
                self.request['text'] = text
            self.backend = Backend()  # Fresh composer per case, as setUp provides.
            self.assertEqual(self.run_request()['code'], 'sent_pending_confirmation')
            self.assertEqual(self.backend.written, [MESSAGE])
        self.assertEqual(len(cases), 3)

    def test_text_over_2000_chars_rejected_without_attempt(self):
        self.request['text'] = '字' * 2001
        self.assertEqual(self.run_code(), {'code': 'text_invalid', 'attempted': False})
        self.assertEqual((self.backend.writes, self.backend.sends), (0, 0))
        self.assertEqual(self.backend.written, [])
        self.assertFalse(self.journal.rows)
        self.request['key'] = 'app:chat:turn1:max'
        self.request['text'] = '字' * 2000
        self.assertEqual(self.run_request()['code'], 'sent_pending_confirmation')
        self.assertEqual(self.backend.value, '字' * 2000)

    def test_multiline_text_is_written(self):
        self.request['text'] = '刚才中断了。\n请从上一步继续。\n谢谢'
        result = self.run_request()
        self.assertEqual(result['code'], 'sent_pending_confirmation')
        self.assertEqual(self.backend.written, ['刚才中断了。\n请从上一步继续。\n谢谢'])
        self.assertEqual((self.backend.writes, self.backend.sends), (1, 1))

    def test_crlf_readback_matches_multiline_message(self):
        # 部分 contenteditable 以 \r\n 回读；归一化后不得误判 input_changed。
        self.request['text'] = '第一行\n第二行'

        class CRLFBackend(Backend):
            def write(self, message):
                self.writes += 1
                self.written.append(message)
                self.value = message.replace('\n', '\r\n')

        self.backend = CRLFBackend()
        result = self.run_request()
        self.assertEqual(result['code'], 'sent_pending_confirmation')
        self.assertEqual((self.backend.writes, self.backend.sends), (1, 1))

    def test_text_with_control_characters_is_rejected(self):
        bad_texts = ['请继续\r重来', '请\x00继续', 'a\x0bb', 'a\x1fb', '删\x7f除']
        for index, text in enumerate(bad_texts):
            self.request['key'] = 'app:chat:ctrl%d' % index
            self.request['text'] = text
            result = self.run_code()
            self.assertEqual(result, {'code': 'text_invalid', 'attempted': False})
        self.assertEqual(self.backend.written, [])
        self.assertFalse(self.journal.rows)

    def test_draft_requires_explicit_overwrite(self):
        self.backend.value = '用户未发草稿'
        with self.assertRaisesRegex(Blocked, 'draft_present'):
            self.run_request()
        self.assertEqual(self.backend.value, '用户未发草稿')
        self.assertEqual(self.backend.writes, 0)

    def test_authorized_draft_and_placeholder_are_replaced(self):
        for index, draft in enumerate(('用户未发草稿', '\ufeff\nWhat can I help you with today?')):
            self.request.update(id='test:%s' % index, key='test:%s:turn' % index, overwrite_draft=True)
            self.backend = Backend()
            self.backend.value = draft
            self.assertEqual(self.run_request()['code'], 'sent_pending_confirmation')
            self.assertEqual(self.backend.value, MESSAGE)
            self.assertEqual((self.backend.writes, self.backend.sends), (1, 1))

    def test_overwrite_does_not_relax_identity_busy_or_readability(self):
        self.request['overwrite_draft'] = True
        self.backend.value = '草稿'
        self.backend.identity = False
        self.assertEqual(self.run_code()['code'], 'target_unverified')
        self.backend.identity = True
        self.backend.busy = True
        self.assertEqual(self.run_code()['code'], 'already_running')
        self.backend.busy = False
        self.backend.value = None
        self.assertEqual(self.run_code()['code'], 'composer_unreadable')
        self.assertEqual(self.backend.writes, 0)

    def test_draft_changed_during_preflight_is_not_overwritten(self):
        self.request['overwrite_draft'] = True
        calls = [0]
        original = self.backend.snapshot
        def changing():
            calls[0] += 1
            if calls[0] == 2:
                self.backend.value = '刚输入的新草稿'
            return original()
        self.backend.snapshot = changing
        self.assertEqual(self.run_code()['code'], 'draft_present')
        self.assertEqual(self.backend.writes, 0)
        self.assertFalse(self.journal.rows)

    def test_wrong_conversation(self):
        self.backend.identity = False
        with self.assertRaisesRegex(Blocked, 'target_unverified'):
            self.run_request()
        self.assertEqual(self.backend.writes, 0)

    def test_switch_after_input_cannot_send(self):
        self.backend.change_after_write = True
        with self.assertRaisesRegex(Blocked, 'target_unverified'):
            self.run_request()
        self.assertEqual(self.backend.sends, 0)
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.run_request()

    def test_active_task_not_sent(self):
        self.backend.busy = True
        with self.assertRaisesRegex(Blocked, 'already_running'):
            self.run_request()
        self.assertEqual(self.backend.writes, 0)

    def test_environment_guard_blocks_without_reserving(self):
        for error in ('locked', 'permission_required', 'user_active', 'state_changed', 'user_stopped'):
            self.backend.fail_guard = error
            with self.assertRaisesRegex(Blocked, error):
                self.run_request()
        self.assertFalse(self.journal.rows)
        self.assertEqual(self.backend.writes, 0)

    def test_check_mode_does_not_write_or_reserve(self):
        self.request['mode'] = 'check'
        self.assertEqual(self.run_request()['code'], 'ready')
        self.assertEqual(self.backend.writes, 0)
        self.assertFalse(self.journal.rows)

    def test_persisted_attempt_survives_restart(self):
        self.journal.reserve(self.request)
        self.journal.lock.close()
        self.journal = Journal(Path(self.temp.name))
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.run_request()

    def test_global_lock(self):
        with self.assertRaisesRegex(Blocked, 'another_recovery'):
            Journal(Path(self.temp.name))

    def test_cooldown_and_hourly_caps(self):
        self.journal.reserve(self.request)
        self.request['key'] += 'next'
        with self.assertRaisesRegex(Blocked, 'cooldown'):
            self.run_request()

    def test_corrupt_journal_blocks(self):
        self.journal.lock.close()
        self.journal.path.write_text('{broken')
        with self.assertRaisesRegex(Blocked, 'journal_unreadable'):
            Journal(Path(self.temp.name))

    def test_read_error_is_not_empty(self):
        with self.assertRaisesRegex(Blocked, 'composer_unreadable'):
            validate_snapshot(dict(identity=True, input_count=1, value=None), '')
        with self.assertRaisesRegex(Blocked, 'composer_unreadable'):
            validate_snapshot(dict(identity=True, input_count=2, value=''), '')


class RageBridgeTests(unittest.TestCase):
    setUp = ContinuationTests.setUp
    tearDown = ContinuationTests.tearDown
    run_request = ContinuationTests.run_request
    run_code = ContinuationTests.run_code
    def test_rage_fixed_interruption_and_double_click(self):
        self.request.update(automation_mode='rage', started_at=100, kind='interrupt', text='错误的可配置文本')
        self.assertEqual(self.run_request()['code'], 'sent_pending_confirmation')
        self.assertEqual(self.backend.written, [MESSAGE])
        self.assertEqual(self.run_code()['code'], 'already_attempted')
        self.assertEqual(self.backend.sends, 1)

    def test_rage_over_hourly_limit_and_old_round_after_compaction(self):
        self.request.update(automation_mode='rage', kind='followup')
        with patch('supervisor.bridge.time.time', return_value=100000) as clock:
            for turn in range(20):
                clock.return_value = 100000 + turn * 5
                self.request.update(key='round:' + str(turn), started_at=turn + 1)
                self.journal.reserve(self.request)
        # Compaction of recent rows cannot erase the latest-round watermark.
        self.journal.rows = []
        self.request.update(key='round:0:changed-suffix', started_at=1)
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.journal.check(self.request)
        self.journal.lock.close()
        self.journal = Journal(Path(self.temp.name))
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.journal.check(self.request)

    def test_rage_followup_cooldown_does_not_delay_new_interruption(self):
        self.request.update(automation_mode='rage', kind='followup', started_at=1)
        with patch('supervisor.bridge.time.time', return_value=10000) as clock:
            self.journal.reserve(self.request)
            self.request.update(key='round2', started_at=2)
            clock.return_value = 10002
            with self.assertRaises(Blocked) as failure:
                self.journal.check(self.request)
            self.assertEqual(str(failure.exception), 'cooldown')
            self.assertEqual(failure.exception.retry_after, 3)
            self.request['kind'] = 'interrupt'
            self.journal.check(self.request)
            self.journal.reserve(self.request)
            with self.assertRaisesRegex(Blocked, 'already_attempted'):
                self.journal.check(self.request)

    def test_rapid_interrupted_new_rounds_send_once_each_without_cooldown(self):
        self.request.update(automation_mode='rage', kind='interrupt', overwrite_draft=True)
        with patch('supervisor.bridge.time.time', return_value=10000):
            for turn in range(12):
                self.request.update(key='interrupt:' + str(turn), started_at=turn + 1)
                self.assertEqual(self.run_request()['code'], 'sent_pending_confirmation')
                with self.assertRaisesRegex(Blocked, 'already_attempted'):
                    self.run_request()
        self.assertEqual(self.backend.written, [MESSAGE] * 12)
        self.assertEqual(self.backend.sends, 12)
        self.journal.lock.close()
        self.journal = Journal(Path(self.temp.name))
        self.request.update(key='interrupt:0:relabelled', started_at=1)
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.run_request()

    def test_invalid_rage_round_cannot_write(self):
        self.request.update(automation_mode='rage')
        self.assertEqual(self.run_code()['code'], 'invalid_request')
        self.assertEqual(self.backend.writes, 0)


    def test_maximum_legal_journal_is_readable_and_does_not_evict_sessions(self):
        from supervisor.bridge import digest
        self.journal.latest = {digest(str(i)): dict(key=digest('key' + str(i)), start=1000000 + i) for i in range(10000)}
        self.journal.rows = [dict(key=digest('row' + str(i)), session=digest('0'), time=1, start=1) for i in range(4096)]
        self.journal.path.write_text(json.dumps(dict(version=2, latest=self.journal.latest, rows=self.journal.rows)))
        self.assertLess(self.journal.path.stat().st_size, 4 * 1024 * 1024)
        self.journal.lock.close()
        self.journal = Journal(Path(self.temp.name))
        self.request.update(automation_mode='rage', started_at=2000000, key='fresh', id='fresh-session')
        with self.assertRaisesRegex(Blocked, 'journal_capacity'):
            self.journal.check(self.request)
        self.assertEqual(len(self.journal.latest), 10000)


class LegacyJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'continuation-attempts.json'
        self.journal = None

    def tearDown(self):
        if self.journal: self.journal.lock.close()
        self.temp.cleanup()

    def load(self, data=None):
        if self.journal: self.journal.lock.close()
        if data is not None: self.path.write_text(json.dumps(data))
        self.journal = Journal(self.root)
        return self.journal

    def old(self, key='old:turn', sid='old:chat', timestamp=1):
        from supervisor.bridge import digest
        return dict(key=digest(key), session=digest(sid), time=timestamp)

    def test_expired_v1_digest_survives_save_and_restart(self):
        self.load([self.old()])
        self.journal.reserve(dict(id='new:chat', key='new:round', started_at=100, automation_mode='rage'))
        self.assertNotIn(self.old(), self.journal.rows)
        self.load()
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.journal.check(dict(id='old:chat', key='old:turn', started_at=1, automation_mode='rage'))
        self.assertEqual(len(self.journal.legacy), 1)

    def test_v1_ring_eviction_cannot_drop_old_digest(self):
        now = time.time() - 100
        old = [self.old(key='key:' + str(i), sid='chat:' + str(i), timestamp=now) for i in range(4100)]
        self.load(old)
        self.journal.reserve(dict(id='new', key='new', started_at=1, automation_mode='rage'))
        self.assertEqual(len(self.journal.rows), 4096)
        self.load()
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.journal.check(dict(id='chat:0', key='key:0', started_at=1, automation_mode='rage'))
        self.assertEqual(len(self.journal.legacy), 4100)

    def test_verified_plain_stable_key_uses_watermark(self):
        from supervisor.bridge import digest
        sid = 'grok:abc'
        key = sid + ':1700000000000'
        self.load([dict(session=digest(sid), key=key, time=1)])
        self.assertEqual(self.journal.latest[digest(sid)]['start'], 1700000000)
        self.assertFalse(self.journal.legacy)
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.journal.check(dict(id=sid, key=key, started_at=1700000000, automation_mode='rage'))
        self.journal.reserve(dict(id=sid, key=sid + ':1700000001000', started_at=1700000001, automation_mode='rage'))
        self.load()
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.journal.check(dict(id=sid, key=key, started_at=1700000000, automation_mode='rage'))

    def test_existing_v2_hash_without_start_is_preserved(self):
        from supervisor.bridge import digest
        self.load(dict(version=2, rows=[self.old()], latest={digest('old:chat'): dict(key=digest('later'), start=2)}))
        self.assertIn(digest('old:turn'), self.journal.legacy)
        self.journal.reserve(dict(id='another', key='another', started_at=3, automation_mode='rage'))
        self.load()
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.journal.check(dict(id='old:chat', key='old:turn'))

    def test_long_run_keeps_one_watermark_not_per_round_tombstones(self):
        self.load([])
        with patch('supervisor.bridge.time.time', return_value=10000) as clock:
            for index in range(30):
                clock.return_value += 5
                self.journal.reserve(dict(id='same', key='turn:' + str(index), started_at=index + 1, automation_mode='rage', kind='followup'))
                self.load()
        self.assertEqual(len(self.journal.latest), 1)
        self.assertFalse(self.journal.legacy)

    def test_malformed_migration_refuses_without_rewriting(self):
        from supervisor.bridge import digest
        malformed = [
            [dict(key='unknown-format', session=digest('s'), time=1)],
            [dict(key='s:1700000000000', session=digest('other'), time=1)],
            [dict(key=digest('k'), session='invalid', time=1)],
            dict(version=2, rows=[], latest={}, legacy=['invalid']),
            dict(version=99, rows=[], latest={}),
            dict(version=2, rows=[dict(self.old(), start=None)], latest={}),
        ]
        for data in malformed:
            self.path.write_text(json.dumps(data))
            original = self.path.read_bytes()
            with self.assertRaisesRegex(Blocked, 'journal_unreadable'): Journal(self.root)
            self.assertEqual(self.path.read_bytes(), original)

    def test_legacy_capacity_refuses_new_identity_without_dropping_data(self):
        from supervisor.bridge import digest
        legacy = [digest('old:' + str(i)) for i in range(10000)]
        self.load(dict(version=2, rows=[], latest={}, legacy=legacy))
        original = self.path.read_bytes()
        with self.assertRaisesRegex(Blocked, 'journal_capacity'):
            self.journal.reserve(dict(id='new', key='new', started_at=1, automation_mode='rage'))
        self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.journal.check(dict(id='old', key='old:0'))
        self.journal.lock.close()
        self.journal = None
        self.path.write_text(json.dumps(dict(version=2, rows=[], latest={}, legacy=legacy + [digest('overflow')])))
        with self.assertRaisesRegex(Blocked, 'journal_capacity'): Journal(self.root)


class GrokMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = [dict(id='sid', title='完整会话标题', projectId='p')]
        self.projects = [dict(id='p', name='工程', path='/tmp/project')]
        self.request = dict(id='grok:sid', title='完整会话标题')
        self.save()

    def tearDown(self): self.temp.cleanup()

    def save(self):
        (self.root / 'sessions_index.json').write_text(json.dumps(self.sessions))
        (self.root / 'projects.json').write_text(json.dumps(self.projects))

    def test_stable_id_full_title_and_unique_project(self):
        self.assertEqual(grok_target(self.request, self.root), ('完整会话标题', '工程', '/tmp/project'))
        for mutation in [dict(id='grok:wrong'), dict(title='完整会话…')]:
            with self.assertRaisesRegex(Blocked, 'target_unverified'):
                grok_target(dict(self.request, **mutation), self.root)

    def test_duplicate_title_or_project_name_cannot_route(self):
        self.sessions.append(dict(id='other', title='完整会话标题', projectId='another'))
        self.save()
        with self.assertRaisesRegex(Blocked, 'target_unverified'): grok_target(self.request, self.root)
        self.sessions.pop()
        self.projects.append(dict(id='other', name='工程', path='/tmp/other'))
        self.save()
        with self.assertRaisesRegex(Blocked, 'target_unverified'): grok_target(self.request, self.root)

    def test_display_alias_is_rejected_if_another_session_could_match(self):
        self.sessions[0]['title'] = '继续优化全量的去更新。'
        self.request['title'] = self.sessions[0]['title']
        self.sessions.append(dict(id='other', title='继续优化全量去更新', projectId='other'))
        self.save()
        with self.assertRaisesRegex(Blocked, 'target_unverified'):
            grok_target(self.request, self.root)


class AXSelectionTests(unittest.TestCase):
    def backend(self, app='autoclaw'):
        class AX:
            @staticmethod
            def rect_of(n): return n.get('rect')
            @staticmethod
            def element_name(n): return n.get('label', '')
            @staticmethod
            def get_attr(n, k, default=None): return n.get(k, default)
            @staticmethod
            def children(n): return n.get('children', [])
            @staticmethod
            def role_description(n): return n.get('description', '')
            @staticmethod
            def placeholder(n): return n.get('placeholder', '')
            @staticmethod
            def enabled(n): return n.get('enabled', True)
            @staticmethod
            def get(n, k): return (n.get('err', 0), n.get(k))
            @staticmethod
            def trusted(): return True
        b = object.__new__(MacBackend)
        b.ax = AX
        b.window = SimpleNamespace(rect=(0, 0, 1000, 800), pid=123)
        b.request = {'app_id': app}
        b.title = '正确会话'
        b.winops = SimpleNamespace(frontmost_pid=lambda: 123, user_active=lambda _: False)
        return b

    def test_navigation_ready_returns_without_fixed_sleep(self):
        b = self.backend()
        b.guard = lambda **_: None
        nodes = [object()]
        b.nodes = lambda: nodes
        b.current_identity = lambda value: value is nodes
        with patch('supervisor.bridge.time.sleep') as sleep:
            self.assertIs(b.navigation_nodes(0.6), nodes)
        sleep.assert_not_called()

    def test_navigation_waits_for_exact_identity_and_retains_deadline(self):
        b = self.backend()
        b.guard = lambda **_: None
        now = [0.0]
        b.nodes = lambda: ['target' if now[0] >= 0.15 else 'wrong']
        b.current_identity = lambda nodes: nodes == ['target']
        with patch('supervisor.bridge.time.monotonic', side_effect=lambda: now[0]), \
             patch('supervisor.bridge.time.sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            self.assertEqual(b.navigation_nodes(0.6), ['target'])
            self.assertLess(now[0], 0.2)
            b.nodes = lambda: ['wrong']
            begin = now[0]
            self.assertEqual(b.navigation_nodes(0.45), ['wrong'])
            self.assertAlmostEqual(now[0] - begin, 0.45)

    def test_navigation_focus_loss_aborts_before_reading_other_window(self):
        b = self.backend()
        b.guard = lambda **_: (_ for _ in ()).throw(Blocked('focus_changed'))
        b.nodes = lambda: self.fail('must not read after losing target focus')
        with self.assertRaisesRegex(Blocked, 'focus_changed'):
            b.navigation_nodes(0.6)

    def test_paste_ready_has_no_sleep_and_slow_readback_has_time_deadline(self):
        b = self.backend()
        b.guard = lambda **_: None
        b.inject = SimpleNamespace(hotkey=lambda _: None)
        cleared = []
        board = SimpleNamespace(pasteboardItems=lambda: [], clearContents=lambda: cleared.append(True),
                                setString_forType_=lambda *_: None, changeCount=lambda: 1)
        b.AppKit = SimpleNamespace(NSPasteboard=SimpleNamespace(generalPasteboard=lambda: board),
                                   NSPasteboardTypeString='public.utf8-plain-text')
        b.snapshot = lambda: dict(identity=True, busy=False, input_count=1, value=MESSAGE, send_enabled=True)
        with patch('supervisor.bridge.time.sleep') as sleep:
            b.paste_verified(MESSAGE)
        sleep.assert_not_called()
        self.assertEqual(len(cleared), 2)
        now, calls = [0.0], []
        def slow_snapshot():
            now[0] += 0.7
            calls.append(True)
            return dict(identity=True, busy=False, input_count=1, value='', send_enabled=False)
        b.snapshot = slow_snapshot
        with patch('supervisor.bridge.time.monotonic', side_effect=lambda: now[0]), \
             patch('supervisor.bridge.time.sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            with self.assertRaisesRegex(Blocked, 'input_failed'):
                b.paste_verified(MESSAGE)
        self.assertEqual(len(calls), 3)
        self.assertLess(now[0], 2.7)  # Two-second budget plus one in-flight AX read.
        self.assertEqual(len(cleared), 4)  # Clipboard restored even on timeout.

    def test_current_header_cannot_be_sidebar_or_transcript(self):
        b = self.backend()
        def heading(rect, label='正确会话'):
            return ({'rect': rect, 'AXValue': 1, 'label': label}, 'AXHeading')
        self.assertTrue(b.current_identity([heading((310, 70, 450, 100))]))
        self.assertFalse(b.current_identity([heading((10, 70, 150, 100))]))
        self.assertFalse(b.current_identity([heading((310, 450, 450, 480))]))
        self.assertFalse(b.current_identity([heading((310, 70, 450, 100), '别的会话')]))

    def test_placeholder_needs_declared_exact_match_and_send_is_unique(self):
        b = self.backend()
        header = ({'rect': (310, 70, 450, 100), 'AXValue': 1, 'label': '正确会话'}, 'AXHeading')
        box = ({'rect': (310, 650, 880, 710), 'AXValue': '输入消息', 'placeholder': '输入消息'}, 'AXTextArea')
        send = ({'rect': (850, 730, 880, 760), 'label': '发送消息', 'enabled': False}, 'AXButton')
        nodes = [header, box, send]
        b.nodes = lambda: nodes
        snapshot = b.snapshot()
        self.assertEqual(snapshot['value'], '')
        self.assertTrue(snapshot['send_exists'])
        self.assertFalse(snapshot['send_enabled'])
        box[0].pop('placeholder')
        self.assertEqual(b.snapshot()['value'], '输入消息')
        nodes.append(send)
        self.assertFalse(b.snapshot()['send_exists'])

    def test_workbuddy_replaces_draft_without_axvalue(self):
        b = self.backend('workbuddy-ai')
        b.request['overwrite_draft'] = True
        b.did_write = False
        b.box = {'AXFocused': True}
        def reject_ax_write(*args):
            self.fail('WorkBuddy must not use AXValue')
        b.ax.set_attr = reject_ax_write
        b.ax.focus = lambda _: True
        b.guard = lambda **kw: None
        value = ['\ufeff\nWhat can I help you with today?']
        actions = []
        b.snapshot = lambda: dict(identity=True, busy=False, input_count=1, value=value[0], send_enabled=value[0] == MESSAGE)
        def paste(text, **kw):
            actions.append('paste')
            value[0] = text
            return True
        b.inject = SimpleNamespace(hotkey=lambda key: actions.append(('select-all', key)))
        b.paste_verified = paste
        b.focus_composer = lambda: None
        b.write(MESSAGE)
        self.assertEqual(actions, [('select-all', 0), 'paste'])
        self.assertEqual(value[0], MESSAGE)
        self.assertTrue(b.did_write)

    def test_focus_click_stays_inside_the_verified_editor(self):
        b = self.backend('workbuddy-ai')
        b.box = {'rect': (310, 650, 880, 710), 'AXFocused': True}
        clicks = []
        b.inject = SimpleNamespace(click_at=lambda x, y: clicks.append((x, y)))
        b.focus_composer()
        self.assertEqual(clicks, [(595, 680)])
        b.box['rect'] = None
        with self.assertRaisesRegex(Blocked, 'composer_unreadable'):
            b.focus_composer()
        self.assertEqual(len(clicks), 1)

    def test_readback_ignores_editor_boundary_sentinel(self):
        b = self.backend()
        b.current_identity = lambda _: True
        b.nodes = lambda: [({'rect': (310, 650, 880, 710), 'AXValue': '\ufeff' + MESSAGE + '\ufeff'}, 'AXTextArea')]
        self.assertEqual(b.snapshot()['value'], MESSAGE)

    def test_permission_and_real_quartz_console_key_fail_closed(self):
        b = self.backend()
        state = {'kCGSSessionOnConsoleKey': True}
        b.Quartz = SimpleNamespace(kCGSessionOnConsoleKey='kCGSSessionOnConsoleKey',
                                   CGSessionCopyCurrentDictionary=lambda: state)
        b.guard()
        state['CGSSessionScreenIsLocked'] = True
        with self.assertRaisesRegex(Blocked, 'locked'): b.guard()
        state.clear()
        with self.assertRaisesRegex(Blocked, 'locked'): b.guard()

    def test_similarly_named_sidebar_cannot_match(self):
        b = self.backend()
        self.assertTrue(b.sidebar_match('正确会话 有未查看回复'))
        self.assertTrue(b.sidebar_match('正确会话 18:23'))
        self.assertFalse(b.sidebar_match('正确会话的备份'))
        self.assertFalse(b.sidebar_match('正确会话 backup'))


    def test_workbuddy_ai_observed_minute_day_age_preserves_full_title_matching(self):
        b = self.backend('workbuddy-ai')
        self.assertTrue(b.sidebar_match('正确会话'))
        self.assertTrue(b.sidebar_match('正确会话 2d'))
        self.assertTrue(b.sidebar_match('正确会话 10d'))
        self.assertTrue(b.sidebar_match('正确会话 1m'))
        self.assertTrue(b.sidebar_match('正确会话 47m'))
        for value in ('正确会… 2d', '正确会话的副本 2d', '正确会话 2d backup',
                      '未读 正确会话 2d', '正确会话 More Archive Pin', '正确会话 2h',
                      '正确会… 1m', '正确会话 47m backup', '正确会话 1month'):
            self.assertFalse(b.sidebar_match(value), value)
        other = self.backend('workbuddy')
        self.assertFalse(other.sidebar_match('正确会话 2d'))

    def test_grok_identity_requires_heading_and_active_unique_project(self):
        b = self.backend('grok')
        b.grok_identity = ('正确会话', '工程', '/tmp/project')
        header = ({'rect': (310, 70, 450, 100), 'AXValue': 1, 'label': '正确会话'}, 'AXHeading')
        child = ({'rect': (310, 70, 450, 100), 'label': '正确会话'}, 'AXStaticText')
        project = ({'rect': (400, 700, 500, 750), 'label': '工程'}, 'AXPopUpButton')
        self.assertTrue(b.current_identity([header, child, project]))
        self.assertFalse(b.current_identity([header, child]))
        self.assertFalse(b.current_identity([header, child, project, project]))
        header[0]['label'] = '错误会话'
        self.assertFalse(b.current_identity([header, child, project]))
        self.assertTrue(b.sidebar_match('正确会话 5小时前'))
        self.assertTrue(b.sidebar_match('正确会话 现在'))
        self.assertTrue(b.sidebar_match('正确会话 50秒钟前'))
        self.assertFalse(b.sidebar_match('正确会话 现在副本'))
        self.assertFalse(b.sidebar_match('未读 正确会话 现在'))
        self.assertFalse(b.sidebar_match('正确会… 50秒钟前'))
        self.assertFalse(b.sidebar_match('正确会话 副本'))

    def test_grok_unread_badge_and_observed_title_edit(self):
        b = self.backend('grok')
        b.title = '继续优化全量的去更新。'
        b.grok_identity = (b.title, '黑洞吞噬', '/tmp/project')
        header = ({'rect': (310, 70, 450, 100), 'AXValue': 1,
                   'label': '继续优化全量去更新'}, 'AXHeading')
        project = ({'rect': (400, 700, 500, 750), 'label': '黑洞吞噬'}, 'AXPopUpButton')
        self.assertTrue(b.sidebar_match('未读 — 后台回合已完成 继续优化全量去更新 33分钟前'))
        self.assertTrue(b.current_identity([header, project]))
        self.assertFalse(b.sidebar_match('未读 — 后台回合已完成 继续优化全量去更新 副本'))
        self.assertFalse(b.sidebar_match('未读 — 后台回合已完成 继续优化全量去更新 33分钟前 备份'))
        self.assertFalse(b.current_identity([header, project, project]))

    def test_grok_broken_ui_refuses_composer(self):
        b = self.backend('grok')
        b.nodes = lambda: [({'label': '已断开'}, 'AXStaticText')]
        with self.assertRaisesRegex(Blocked, 'app_ui_unavailable'): b.snapshot()

    def test_grok_uses_verified_paste_and_overwrites_draft(self):
        b = self.backend('grok')
        b.request['overwrite_draft'] = True
        b.box = {'AXFocused': True}
        b.guard = lambda **kw: None
        b.ax.set_attr = lambda *args: self.fail('Grok must use native paste')
        value = ['已有草稿']
        b.snapshot = lambda: dict(identity=True, busy=False, input_count=1, value=value[0], send_enabled=value[0] == MESSAGE)
        b.focus_composer = lambda: None
        actions = []
        b.inject = SimpleNamespace(hotkey=lambda k: actions.append(k))
        b.paste_verified = lambda text: value.__setitem__(0, text)
        b.write(MESSAGE)
        self.assertEqual(value[0], MESSAGE)
        self.assertEqual(actions, [0])


    def test_fresh_guard_rage_completed_only_and_manual_interrupt_reason(self):
        b = self.backend('codex')
        b.request.update(id='codex:test', started_at=123, kind='followup', automation_mode='rage')
        state = {'kCGSSessionOnConsoleKey': True}
        b.Quartz = SimpleNamespace(kCGSessionOnConsoleKey='kCGSSessionOnConsoleKey', CGSessionCopyCurrentDictionary=lambda: state)
        row = dict(id='codex:test', started_at=123, status='idle')
        with patch.dict('sys.modules', {'codex_adapter': SimpleNamespace(collect=lambda: [row])}):
            with self.assertRaisesRegex(Blocked, 'state_changed'): b.guard(fresh=True)
            row['status'] = 'completed'
            b.guard(fresh=True)
            b.request.update(kind='interrupt', manual_retry=True)
            row.update(status='interrupted', status_reason='中止事件，触发者待确认')
            b.guard(fresh=True)
            b.request['manual_retry'] = False
            with self.assertRaisesRegex(Blocked, 'manual_resolution'): b.guard(fresh=True)
            b.request['manual_retry'] = True
            row['status_reason'] = '认证失败'
            with self.assertRaisesRegex(Blocked, 'manual_resolution'): b.guard(fresh=True)
            for stopped in ('用户主动取消', '用户停止', 'user cancelled'):
                row['status_reason'] = stopped
                with self.assertRaisesRegex(Blocked, 'user_stopped'): b.guard(fresh=True)
            row.update(status_reason='', user_stopped=True)
            with self.assertRaisesRegex(Blocked, 'user_stopped'): b.guard(fresh=True)
            row.update(user_stopped=False, status='running')
            with self.assertRaisesRegex(Blocked, 'state_changed'): b.guard(fresh=True)

    def test_fresh_guard_binds_reviewed_project_source_and_route(self):
        b = self.backend('codex')
        b.request.update(id='codex:test', started_at=123, kind='followup', automation_mode='rage',
                         project='/reviewed', source='local-session', target='codex://threads/test')
        b.Quartz = SimpleNamespace(kCGSessionOnConsoleKey='on', CGSessionCopyCurrentDictionary=lambda: {'on': True})
        row = dict(id='codex:test', started_at=123, status='completed', project='/reviewed',
                   source='local-session', target='codex://threads/test')
        with patch.dict('sys.modules', {'codex_adapter': SimpleNamespace(collect=lambda: [row])}):
            b.guard(fresh=True)
            for field in ('project', 'source', 'target'):
                original = row[field]
                row[field] = 'changed'
                with self.assertRaisesRegex(Blocked, 'target_unverified'):
                    b.guard(fresh=True)
                row[field] = original
            b.guard(fresh=True, after_write=True)


if __name__ == '__main__':
    unittest.main()
