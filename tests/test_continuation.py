import json
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
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
            standard = dict(request, id='qoder:' + sid, app_id='qoder')
            self.assertEqual(qoder_target(standard, db_path), (sid, '任务标题', '项目', '/work/项目'))
            self.assertTrue(qoder_chat_url(sid, 'qoder').startswith('qoder-app://'))
            with self.assertRaisesRegex(Blocked, 'target_unverified'):
                qoder_target(dict(standard, app_id='qoder-cn'), db_path)
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
            self.assertEqual(zcode_target(request, db_path), ('Unique task', 'Project', '/w/Project', True))
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,0,0)', ('another', '/x/Other', 'id2', 'Unique task'))
            db.commit()
            self.assertEqual(zcode_target(request, db_path), ('Unique task', 'Project', '/w/Project', False))
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,0,0)', ('third', '/w/Project', 'id3', 'Unique task'))
            db.commit()
            with self.assertRaisesRegex(Blocked, 'session_title_ambiguous'):
                zcode_target(request, db_path)
            db.close()
        finally:
            db_path.unlink(missing_ok=True)
            db_path.parent.rmdir()

    def test_qoder_named_workspace_is_resolved_by_exact_session_id(self):
        import sqlite3
        sid = '9a249604-577a-448a-8c1c-1414e4fd29f7'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'main.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE chat_sessions (session_id,title,cwd,archived,deleted_at,session_kind,owner_session_id,workspace_id)')
                db.execute('CREATE TABLE workspaces (workspace_id,name,archived,deleted_at)')
                db.execute("INSERT INTO chat_sessions VALUES (?,?,?,0,NULL,'standard',NULL,'workspace')", (sid, '任务', '/work/directory'))
                db.execute("INSERT INTO workspaces VALUES ('workspace','工作区别名',0,NULL)")
            request = dict(app_id='qoder-cn', id='qoder-cn:'+sid, navigation_key=sid,
                           title='任务', navigation_title='任务', project='/work/directory')
            self.assertEqual(qoder_target(request, path)[2], '工作区别名')
            with sqlite3.connect(path) as db:
                db.execute("UPDATE workspaces SET workspace_id='another'")
            with self.assertRaisesRegex(Blocked, 'target_unverified'):
                qoder_target(request, path)

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

    def test_send_receipt_never_reports_queue_promotion(self):
        original_send = self.backend.send
        def promoted_send():
            original_send()
            self.backend.queue_promoted = True
        self.backend.send = promoted_send
        self.assertEqual(self.run_request(), {'code': 'sent_pending_confirmation', 'attempted': True})
        self.assertEqual(self.backend.sends, 1)
        with self.assertRaisesRegex(Blocked, 'already_attempted'):
            self.run_request()

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
        # 无自定义文本时回落固定 MESSAGE；显式 text 走已校验的按软件提示词。
        self.request.update(automation_mode='rage', started_at=100, kind='interrupt', text='错误的可配置文本')
        self.assertEqual(self.run_request()['code'], 'sent_pending_confirmation')
        self.assertEqual(self.backend.written, ['错误的可配置文本'])
        self.assertEqual(self.run_code()['code'], 'already_attempted')
        self.assertEqual(self.backend.sends, 1)

    def test_rage_interrupt_without_text_falls_back_to_message(self):
        self.request.update(automation_mode='rage', started_at=100, kind='interrupt')
        self.request.pop('text', None)
        self.assertEqual(self.run_request()['code'], 'sent_pending_confirmation')
        self.assertEqual(self.backend.written, [MESSAGE])

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
    def test_zcode_returns_from_statistics_once_before_search(self):
        b = self.backend('zcode')
        b.guard = lambda **kw: None
        b.current_identity = lambda nodes: nodes == ['verified task']
        back = dict(label='返回工作区', rect=(10, 70, 120, 100), actions=['AXPress'])
        calls = []
        b.ax.press = lambda node: calls.append(node) or True
        b.navigation_nodes = lambda _: ['verified task']
        self.assertEqual(b.route_zcode_search([(back, 'AXButton')]), ['verified task'])
        self.assertEqual(calls, [back])
        # A still-rendering settings page never loops or presses repeatedly.
        b.navigation_nodes = lambda _: [(back, 'AXButton')]
        calls.clear()
        with self.assertRaisesRegex(Blocked, 'search_unavailable'):
            b.route_zcode_search([(back, 'AXButton')])
        self.assertEqual(calls, [back])
        calls.clear()
        with self.assertRaisesRegex(Blocked, 'search_unavailable'):
            b.route_zcode_search([(back, 'AXButton'), (dict(back), 'AXButton')])
        self.assertEqual(calls, [])

    def test_zcode_native_paste_avoids_partial_axvalue_and_rechecks_identity(self):
        b = self.backend('zcode')
        b.request['overwrite_draft'] = True
        b.box = {'AXFocused': True}
        b.guard = lambda **kw: None
        b.ax.set_attr = lambda *args: self.fail('ZCode must not mutate AXValue before native paste')
        value = ['existing draft']
        identity = [True]
        b.snapshot = lambda: dict(identity=identity[0], busy=False, input_count=1,
                                  value=value[0], send_enabled=value[0] == MESSAGE)
        b.focus_composer = lambda: None
        actions = []
        b.inject = SimpleNamespace(hotkey=lambda key: actions.append(key))
        b.paste_verified = lambda text: value.__setitem__(0, text)
        b.write(MESSAGE)
        self.assertEqual(actions, [0])
        self.assertEqual(value[0], MESSAGE)
        b.focus_composer = lambda: identity.__setitem__(0, False)
        with self.assertRaisesRegex(Blocked, 'target_unverified'):
            b.write('another message')
        self.assertEqual(value[0], MESSAGE)

    def backend(self, app='autoclaw'):
        class AX:
            @staticmethod
            def role(n): return n.get('role', '')
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
            @staticmethod
            def action_names(n): return n.get('actions', ['AXPress'])
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

    def test_zcode_large_history_does_not_hide_identity_search_or_composer(self):
        b = self.backend('zcode')
        history = dict(role='AXGroup', AXDOMClassList=['@md/conversation:px-6', 'gap-5', 'pb-5'],
                       children=[dict(role='AXStaticText') for _ in range(6500)])
        header = dict(role='AXHeading', label=b.title, rect=(400, 50, 600, 80))
        project = dict(role='AXButton', label='Project', rect=(320, 50, 370, 80))
        box = dict(role='AXGroup', placeholder='提出后续修改要求', description='text entry area',
                   rect=(350, 600, 900, 700), AXValue='')
        send = dict(role='AXButton', label='发送', rect=(850, 710, 900, 740))
        stop = dict(role='AXButton', label='停止生成', rect=(800, 710, 850, 740))
        search = dict(role='AXComboBox', placeholder='搜索操作、任务或文件')
        b.root = dict(role='AXWindow', children=[search, dict(role='AXGroup',
                      AXDOMIdentifier='conversation', children=[project, header, history, box, stop, send])])
        b.zcode_identity = (b.title, 'Project', '/w/Project', True)
        nodes = b.nodes()
        self.assertEqual(len(nodes), 9)
        self.assertEqual(b.search_boxes(nodes), [search])
        snap = b.snapshot()
        self.assertTrue(snap['identity'])
        self.assertEqual(snap['input_count'], 1)
        self.assertTrue(snap['busy'])
        self.assertTrue(snap['send_exists'])
        # Other providers, unknown classes, and lookalikes outside the
        # conversation must still reject an incomplete control tree.
        for mutation in ('other-app', 'unknown-layout', 'outside-conversation'):
            with self.subTest(mutation=mutation):
                b.request['app_id'] = 'zcode'
                history['AXDOMClassList'] = ['@md/conversation:px-6', 'gap-5', 'pb-5']
                b.root['children'][1]['AXDOMIdentifier'] = 'conversation'
                if mutation == 'other-app': b.request['app_id'] = 'autoclaw'
                if mutation == 'unknown-layout': history['AXDOMClassList'] = ['gap-5', 'pb-5']
                if mutation == 'outside-conversation': b.root['children'][1]['AXDOMIdentifier'] = ''
                with self.assertRaisesRegex(Blocked, 'tree_incomplete'):
                    b.nodes()

    def test_zcode_navigation_waits_for_editor_after_matching_header(self):
        b = self.backend('zcode')
        b.guard = lambda **_: None
        header_only, mounted = [object()], [object()]
        b.nodes = Mock(side_effect=[header_only, mounted])
        b.current_identity = lambda _: True
        b.snapshot = Mock(side_effect=[dict(identity=True, input_count=0, value=None, send_exists=False),
                                      dict(identity=True, input_count=1, value='', send_exists=True)])
        with patch('supervisor.bridge.time.sleep') as sleep:
            self.assertIs(b.navigation_nodes(1.5), mounted)
        self.assertEqual(b.nodes.call_count, 2)
        self.assertEqual(b.snapshot.call_args_list[0].args, (header_only,))
        sleep.assert_called_once()

    def test_inspect_is_readonly_and_excludes_draft_text(self):
        b = self.backend()
        b.bind_window = lambda: None
        b.guard = b.route = b.write = b.send = lambda *_: self.fail('inspect cannot act on the desktop')
        b.snapshot = lambda: dict(identity=True, input_count=1, busy=False, value='private draft',
                                 send_exists=True, send_enabled=True)
        result = b.inspect()
        self.assertEqual(result['code'], 'ready')
        self.assertFalse(result['attempted'])
        self.assertFalse(result['empty_composer'])
        self.assertNotIn('private', str(result))

    def test_zcode_search_accepts_unique_indexed_title_with_project_in_composite_label(self):
        b = self.backend('zcode')
        result = {'label': b.title + ' task summary Project 刚刚',
                  'children': [{'label': b.title, 'role': 'AXStaticText'}]}
        b.zcode_identity = (b.title, 'Project', '/w/Project', True)
        self.assertTrue(b.zcode_search_match(result))
        b.zcode_identity = (b.title, 'Project', '/w/Project', False)
        self.assertFalse(b.zcode_search_match(result))
        result['children'].append({'label': 'Project', 'role': 'AXStaticText'})
        self.assertTrue(b.zcode_search_match(result))
        result['children'][0]['label'] = 'Other task'
        self.assertFalse(b.zcode_search_match(result))

    def test_zcode_search_reads_nested_title_and_english_input(self):
        b = self.backend('zcode')
        b.zcode_identity = (b.title, 'Project', '/w/Project', True)
        leaf = {'role': 'AXStaticText', 'label': b.title}
        for _ in range(5):
            leaf = {'children': [leaf]}
        self.assertTrue(b.zcode_search_match(leaf))
        boxes = [({'placeholder': 'Search actions, tasks, or files'}, 'AXComboBox')]
        self.assertEqual(b.search_boxes(boxes), [boxes[0][0]])
        boxes[0][0]['placeholder'] = '提出后续修改要求'
        self.assertEqual(b.search_boxes(boxes), [])

    def test_zcode_repeated_message_hits_choose_top_result_and_route(self):
        b = self.backend('zcode')
        b.zcode_identity = (b.title, 'Project', '/w/Project', True)
        box = {'placeholder': '搜索操作、任务或文件', 'AXValue': b.title,
               'rect': (200, 150, 500, 180)}
        def hit(y):
            return {'role': 'AXMenuItem', 'rect': (100, y, 700, y + 52), 'children': [
                {'children': [{'role': 'AXStaticText', 'label': b.title}]},
                {'role': 'AXStaticText', 'label': 'Message hit'},
                {'role': 'AXStaticText', 'label': 'Project'}]}
        top, lower = hit(220), hit(272)
        nodes = [(box, 'AXComboBox'), (lower, 'AXMenuItem'), (top, 'AXMenuItem')]
        b.nodes = lambda: nodes
        b.guard = lambda: None
        b.write_search_query = Mock()
        b.ax = SimpleNamespace(**{name: getattr(b.ax, name) for name in
                                 ('placeholder', 'get_attr', 'enabled', 'action_names', 'rect_of',
                                  'children', 'role', 'element_name')}, press=Mock(return_value=True))
        final = [object()]
        b.navigation_nodes = Mock(return_value=final)
        self.assertIs(b.route_zcode_search(nodes), final)
        b.ax.press.assert_called_once_with(top)
        b.write_search_query.assert_called_once_with(box)
        b.zcode_identity = (b.title, 'Project', '/w/Project', False)
        with self.assertRaisesRegex(Blocked, 'search_result_ambiguous'):
            b.zcode_search_result(nodes)

    def test_zcode_search_rejects_title_only_in_another_task_message(self):
        b = self.backend('zcode')
        b.zcode_identity = (b.title, 'Project', '/w/Project', True)
        result = {'children': [
            {'children': [{'role': 'AXStaticText', 'label': 'Other task'}]},
            {'children': [{'role': 'AXStaticText', 'label': b.title}]},
            {'role': 'AXStaticText', 'label': 'Project'}]}
        self.assertFalse(b.zcode_search_match(result))

    def test_zcode_highlighted_title_preserves_spaces_and_clipped_tail(self):
        b = self.backend('zcode')
        b.title = '剩余问题 相似图 pHash 堆叠 长标题...'
        b.zcode_identity = (b.title, 'Project', '/w/Project', True)
        def text(value, y=220, height=17):
            return {'role': 'AXStaticText', 'label': value.strip(), 'AXValue': value,
                    'rect': (200, y, 600, y + height)}
        parts = ['剩余问题', ' ', '相似图', ' ', 'pHash', ' ', '堆叠', ' ', '长标题...']
        leaves = [text(value) for value in parts[:-1]] + [text(parts[-1], 269, 1)]
        result = {'children': leaves + [text(b.title, 238), text('Project', 228)]}
        self.assertTrue(b.zcode_search_match(result))
        # Same-line suffixes and a title occurring only in the snippet cannot
        # become the target, even when the index says its title is unique.
        result['children'].insert(1, text(' Other'))
        self.assertFalse(b.zcode_search_match(result))
        result['children'] = [text('Other task')] + [text(value, 238) for value in parts]
        self.assertFalse(b.zcode_search_match(result))

    def test_zcode_search_excludes_hidden_and_disabled_hits(self):
        b = self.backend('zcode')
        b.zcode_identity = (b.title, 'Project', '/w/Project', True)
        title = [{'role': 'AXStaticText', 'label': b.title}]
        hidden = {'rect': (100, 160, 700, 161), 'children': title}
        disabled = {'rect': (100, 180, 700, 232), 'enabled': False, 'children': title}
        outside = {'rect': (100, -20, 700, 32), 'children': title}
        inactive = {'rect': (100, 240, 700, 292), 'actions': [], 'children': title}
        self.assertIsNone(b.zcode_search_result([(n, 'AXMenuItem') for n in
                                                (hidden, disabled, outside, inactive)]))

    def test_zcode_collapsed_sidebar_still_requires_adjacent_project_and_header(self):
        b = self.backend('zcode')
        b.zcode_identity = (b.title, 'Project', '/w/Project', True)
        heading = ({'rect': (90, 60, 260, 81), 'AXValue': 1, 'label': b.title}, 'AXHeading')
        project = ({'rect': (55, 57, 83, 85), 'label': 'Project'}, 'AXButton')
        self.assertTrue(b.current_identity([heading, project]))
        self.assertFalse(b.current_identity([heading]))
        project[0]['rect'] = (55, 300, 83, 328)
        self.assertFalse(b.current_identity([heading, project]))
        project[0]['rect'] = (55, 57, 83, 85)
        self.assertFalse(b.current_identity([heading, heading, project]))
        clipped = ({'rect': (90, 90, 260, 91), 'AXValue': 1, 'label': b.title}, 'AXHeading')
        self.assertTrue(b.current_identity([heading, project, clipped]))
        box = ({'rect': (60, 650, 800, 710), 'placeholder': '提出后续修改要求', 'AXValue': ''}, 'AXGroup')
        box[0]['description'] = 'text entry area'
        preview = ({'rect': (850, 650, 980, 710), 'placeholder': '网站搜索', 'AXValue': ''}, 'AXTextField')
        b.nodes = lambda: [heading, project, box, preview]
        self.assertEqual(b.snapshot()['input_count'], 1)
        self.assertIs(b.box, box[0])

    def test_autoclaw_current_age_labels_keep_exact_title(self):
        b = self.backend()
        for suffix in ('2天', '1周', '2周', '1个月', '3个月', '1年', '99年', '正在回复...'):
            self.assertTrue(b.sidebar_match(b.title + ' ' + suffix), suffix)
        for suffix in ('2天备份', '1周 新对话', '2月', '正在回复副本'):
            self.assertFalse(b.sidebar_match(b.title + ' ' + suffix), suffix)
        self.assertFalse(b.sidebar_match('正确会话的备份 2天'))

    def test_zcode_identity_captures_each_geometry_once_during_refresh(self):
        b = self.backend('zcode')
        b.zcode_identity = (b.title, 'Project', '/w/Project', True)
        header = {'rect': (90, 60, 260, 81), 'AXValue': 1, 'label': b.title}
        project = {'rect': (55, 57, 83, 85), 'label': 'Project'}
        reads = {}
        def transient_rect(node):
            key = id(node)
            reads[key] = reads.get(key, 0) + 1
            return node['rect'] if reads[key] == 1 else None
        b.ax.rect_of = transient_rect
        self.assertTrue(b.current_identity([(header, 'AXHeading'), (project, 'AXButton')]))
        self.assertEqual(list(reads.values()), [1, 1])

    def test_zcode_identity_missing_geometry_or_label_fails_closed(self):
        b = self.backend('zcode')
        b.zcode_identity = (b.title, 'Project', '/w/Project', True)
        header = {'rect': (90, 60, 260, 81), 'AXValue': 1, 'label': b.title}
        project = {'rect': (55, 57, 83, 85), 'label': 'Project'}
        nodes = [(header, 'AXHeading'), (project, 'AXButton')]
        for node in (header, project):
            rect = node.pop('rect')
            self.assertFalse(b.current_identity(nodes))
            node['rect'] = rect
        project['label'] = ''
        self.assertFalse(b.current_identity(nodes))
        project['label'] = 'Project'
        header['label'] = ''
        self.assertFalse(b.current_identity(nodes))

    def test_qoder_identity_captures_heading_geometry_once_during_refresh(self):
        b = self.backend('qoder')
        b.qoder_identity = ('sid', b.title, 'Project', '/w/Project')
        header = {'rect': (90, 60, 260, 81), 'AXValue': 1, 'label': b.title}
        area = {'AXURL': qoder_chat_url('sid', 'qoder')}
        context = {'label': '当前任务上下文', 'children': [{'role': 'AXGroup', 'label': 'Project'}]}
        rect = Mock(side_effect=[header['rect'], None])
        b.ax.rect_of = rect
        nodes = [(header, 'AXHeading'), (area, 'AXWebArea'), (context, 'AXGroup')]
        self.assertTrue(b.current_identity(nodes))
        rect.assert_called_once_with(header)
        self.assertFalse(b.current_identity(nodes))

    def test_sidebar_candidate_geometry_is_not_read_again_after_refresh(self):
        b = self.backend()
        target = {'rect': (20, 120, 270, 150), 'label': b.title}
        rect = Mock(side_effect=[target['rect'], None])
        b.ax.rect_of = rect
        self.assertEqual(b.sidebar_candidates([(target, 'AXButton')]), [target])
        rect.assert_called_once_with(target)
        self.assertEqual(b.sidebar_candidates([(target, 'AXButton')]), [])

    def test_autoclaw_expands_only_unique_navigation_controls(self):
        b = self.backend()
        b.guard = lambda **_: None
        presses = []
        b.ax.press = lambda node: presses.append(node['label']) or True
        expand = ({'label': '展开侧边栏', 'rect': (20, 60, 50, 90)}, 'AXButton')
        more = ({'label': '展示更多', 'rect': (20, 300, 150, 330)}, 'AXButton')
        target = ({'label': b.title + ' 2天', 'rect': (20, 400, 150, 430)}, 'AXButton')
        b.navigation_nodes = lambda _: [more] if len(presses) == 1 else [target]
        self.assertEqual(b.expand_autoclaw_sidebar([expand]), [target])
        self.assertEqual(presses, ['展开侧边栏', '展示更多'])
        presses.clear()
        self.assertEqual(b.expand_autoclaw_sidebar([more, more]), [more, more])
        self.assertEqual(presses, [])

    def search_backend(self):
        b = self.backend('zcode')
        b.guard = lambda **_: None
        box = {'rect': (200, 200, 500, 230), 'AXFocused': True,
               'placeholder': '搜索操作、任务或文件', 'AXValue': ''}
        b.nodes = lambda: [(box, 'AXComboBox')]
        actions = []
        b.inject = SimpleNamespace(click_at=lambda *_: actions.append('focus-search'),
            hotkey=lambda key: (actions.append(key), box.update(AXValue=b.title) if key == 9 else None))
        saved_item = SimpleNamespace(types=lambda: ['custom'], dataForType_=lambda _: b'original')
        clone = SimpleNamespace(setData_forType_=lambda *_: None)
        board = SimpleNamespace(pasteboardItems=lambda: [saved_item], clearContents=lambda: actions.append('clear'),
            setString_forType_=lambda value, _: actions.append(('query', value)), changeCount=lambda: 1,
            writeObjects_=lambda items: actions.append(('restore', items == [clone])))
        b.AppKit = SimpleNamespace(NSPasteboard=SimpleNamespace(generalPasteboard=lambda: board),
            NSPasteboardItem=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: clone)),
            NSPasteboardTypeString='text')
        b.snapshot = b.send = b.write = lambda *_: self.fail('search must never touch the composer/send')
        return b, box, actions

    def test_search_paste_never_calls_composer_or_send_and_restores_clipboard(self):
        b, box, actions = self.search_backend()
        b.write_search_query(box)
        self.assertEqual(actions, ['focus-search', 0, 'clear', ('query', b.title), 9, 'clear', ('restore', True)])
        box['AXFocused'] = False
        box['AXValue'] = ''
        with self.assertRaisesRegex(Blocked, 'search_input_changed'):
            b.write_search_query(box)

    def test_search_rebinds_replaced_handle_before_paste(self):
        b, old, actions = self.search_backend()
        replacement = dict(old)
        b.nodes = lambda: [(replacement, 'AXComboBox')]
        def hotkey(key):
            actions.append(key)
            if key == 9:
                replacement['AXValue'] = b.title
        b.inject.hotkey = hotkey
        b.write_search_query(old)
        self.assertEqual(replacement['AXValue'], b.title)
        self.assertEqual(old['AXValue'], '')
        self.assertEqual(actions.count(9), 1)

    def test_search_waits_for_delayed_focus_without_typing(self):
        b, box, actions = self.search_backend()
        pending = dict(box, AXFocused=False)
        states = [[], [(pending, 'AXComboBox')], [(box, 'AXComboBox')]]
        b.nodes = lambda: states.pop(0)
        self.assertIs(b.wait_search_box({''}, focused=True), box)
        self.assertEqual(actions, [])

    def test_search_paste_survives_transient_missing_field_and_result_focus(self):
        b, box, actions = self.search_backend()
        after = dict(box, AXValue=b.title, AXFocused=False)
        reads = []
        def nodes():
            if 9 not in actions:
                return [(box, 'AXComboBox')]
            reads.append(True)
            return [] if len(reads) == 1 else [(after, 'AXComboBox')]
        b.nodes = nodes
        b.write_search_query(box)
        self.assertEqual(actions.count(9), 1)
        self.assertEqual(actions[-1], ('restore', True))

    def test_search_exact_existing_query_uses_no_keyboard_or_clipboard(self):
        b, box, actions = self.search_backend()
        box.update(AXValue=b.title, AXFocused=False)
        b.write_search_query(box)
        self.assertEqual(actions, [])

    def test_search_rejects_changed_value_duplicate_or_outside_field(self):
        b, box, actions = self.search_backend()
        for nodes, code in [
            ([(dict(box, AXValue='Other query'), 'AXComboBox')], 'search_input_changed'),
            ([(dict(box, AXValue=[]), 'AXComboBox')], 'search_input_changed'),
            ([(box, 'AXComboBox'), (dict(box), 'AXComboBox')], 'search_input_changed'),
            ([(dict(box, rect=(1200, 200, 1500, 230)), 'AXComboBox')], 'search_unavailable')]:
            b.nodes = lambda: nodes
            with self.assertRaisesRegex(Blocked, code):
                b.write_search_query(box)
        self.assertEqual(actions, [])

    def test_search_user_activity_after_paste_stops_without_repeating(self):
        b, box, actions = self.search_backend()
        def guard(**_):
            if 9 in actions:
                raise Blocked('user_active')
        b.guard = guard
        with self.assertRaisesRegex(Blocked, 'user_active'):
            b.write_search_query(box)
        self.assertEqual(actions.count(9), 1)
        self.assertEqual(actions[-1], ('restore', True))

    def test_send_leaves_a_new_queue_control_in_arrival_order(self):
        b = self.backend('zcode')
        presses = []
        b.send_button = {'label': '发送'}
        b.ax.press = lambda node: presses.append(node['label']) or True
        b.current_identity = lambda _: True
        b.queue_promoted = False
        before = [(b.send_button, 'AXButton')]
        after = [({'label': '插队', 'enabled': True}, 'AXButton')]
        calls = [before, after]
        b.nodes = lambda: calls.pop(0)
        b.send()
        self.assertEqual(presses, ['发送'])
        self.assertFalse(b.queue_promoted)

    def test_send_leaves_existing_or_ambiguous_queue_untouched(self):
        for before, after in [
            ([({'label': '插队'}, 'AXButton')], [({'label': '插队'}, 'AXButton')]),
            ([], [({'label': '插队'}, 'AXButton'), ({'label': '插队'}, 'AXButton')]),
        ]:
            b = self.backend('zcode')
            presses = []
            b.send_button = {'label': '发送'}
            b.ax.press = lambda node: presses.append(node['label']) or True
            b.current_identity = lambda _: True
            b.queue_promoted = False
            calls = [before, after]
            b.nodes = lambda: calls.pop(0)
            b.send()
            self.assertEqual(presses, ['发送'])
            self.assertFalse(b.queue_promoted)

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
        box = ({'rect': (310, 650, 880, 710), 'AXValue': '输入“@”使用技能',
                'placeholder': '输入“@”使用技能', 'AXDescription': '输入“@”使用技能'}, 'AXTextArea')
        send = ({'rect': (850, 730, 880, 760), 'label': '发送消息', 'enabled': False}, 'AXButton')
        nodes = [header, box, send]
        b.nodes = lambda: nodes
        snapshot = b.snapshot()
        self.assertEqual(snapshot['value'], '')
        self.assertTrue(snapshot['send_exists'])
        self.assertFalse(snapshot['send_enabled'])
        box[0].pop('placeholder')
        self.assertEqual(b.snapshot()['value'], '输入“@”使用技能')
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
        b.nodes = lambda: [({'rect': (310, 650, 880, 710), 'AXDescription': '发送给 AutoClaw',
                            'AXValue': '\ufeff' + MESSAGE + '\ufeff'}, 'AXTextArea')]
        self.assertEqual(b.snapshot()['value'], MESSAGE)

    def test_autoclaw_goal_review_fields_never_become_the_chat_composer(self):
        b = self.backend()
        b.current_identity = lambda _: True
        main = ({'rect': (310, 650, 880, 710), 'AXDescription': '填写你的目标，AutoClaw会持续工作至完成目标...',
                 'AXValue': '', 'placeholder': '填写你的目标，AutoClaw会持续工作至完成目标...'}, 'AXTextArea')
        reviews = [({'rect': (310, 450 + i * 15, 880, 480 + i * 15),
                     'AXDescription': '描述一条完成标准', 'AXValue': 'user goal'}, 'AXTextArea') for i in range(12)]
        b.nodes = lambda: reviews + [main]
        snapshot = b.snapshot()
        self.assertEqual(snapshot['input_count'], 1)
        self.assertEqual(snapshot['value'], '')
        self.assertIs(b.box, main[0])
        main[0]['enabled'] = False
        self.assertEqual(b.snapshot()['input_count'], 0)
        self.assertIsNone(b.box)

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

    def test_grok_current_running_badge_and_collapsed_sidebar(self):
        b = self.backend('grok')
        b.grok_identity = (b.title, '工程', '/tmp/project')
        for age in ('11分钟前', '2周前', '1个月前'):
            self.assertTrue(b.sidebar_match(b.title + ' ' + age + ' 进行中…'))
        self.assertFalse(b.sidebar_match(b.title + ' 11分钟前 进行中… 副本'))
        header = ({'rect': (70, 60, 300, 80), 'AXValue': 1, 'label': b.title}, 'AXHeading')
        clipped = ({'rect': (70, 90, 300, 91), 'AXValue': 1, 'label': b.title}, 'AXHeading')
        project = ({'rect': (70, 650, 300, 680), 'label': '工程'}, 'AXPopUpButton')
        self.assertTrue(b.current_identity([header, clipped, project]))
        project[0]['rect'] = (70, 60, 300, 80)
        self.assertFalse(b.current_identity([header, project]))

    def test_grok_expands_only_exact_unique_collapsed_project(self):
        b = self.backend('grok')
        b.grok_identity = (b.title, '工程', '/tmp/project')
        b.guard = lambda **_: None
        project = {'label': '工程', 'rect': (20, 100, 150, 130), 'AXExpanded': False}
        calls = []
        b.ax.press = lambda node: calls.append(node) or True
        target = [({'label': b.title + ' 11分钟前 进行中…', 'rect': (20, 140, 170, 170)}, 'AXButton')]
        b.navigation_nodes = lambda _: target
        self.assertIs(b.expand_grok_project([(project, 'AXButton')]), target)
        self.assertEqual(calls, [project])
        calls.clear()
        b.expand_grok_project([(project, 'AXButton'), (project, 'AXButton')])
        self.assertEqual(calls, [])
        project['AXExpanded'] = True
        b.expand_grok_project([(project, 'AXButton')])
        self.assertEqual(calls, [])

    def test_qoder_editions_bind_url_and_only_real_composer(self):
        sid = '9a249604-577a-448a-8c1c-1414e4fd29f7'
        for app in ('qoder', 'qoder-cn'):
            b = self.backend(app)
            b.qoder_identity = (sid, b.title, '工程', '/tmp/工程')
            url = ({'AXURL': qoder_chat_url(sid, app)}, 'AXWebArea')
            header = ({'rect': (70, 70, 300, 95), 'label': b.title, 'AXValue': 1}, 'AXHeading')
            context = ({'label': '当前任务上下文', 'children': [{'label': '工程', 'role': 'AXGroup'}]}, 'AXGroup')
            box = ({'rect': (60, 650, 800, 710), 'AXDescription': '发送任务消息', 'AXValue': ''}, 'AXTextArea')
            preview = ({'rect': (800, 650, 980, 710), 'AXDescription': '网站留言', 'AXValue': ''}, 'AXTextArea')
            b.nodes = lambda: [url, header, context, box, preview]
            snapshot = b.snapshot()
            self.assertTrue(snapshot['identity'])
            self.assertEqual(snapshot['input_count'], 1)
            other = 'qoder-cn' if app == 'qoder' else 'qoder'
            url[0]['AXURL'] = qoder_chat_url(sid, other)
            self.assertFalse(b.snapshot()['identity'])

    def test_real_user_input_still_interrupts_after_our_write(self):
        b = self.backend('grok')
        b.Quartz = SimpleNamespace(kCGSessionOnConsoleKey='onConsole', CGSessionCopyCurrentDictionary=lambda: {'onConsole': True})
        b.winops.user_active = lambda _: True
        with self.assertRaisesRegex(Blocked, 'user_active'):
            b.guard(after_write=True)

    def test_qoder_expands_only_bound_workspace_until_uuid_link_appears(self):
        b = self.backend('qoder')
        sid = '9a249604-577a-448a-8c1c-1414e4fd29f7'
        b.qoder_identity = (sid, b.title, '别名', '/tmp/directory')
        b.guard = lambda **_: None
        calls = []
        b.ax.press = lambda node: calls.append(node['label']) or True
        expand = ({'label': '展开工作目录 别名'}, 'AXButton')
        more = ({'label': '展示 别名 的更多任务'}, 'AXButton')
        link = ({'AXURL': qoder_chat_url(sid, 'qoder')}, 'AXLink')
        b.navigation_nodes = lambda _: [more] if len(calls) == 1 else [link]
        self.assertEqual(b.expand_qoder_workspace([expand]), [link])
        self.assertEqual(calls, ['展开工作目录 别名', '展示 别名 的更多任务'])
        calls.clear()
        b.expand_qoder_workspace([expand, expand])
        self.assertEqual(calls, [])

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
