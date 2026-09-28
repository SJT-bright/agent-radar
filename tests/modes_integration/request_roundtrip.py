"""Actual Swift controller JSON -> actual Python bridge + persisted journal."""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from supervisor.bridge import Blocked, Journal, perform
class IsolatedBackend:
    def __init__(self):
        self.value = '待覆盖的草稿'
        self.sends = 0
    def guard(self, **_): pass
    def route(self): pass
    def snapshot(self):
        return dict(identity=True, busy=False, input_count=1, value=self.value, send_exists=True, send_enabled=True)
    def write(self, text): self.value = text
    def send(self): self.sends += 1
requests = json.load(sys.stdin)
assert len(requests) == 12
with tempfile.TemporaryDirectory(prefix='radar-integration-') as temp:
    journal = Journal(Path(temp))
    backend = IsolatedBackend()
    for index, request in enumerate(requests, start=1):
        assert request['automation_mode'] == 'rage' and request['kind'] == 'followup'
        assert request['overwrite_draft'] is True and request['manual_retry'] is False
        assert request['key'] == request['id'] + ':' + str(int(request['started_at'] * 1000))
        with patch('supervisor.bridge.time.time', return_value=request['started_at'] + 5):
            assert perform(request, backend, journal) == dict(code='sent_pending_confirmation', attempted=True)
        assert backend.value == ('以用户视角优化并验证' if index % 3 == 0 else '继续优化并验证')
    assert backend.sends == 12
    journal.lock.close()
    reloaded = Journal(Path(temp))
    try:
        reloaded.check(requests[0])
        raise AssertionError('old round replay accepted')
    except Blocked as exc:
        assert str(exc) == 'already_attempted', str(exc)
    finally:
        reloaded.lock.close()
print('Swift/Python bridge roundtrip: 12 consecutive rounds + persisted replay refusal passed')
