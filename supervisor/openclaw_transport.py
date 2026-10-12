"""Resume one verified existing OpenClaw session through its local gateway.

The installed CLI resolves its own authentication; Radar never opens credentials.
No --local, delivery, model overrides, new sessions, stop, steer or interrupt.
"""
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

LOCAL_URL = 'ws://127.0.0.1:19189'


def cli_path():
    candidates = [Path.home() / '.local/bin/openclaw', Path('/opt/homebrew/bin/openclaw'),
                  Path('/usr/local/bin/openclaw')]
    return next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)


class OpenClawTransport:
    def __init__(self, request, blocked, collector=None, runner=subprocess.run, clock=time.time):
        try:
            from openclaw_adapter import collect_session, parse_id
        except ImportError:
            from collector.openclaw_adapter import collect_session, parse_id
        self.request, self.blocked = request, blocked
        self.collector = collector or (lambda sid: collect_session(sid, include_children=request.get('manual_retry') is True))
        self.runner, self.clock = runner, clock
        self.did_write = False
        self.stage = 'preflight'
        self.sent_at = None
        self.parse_id = parse_id

    def guard(self):
        request = self.request
        parsed = self.parse_id(request.get('id'))
        if not parsed or request.get('app_id') != 'openclaw':
            raise self.blocked('target_unverified')
        row = self.collector(request['id'])
        if not row or row.get('id') != request['id'] or row.get('app_id') != 'openclaw':
            raise self.blocked('target_unverified')
        for key in ('title', 'project', 'source', 'navigation_key'):
            if row.get(key) != request.get(key):
                raise self.blocked('target_unverified')
        if row.get('source') != 'local-db' or row.get('user_stopped'):
            raise self.blocked('user_stopped' if row.get('user_stopped') else 'target_unverified')
        if row.get('manual_only') and request.get('manual_retry') is not True:
            raise self.blocked('manual_resolution')
        expected_revision = request.get('lifecycle_revision')
        if expected_revision and expected_revision != row.get('lifecycle_revision'):
            raise self.blocked('state_changed')
        if row.get('status') == 'running':
            raise self.blocked('already_running')
        if row.get('status') not in ('completed', 'interrupted'):
            raise self.blocked('state_unverified')
        if row.get('started_at') != request.get('started_at') or row.get('timing_basis') != 'turn':
            raise self.blocked('state_changed')
        if request.get('kind') == 'followup' and row['status'] != 'completed':
            raise self.blocked('state_changed')
        if request.get('kind') != 'followup' and row['status'] != 'interrupted':
            raise self.blocked('state_changed')
        reason = row.get('status_reason', '')
        if any(token in reason for token in ('认证', '访问权限', '上下文超过')) or (
                any(token in reason for token in ('429', '配额', '频率')) and not request.get('allow_rate_limit')):
            raise self.blocked('manual_resolution')
        if ('触发者待确认' in reason or '未记录触发者' in reason) and not request.get('manual_retry'):
            raise self.blocked('user_stopped')
        if not cli_path():
            raise self.blocked('openclaw_cli_unavailable')
        return row

    def inspect(self):
        row = self.collector(self.request.get('id', ''))
        identity = bool(row and row.get('id') == self.request.get('id') and
                        row.get('app_id') == 'openclaw' and self.parse_id(row['id']) and all(
            row.get(k) == self.request.get(k) for k in ('title', 'project', 'source', 'navigation_key')))
        if not identity:
            return dict(code='target_unverified', attempted=False, identity=False, transport='local-rpc')
        code = ('already_running' if row['status'] == 'running' else
                'state_unverified' if row['status'] not in ('completed', 'interrupted') else
                'user_stopped' if row.get('user_stopped') else
                'ready' if cli_path() else 'openclaw_cli_unavailable')
        return dict(code=code, attempted=False, identity=True, transport='local-rpc',
                    busy=row['status'] == 'running', state_status=row['status'],
                    diagnostic_summary='原会话 ID 与会话键已核验；本地 Gateway RPC，不操作屏幕输入框。状态：' + row['status_reason'])

    def perform(self, journal, resolve_text):
        text = resolve_text(self.request)
        if text is None:
            raise self.blocked('text_invalid')
        journal.check(self.request)
        self.guard()
        # An offline gateway is a preparation failure, not a consumed send slot.
        env = dict(os.environ)
        for key in ('OPENCLAW_PROFILE', 'OPENCLAW_STATE_DIR', 'OPENCLAW_CONFIG_PATH', 'OPENCLAW_GATEWAY_URL'):
            env.pop(key, None)
        env['PATH'] = str(Path.home() / '.local/bin') + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'
        health_command = [cli_path(), 'gateway', 'call', 'health', '--expect-url', LOCAL_URL,
                          '--json', '--timeout', '3000']
        try:
            health = self.runner(health_command, capture_output=True, text=True, timeout=10,
                                 cwd=str(Path.home()), env=env)
            if health.returncode != 0 or len(health.stdout) > 65536 or json.loads(health.stdout).get('ok') is not True:
                raise self.blocked('openclaw_gateway_unavailable')
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            raise self.blocked('openclaw_gateway_unavailable')
        if self.request['mode'] == 'check':
            return dict(code='ready', attempted=False, transport='local-rpc')
        # Re-read the exact session directly before claiming the one send slot.
        row = self.guard()
        journal.reserve(self.request)
        self.stage = 'send'
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'AgentRadar:' + self.request['key']))
        params = dict(sessionKey=row['navigation_key'], sessionId=self.parse_id(row['id'])[1],
                      expectedLeafEntryId=None, message=text, deliver=False, idempotencyKey=run_id)
        command = [cli_path(), 'gateway', 'call', 'chat.send', '--expect-url', LOCAL_URL,
                   '--json', '--timeout', '10000', '--params', json.dumps(params, ensure_ascii=False)]
        # Never inherit a selected profile/remote instance from a parent shell.
        self.sent_at = self.clock()
        self.did_write = True  # A timeout may have accepted the request; never replay.
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=18,
                                 cwd=str(Path.home()), env=env)
            # No stderr or full response is persisted; only fixed acknowledgement fields.
            if result.returncode != 0 or len(result.stdout) > 65536:
                raise self.blocked('send_unconfirmed')
            raw = result.stdout.strip()
            ack = json.loads(raw)
            if not isinstance(ack, dict) or ack.get('runId') != run_id or ack.get('status') not in ('started', 'queued', 'ok'):
                raise self.blocked('send_unconfirmed')
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            raise self.blocked('send_unconfirmed')
        return dict(code='sent_pending_confirmation', attempted=True, sent_at=self.sent_at,
                    run_id=run_id, transport='local-rpc')
