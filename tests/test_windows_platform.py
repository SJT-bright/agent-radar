import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from collector import runtime_paths as paths
from supervisor import windows_bridge as win
from supervisor import platform_support as support
from supervisor.openclaw_runtime import local_url, cli_prefix
from supervisor.bridge import Blocked

class PortableTests(unittest.TestCase):
    def test_tasklist_exact_image_only(self):
        parsed=paths.parse_tasklist('"Codex.exe","123","Console","1","10 K"\n"C:\\rogue\\Codex.exe","124"\nnot csv')
        self.assertEqual(parsed,{123:'Codex.exe'})
        self.assertTrue(paths.windows_image_matches('QoderCN.exe','Qoder CN',('QoderCN',)))
        self.assertFalse(paths.windows_image_matches('/Qoder CN.app/Contents/MacOS/Qoder','Qoder CN'))
    def test_codex_owner_requires_actual_file_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);locks=root/'thread-writer-locks';locks.mkdir();(locks/'fixture.lock').write_text('')
            procs={7:{'app':'codex'}}
            self.assertEqual(paths.codex_lock_owners(procs,root,lambda _:None),{})
            self.assertEqual(paths.codex_lock_owners(procs,root,lambda _:{8}),{})
            self.assertEqual(paths.codex_lock_owners(procs,root,lambda _:{7}),{'fixture':7})
    def test_windows_root_requires_supported_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);roaming=home/'AppData/Roaming';root=roaming/'Qoder';root.mkdir(parents=True)
            (root/'main.sqlite').write_text('not sqlite')
            result=paths.app_support_root(home,('mac-only',),('Qoder',),(('main.sqlite','sqlite'),),platform='nt',environ={})
            self.assertEqual(result.state,'format_mismatch');self.assertIsNone(result.path)
            db=sqlite3.connect(root/'main.sqlite') if False else None
            (root/'main.sqlite').unlink();db=sqlite3.connect(root/'main.sqlite');db.execute('create table metadata(id text)');db.commit();db.close()
            self.assertEqual(paths.app_support_root(home,('mac-only',),('Qoder',),(('main.sqlite','sqlite'),),platform='nt',environ={}).path,root)
    def test_root_ambiguity_never_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            for name in ('One','Two'):
                root=home/'AppData/Roaming'/name;root.mkdir(parents=True);(root/'index.json').write_text('{}')
            self.assertEqual(paths.app_support_root(home,(),('One','Two'),(('index.json','json'),),platform='nt',environ={}).state,'ambiguous')
    def test_helper_path_not_selected_from_request(self):
        self.assertIsNone(support.validate_helper_path('relative.exe'))
        self.assertEqual(support.strip_helper_keys({'native_helper':'rogue.exe','text':'safe'}),{'text':'safe'})
    def test_plain_loopback_and_no_shell(self):
        for url in ('ws://remote:18789','ws://user@127.0.0.1:18789','ws://127.0.0.1:18789/?token=secret','wss://127.0.0.1:18789'):
            with patch('supervisor.openclaw_runtime.own_config',return_value={'url':url}):self.assertIsNone(local_url())
        with patch('supervisor.openclaw_runtime.own_config',return_value={'url':'ws://127.0.0.1:18789'}):self.assertEqual(local_url(),'ws://127.0.0.1:18789')
    def test_native_payload_never_accepts_title_proof_from_request(self):
        p=win._payload({'id':'fixture','title':'Fixture','app_id':'codex','title_unique':True,'supervisor_verified':True},'snapshot',None)
        self.assertNotIn('title_unique',p);self.assertNotIn('supervisor_verified',p);self.assertFalse(p['enter_fallback'])
    def test_metadata_digest_and_identity_fail_closed(self):
        self.assertFalse(win._identity_ok({'identity':True,'stable_id':'wrong'},'fixture'))
        self.assertFalse(win._identity_ok({'identity':True,'stable_id':'fixture','title_only':True},'fixture'))
        with self.assertRaises(Blocked):win._composer_value({'empty':True,'digest':'0'*64},None)
    def test_windows_backend_new_round_after_send_is_not_old_state_failure(self):
        backend=object.__new__(win.WindowsBackend);backend.request={'id':'fixture'};backend._written='hello';backend.stage=''
        calls=[];backend.guard=lambda **kwargs:calls.append('guard')
        backend.snapshot=lambda:dict(identity=True,input_count=1,value='hello',busy=False)
        backend._invoke=lambda _:dict(code='sent',focused=True,attempted=True)
        backend.send();self.assertEqual(calls,['guard']);self.assertEqual(backend.stage,'send')
    def test_full_native_protocol_uses_fresh_title_proof_and_one_send(self):
        import hashlib,time
        from supervisor.bridge import Journal,perform
        with tempfile.TemporaryDirectory() as tmp:
            helper=Path(tmp)/'helper.exe';helper.write_bytes(b'fixture; never executed')
            now=time.time();sid='codex:11111111-1111-4111-8111-111111111111'
            row=dict(id=sid,app_id='codex',title='Fixture',project='C:\\Fixture',source='local-session',target='codex://threads/11111111-1111-4111-8111-111111111111',status='interrupted',started_at=now-20,title_unique=True)
            request={**row,'key':sid+':'+str(int((now-20)*1000)),'mode':'send','text':'hello','overwrite_draft':True}
            value=[''];stages=[]
            def runner(argv,data,timeout):
                payload=json.loads(data);stages.append(payload['stage']);self.assertTrue(payload['supervisor_verified']);self.assertTrue(payload['title_unique'])
                if payload['stage']=='write':value[0]=payload['text']
                if payload['stage']=='send':row['status']='running';row['started_at']=now+1
                code='sent' if payload['stage']=='send' else 'written' if payload['stage']=='write' else 'ready'
                result=dict(code=code,identity=True,stable_id=sid,busy=False,input_count=1,send_available=True,send_enabled=True,empty=not value[0],digest=hashlib.sha256(value[0].encode()).hexdigest(),focused=True,attempted=payload['stage'] in ('write','send'))
                return 0,json.dumps(result).encode()
            backend=win.WindowsBackend(request,helper=helper,reader=lambda _:dict(row),identity=lambda _:None,idle=lambda:10,locked=lambda:False,runner=runner)
            result=perform(request,backend,Journal(Path(tmp)/'journal'))
            self.assertEqual(result['code'],'sent_pending_confirmation');self.assertEqual(stages.count('write'),1);self.assertEqual(stages.count('send'),1)
    def test_window_title_uniqueness_cannot_be_supplied_by_request(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            helper=Path(tmp)/'helper.exe';helper.write_bytes(b'fixture')
            request=dict(id='codex:fixture',app_id='codex',title='Fixture',project='C:\\Fixture',source='local-session',target='codex://threads/fixture',status='interrupted',started_at=time.time()-20,key='fixture',title_unique=True)
            row={**request,'title_unique':False}
            backend=win.WindowsBackend(request,helper=helper,reader=lambda _:dict(row),identity=lambda _:None,idle=lambda:10,locked=lambda:False)
            backend.guard(fresh=True);self.assertFalse(backend._title_unique)
    def test_script_startup_does_not_depend_on_checkout_cwd(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(__file__).resolve().parents[1]
            env={**os.environ,'HOME':tmp,'USERPROFILE':tmp,'APPDATA':str(Path(tmp)/'AppData/Roaming'),'LOCALAPPDATA':str(Path(tmp)/'AppData/Local')}
            result=subprocess.run([sys.executable,'-B',str(root/'collector/main.py')],cwd=tmp,env=env,capture_output=True,text=True,encoding="utf-8",timeout=20)
            self.assertEqual(result.returncode,0,result.stderr[-500:]);self.assertIsInstance(json.loads(result.stdout)['sessions'],list)
    def test_frozen_worker_uses_only_isolated_fixture(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('worker_test',Path(__file__).parents[1]/'scripts/windows_worker.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'result.json';self.assertEqual(module.self_test(output),0)
            result=json.loads(output.read_text());self.assertTrue(result['all_passed']);self.assertEqual(result['external_sends'],0);self.assertFalse(result['formal_data_touched'])
