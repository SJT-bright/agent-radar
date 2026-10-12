import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from supervisor.bridge import Blocked, Journal, resolve_text
from supervisor.openclaw_transport import OpenClawTransport

SID = '11111111-1111-4111-8111-111111111111'


class OpenClawTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.row=dict(id='openclaw:main:'+SID, app_id='openclaw',title='测试任务',project='/demo',
                      source='local-db',navigation_key='agent:main:desktop',started_at=1234.5,
                      status='interrupted',status_reason='OpenClaw 明确记录本轮执行超时',
                      timing_basis='turn',user_stopped=False)
        self.request=dict(self.row,mode='send',key=self.row['id']+':1234500',text='继续未完成工作',
                          kind='interrupt',manual_retry=True,automation_mode='rage')
        self.calls=[]
        self.cli=patch('supervisor.openclaw_transport.cli_path',return_value='/fake/openclaw');self.cli.start()
        self.addCleanup(self.cli.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self,command,**kwargs):
        if 'health' in command:
            return SimpleNamespace(returncode=0,stdout='{"ok":true}')
        self.calls.append(command)
        params=json.loads(command[command.index('--params')+1])
        return SimpleNamespace(returncode=0,stdout=json.dumps(dict(runId=params['idempotencyKey'],status='started')))

    def backend(self,collector=None,runner=None):
        if runner:
            send_runner=runner
            runner=lambda command,**kw: self.runner(command,**kw) if 'health' in command else send_runner(command,**kw)
        return OpenClawTransport(self.request,Blocked,collector or (lambda sid:copy.deepcopy(self.row)),
                                 runner or self.runner,clock=lambda:2000.123)

    def journal(self):
        journal=Journal(Path(self.tmp.name))
        self.addCleanup(journal.lock.close)
        return journal

    def test_exact_session_local_url_no_deliver_no_insert(self):
        result=self.backend().perform(self.journal(),resolve_text)
        self.assertEqual(result['code'],'sent_pending_confirmation')
        command=self.calls[0];params=json.loads(command[-1])
        self.assertEqual(params['sessionId'],SID); self.assertEqual(params['sessionKey'],'agent:main:desktop')
        self.assertFalse(params['deliver']);self.assertNotIn('queueMode',params)
        self.assertEqual(command[command.index('--expect-url')+1],'ws://127.0.0.1:19189')
        self.assertEqual(result['sent_at'],2000.123)

    def test_inspect_is_read_only_without_journal_or_rpc(self):
        self.assertEqual(self.backend().inspect()['code'],'ready')
        self.assertEqual(self.calls,[]);self.assertEqual(list(Path(self.tmp.name).iterdir()),[])

    def test_check_does_not_send_or_reserve(self):
        self.request['mode']='check';j=self.journal()
        self.assertEqual(self.backend().perform(j,resolve_text)['code'],'ready')
        self.assertEqual(j.rows,[]);self.assertEqual(self.calls,[])

    def test_wrong_identity_is_rejected_before_reserve(self):
        for field in ['project','source','navigation_key','title']:
            with self.subTest(field=field):
                old=self.row[field];self.row[field]='changed'
                with self.assertRaises(Blocked):self.backend().guard()
                self.row[field]=old
        self.assertEqual(self.calls,[])

    def test_running_unknown_waiting_and_stopped_never_send(self):
        for status in ['running','unknown','waiting']:
            self.row['status']=status
            with self.assertRaises(Blocked):self.backend().guard()
        self.row['status']='interrupted';self.row['user_stopped']=True
        with self.assertRaises(Blocked):self.backend().guard()

    def test_changed_round_cancels(self):
        self.row['started_at']=1235
        with self.assertRaisesRegex(Blocked,'state_changed'):self.backend().guard()

    def test_revision_change_cancels(self):
        self.request['lifecycle_revision']='old';self.row['lifecycle_revision']='new'
        with self.assertRaisesRegex(Blocked,'state_changed'):self.backend().guard()

    def test_guard_is_repeated_before_reservation(self):
        sequence=[copy.deepcopy(self.row),dict(self.row,status='running')]
        with self.assertRaisesRegex(Blocked,'already_running'):
            self.backend(collector=lambda sid:sequence.pop(0)).perform(self.journal(),resolve_text)
        self.assertEqual(self.calls,[])

    def test_timeout_attempt_is_durable_and_never_repeated(self):
        import subprocess
        backend=self.backend(runner=lambda *a,**kw:(_ for _ in ()).throw(subprocess.TimeoutExpired('openclaw',18)))
        j=self.journal()
        with self.assertRaisesRegex(Blocked,'send_unconfirmed'):backend.perform(j,resolve_text)
        self.assertTrue(backend.did_write)
        with self.assertRaisesRegex(Blocked,'already_attempted'):j.check(self.request)

    def test_ack_must_bind_our_run_id(self):
        b=self.backend(runner=lambda *a,**kw:SimpleNamespace(returncode=0,stdout='{"runId":"other","status":"started"}'))
        with self.assertRaisesRegex(Blocked,'send_unconfirmed'):b.perform(self.journal(),resolve_text)
        self.assertTrue(b.did_write)

    def test_unknown_abort_requires_manual_action(self):
        self.row['status_reason']='OpenClaw 中止事件未记录触发者，触发者待确认'
        self.request['manual_retry']=False
        with self.assertRaises(Blocked):self.backend().guard()

    def test_selected_child_needs_single_manual_authorization(self):
        self.row['manual_only']=True; self.request['manual_retry']=False
        with self.assertRaisesRegex(Blocked,'manual_resolution'):self.backend().guard()
        self.request['manual_retry']=True;self.assertEqual(self.backend().guard()['id'],self.row['id'])

    def test_invalid_text_never_claims_slot(self):
        self.request['text']='x'*2001;j=self.journal()
        with self.assertRaisesRegex(Blocked,'text_invalid'):self.backend().perform(j,resolve_text)
        self.assertEqual(j.rows,[])

    def test_no_cli_gives_actionable_error(self):
        with patch('supervisor.openclaw_transport.cli_path',return_value=None):
            self.assertEqual(self.backend().inspect()['code'],'openclaw_cli_unavailable')
            with self.assertRaisesRegex(Blocked,'openclaw_cli_unavailable'):self.backend().guard()

    def test_gateway_failure_does_not_consume_send_slot(self):
        b=self.backend();b.runner=lambda *a,**kw:SimpleNamespace(returncode=1,stdout='')
        j=self.journal()
        with self.assertRaisesRegex(Blocked,'openclaw_gateway_unavailable'):b.perform(j,resolve_text)
        self.assertFalse(b.did_write);self.assertEqual(j.rows,[])
