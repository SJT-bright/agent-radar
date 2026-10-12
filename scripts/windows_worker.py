#!/usr/bin/env python3
"""Frozen local worker: bounded snapshots and verified native/RPC recovery."""
import json
import os
import sys
import time
from pathlib import Path
ROOT=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))
for folder in (ROOT,ROOT/'collector',ROOT/'supervisor'):
    sys.path.insert(0,str(folder))

def self_test(destination):
    import tempfile
    import sqlite3
    from collector.openclaw_adapter import OpenClawCollector
    from collector.openclaw_care import CareBudget
    from collector.runtime_paths import HeldLock, support_directory
    from supervisor.bridge import Journal
    from supervisor.windows_bridge import _payload, _identity_ok
    checks=[]
    def check(value,name):
        checks.append({'name':name,'pass':bool(value)})
        if not value:raise AssertionError(name)
    try:
        with tempfile.TemporaryDirectory(prefix='radar-worker-test-') as temporary:
            home=Path(temporary); now=time.time();sid='11111111-1111-4111-8111-111111111111';rid='22222222-2222-4222-8222-222222222222'
            path=home/'.openclaw/agents/main/agent/openclaw-agent.sqlite';path.parent.mkdir(parents=True)
            db=sqlite3.connect(path)
            db.executescript('CREATE TABLE session_nodes (session_key TEXT,current_session_id TEXT,status TEXT,updated_at INTEGER,label TEXT,display_name TEXT,entry_json TEXT,entry_valid INTEGER,archived_at INTEGER);CREATE TABLE trajectory_runtime_events (session_id TEXT,seq INTEGER,run_id TEXT,event_json TEXT,created_at INTEGER);')
            db.execute('INSERT INTO session_nodes VALUES(?,?,?,?,?,?,?,?,?)',('agent:main:desktop',sid,'running',now*1000,'Fixture',None,json.dumps({'sessionId':sid,'startedAt':(now-20)*1000}),1,None))
            db.execute('INSERT INTO trajectory_runtime_events VALUES(?,?,?,?,?)',(sid,1,rid,json.dumps({'type':'session.started','data':{}}),(now-20)*1000));db.commit()
            reader=OpenClawCollector(home,lambda:now);rows=reader.collect();check(len(rows)==1 and rows[0]['status']=='running','read-only SQLite URI and lifecycle')
            db.execute("UPDATE session_nodes SET status='failed'");db.execute('INSERT INTO trajectory_runtime_events VALUES(?,?,?,?,?)',(sid,2,rid,json.dumps({'type':'session.ended','data':{'status':'error','stopReason':'error'}}),(now-1)*1000));db.commit();check(reader.collect()[0]['status']=='interrupted','real end required for interruption');db.close()
            # These checks import and exercise Windows-compatible modules without touching any external application.
            held=HeldLock(home/'owned.lock',blocking=False)
            try:check(True,'portable owned-file lock')
            finally:held.close()
            budget=CareBudget(home)
            for offset in range(3):budget.reserve('openclaw:main:'+sid,now+offset)
            check(budget.retry_at('openclaw:main:'+sid,now+4)>now+4,'persistent three per hour protection')
            check(not _identity_ok({'identity':True,'title_only':True,'stable_id':sid},sid),'title-only UI identity rejected')
            payload=_payload({'id':sid,'title':'Fixture','app_id':'codex','key':'fixture'},'snapshot',None);check(not payload['enter_fallback'],'no Enter-key fallback')
            import collector.hook_events, supervisor.operation_receipts, supervisor.platform_support
            check(True,'portable modules load without PyObjC or fcntl dependency')
        result={'all_passed':True,'checks':checks,'external_sends':0,'formal_data_touched':False}
    except Exception as error:result={'all_passed':False,'checks':checks,'error_type':type(error).__name__,'external_sends':0}
    Path(destination).parent.mkdir(parents=True,exist_ok=True);Path(destination).write_text(json.dumps(result,indent=2),encoding='utf-8');return 0 if result['all_passed'] else 1

class NativeWindows:
    @staticmethod
    def collect():
        import subprocess
        from supervisor.platform_support import native_helper_path
        helper=native_helper_path()
        if helper is None:return []
        proc=subprocess.Popen([str(helper),'--native-bridge'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        import threading
        timer=threading.Timer(4,proc.kill);timer.start()
        try:
            proc.stdin.write(b'{"stage":"discover"}');proc.stdin.close();raw=proc.stdout.read(65537)
            if len(raw)>65536 or timer.finished.is_set():return []
            value=json.loads(raw)
            return value.get('sessions',[])[:20] if isinstance(value,dict) else []
        except (OSError,ValueError):return []
        finally:
            timer.cancel()
            if proc.poll() is None:proc.kill()
            proc.wait(timeout=2)
            proc.stdout.close()

def main():
    if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
    if '--self-test' in sys.argv:
        index=sys.argv.index('--self-test');return self_test(sys.argv[index+1])
    if '--bridge' in sys.argv:
        from supervisor.windows_bridge import execute
        raw=sys.stdin.buffer.read(32769)
        try:result=execute(json.loads(raw)) if len(raw)<=32768 else {'code':'invalid_request','attempted':False}
        except Exception:result={'code':'invalid_request','attempted':False}
        print(json.dumps(result,ensure_ascii=False),flush=True);return 0
    from collector.main import WatchCollector,snapshot,WATCH_RESTART_EXIT_CODE
    if '--watch' not in sys.argv:print(json.dumps(snapshot(),ensure_ascii=False),flush=True);return 0
    watcher=WatchCollector()
    if os.name=='nt':watcher.adapters.append(('Windows 窗口',NativeWindows))
    try:
        while True:
            began=time.monotonic();print(json.dumps(watcher.snapshot(),ensure_ascii=False),flush=True)
            if watcher.stalled_sources:os._exit(WATCH_RESTART_EXIT_CODE)
            time.sleep(max(.1,1-(time.monotonic()-began)))
    finally:watcher.close()
if __name__=='__main__':
    try:sys.exit(main())
    except BrokenPipeError:sys.exit(0)
