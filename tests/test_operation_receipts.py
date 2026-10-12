import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from supervisor import bridge
from supervisor import operation_receipts as receipts


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def fields(operation_id=None, **extra):
    base = {
        'operation_id': operation_id or receipts.new_operation_id(),
        'session_hash': sha('session'),
        'key_hash': sha('round'),
        'app_id': 'codex',
        'stage': 'send',
        'code': 'sent_pending_confirmation',
        'attempted': True,
        'time': 1700000000,
        'elapsed_ms': 12,
    }
    base.update(extra)
    return base


def mode_of(path):
    return os.stat(path).st_mode & 0o777


class OperationReceiptTests(unittest.TestCase):
    def test_app_ids_match_the_bridge(self):
        self.assertEqual(receipts.APP_IDS, set(bridge.BUNDLES))

    def test_generated_id_is_32_hex_and_uuid_text_canonicalizes(self):
        identifier = receipts.new_operation_id()
        self.assertEqual(receipts.canonical_operation_id(identifier), identifier)
        self.assertEqual(len(identifier), 32)
        self.assertIsNone(receipts.canonical_operation_id('SECRET TITLE'))
        self.assertIsNone(receipts.canonical_operation_id('../../last-recovery.json'))
        hyphenated = 'A1B2C3D4-E5F6-4789-A012-3456789ABCDE'
        self.assertEqual(receipts.canonical_operation_id(hyphenated), 'a1b2c3d4e5f64789a0123456789abcde')

    def test_rejects_titles_paths_bodies_requests_and_exception_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = 'SECRET TITLE /Users/secret/path SECRET BODY'
            with self.assertRaises(receipts.ReceiptRejected) as caught:
                receipts.record(tmp, **fields(title='SECRET TITLE', path='/Users/secret/path',
                                              text='SECRET BODY', request={'text': 'SECRET BODY'},
                                              error='RuntimeError: ' + secret))
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertEqual(list(Path(tmp).iterdir()), [])
            for bad in (
                {'code': 'RuntimeError: ' + secret},
                {'stage': 'send ' + secret},
                {'attempted': 'false'},
                {'attempted': 0},
                {'elapsed_ms': True},
                {'time': float('nan')},
                {'session_hash': 'codex:SECRET SESSION'},
                {'app_id': '/Users/secret/path'},
                {'operation_id': secret},
            ):
                with self.assertRaises(receipts.ReceiptRejected):
                    receipts.record(tmp, **fields(**bad))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_bad_lines_do_not_drop_their_neighbors(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = receipts.sanitize(fields(time=1700000001))
            second = receipts.sanitize(fields(time=1700000002, app_id='grok', stage='route',
                                              code='target_unverified', attempted=False))
            leaked = dict(second, title='SECRET TITLE', path='/Users/secret/path',
                          text='SECRET BODY', request={'text': 'SECRET BODY'},
                          error='SECRET EXCEPTION TEXT')
            blob = receipts._encode([first]) + b'\xff not json\n' + (json.dumps(leaked) + '\n').encode()
            blob += receipts._encode([second]) + b'{"operation_id":\n'
            path = directory / receipts.FILENAME
            path.write_bytes(blob)
            before = path.read_bytes()
            loaded = receipts.load(directory)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([row['operation_id'] for row in loaded],
                             [first['operation_id'], second['operation_id']])
            self.assertNotIn('title', loaded[0])
            self.assertNotIn('title', loaded[1])
            third = receipts.record(directory, **fields(time=1700000003, app_id='zcode', stage='cancel',
                                                       code='cancelled', attempted=False))
            text = path.read_text()
            self.assertNotIn('SECRET', text)
            self.assertNotIn('not json', text)
            self.assertIn(first['operation_id'], text)
            self.assertIn(second['operation_id'], text)
            self.assertIn(third['operation_id'], text)
            self.assertEqual(mode_of(path), 0o600)
            self.assertFalse(any(name.endswith('.tmp') or '.tmp' in name for name in os.listdir(directory)
                                 if name != receipts.LOCK_NAME))

    def test_bounds_operations_stages_and_bytes_and_forces_private_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with patch.object(receipts, 'MAX_STAGES', 2):
                identifier = receipts.new_operation_id()
                for stage, stamp in (('queued', 1700000001), ('send', 1700000002), ('new-round', 1700000003)):
                    receipts.record(directory, **fields(identifier, stage=stage, time=stamp,
                                                        code='queued' if stage == 'queued' else 'confirmed',
                                                        attempted=stage != 'queued'))
                self.assertEqual([row['stage'] for row in receipts.read_operation(directory, identifier)],
                                 ['send', 'new-round'])
            with patch.object(receipts, 'MAX_OPERATIONS', 2):
                identifiers = [receipts.new_operation_id() for _ in range(3)]
                for index, identifier in enumerate(identifiers):
                    receipts.record(directory, **fields(identifier, time=1700000010 + index))
                kept = {row['operation_id'] for row in receipts.load(directory)}
                self.assertEqual(kept, {identifiers[1], identifiers[2]})
            sample = receipts.sanitize(fields(time=1700000101))
            one = len(receipts._encode([sample]))
            with patch.object(receipts, 'MAX_BYTES', one):
                older = receipts.new_operation_id()
                newer = receipts.new_operation_id()
                receipts.record(directory, **fields(older, time=1700000201))
                receipts.record(directory, **fields(newer, time=1700000202))
                path = directory / receipts.FILENAME
                self.assertLessEqual(path.stat().st_size, one)
                self.assertEqual(mode_of(path), 0o600)
                loaded = receipts.load(directory)
                self.assertEqual([row['operation_id'] for row in loaded], [newer])
            path.write_bytes(b'x' * (receipts.MAX_READ + 1))
            path.chmod(0o644)
            before = path.read_bytes()
            with self.assertRaises(receipts.ReceiptRejected) as caught:
                receipts.record(directory, **fields())
            self.assertEqual(str(caught.exception), 'unreadable')
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(mode_of(path), 0o644)

    def test_conflict_does_not_attach_another_session_or_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            identifier = receipts.new_operation_id()
            original = receipts.record(tmp, **fields(identifier, stage='queued', code='queued', attempted=False))
            for change in ({'session_hash': sha('other-session')},
                           {'key_hash': sha('other-round')},
                           {'app_id': 'grok'}):
                with self.assertRaises(receipts.ReceiptRejected) as caught:
                    receipts.record(tmp, **fields(identifier, stage='cancel', code='cancelled',
                                                  attempted=False, time=1700000002, **change))
                self.assertEqual(str(caught.exception), 'operation_conflict')
            rows = receipts.read_operation(tmp, identifier)
            self.assertEqual(rows, [original])
            self.assertNotIn('other-session', Path(tmp, receipts.FILENAME).read_text())
            other = receipts.new_operation_id()
            receipts.record(tmp, **fields(other, app_id='grok', session_hash=sha('grok-session'),
                                          stage='timeout', code='timed_out', attempted=True, time=1700000003))
            self.assertEqual(receipts.read_operation(tmp, identifier)[0]['app_id'], 'codex')
            self.assertEqual(receipts.read_operation(tmp, other)[0]['app_id'], 'grok')
            self.assertEqual(receipts.read_operation(tmp, 'not-an-id'), [])

    def test_same_operation_keeps_swift_stages_in_arrival_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            hyphenated = 'a1b2c3d4-e5f6-4789-a012-3456789abcde'
            stages = (
                ('queued', 'queued', False),
                ('identity', 'ready', False),
                ('readback', 'ready', True),
                ('send', 'sent_pending_confirmation', True),
                ('new-round', 'confirmed', True),
                ('timeout', 'timed_out', True),
                ('cancel', 'cancelled', False),
            )
            for index, (stage, code, attempted) in enumerate(stages):
                receipts.record(tmp, **fields(hyphenated, stage=stage, code=code, attempted=attempted,
                                              time=1700000000 + index, elapsed_ms=index))
            rows = receipts.read_operation(tmp, hyphenated.upper())
            self.assertEqual([row['stage'] for row in rows], [stage for stage, _, _ in stages])
            self.assertEqual({row['operation_id'] for row in rows}, {'a1b2c3d4e5f64789a0123456789abcde'})
            self.assertEqual({row['session_hash'] for row in rows}, {sha('session')})
            self.assertTrue(all(set(row) <= set(receipts.FIELDS) for row in rows))

    def test_read_does_not_create_a_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(receipts.load(tmp), [])
            self.assertEqual(receipts.read_operation(tmp, receipts.new_operation_id()), [])
            self.assertEqual(os.listdir(tmp), [])

    def test_concurrent_threads_and_processes_keep_each_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = []
            for index in range(8):
                specs.append({
                    'operation_id': receipts.new_operation_id(),
                    'session_hash': sha('thread-%s' % index),
                    'key_hash': sha('thread-round-%s' % index),
                    'app_id': 'codex' if index % 2 == 0 else 'grok',
                })
            barrier = threading.Barrier(len(specs))
            errors = []

            def worker(spec):
                try:
                    barrier.wait(timeout=10)
                    for step in range(10):
                        receipts.record(tmp, **fields(spec['operation_id'], session_hash=spec['session_hash'],
                                                      key_hash=spec['key_hash'], app_id=spec['app_id'],
                                                      time=1700001000 + step, elapsed_ms=step))
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(spec,)) for spec in specs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self._assert_isolated(tmp, specs, 10)

        with tempfile.TemporaryDirectory() as tmp:
            root = str(Path(__file__).resolve().parents[1])
            specs = []
            processes = []
            child = (
                'import os, sys\n'
                'sys.path.insert(0, os.environ["RECEIPT_ROOT"])\n'
                'from supervisor.operation_receipts import record\n'
                'for step in range(10):\n'
                '    record(os.environ["DIR"], operation_id=os.environ["OID"],\n'
                '           session_hash=os.environ["SESSION"], key_hash=os.environ["ROUND"],\n'
                '           app_id=os.environ["APP"], stage="send", code="sent_pending_confirmation",\n'
                '           attempted=True, time=1700002000 + step, elapsed_ms=step)\n'
            )
            for name, app in (('codex', 'codex'), ('grok', 'grok')):
                spec = {
                    'operation_id': receipts.new_operation_id(),
                    'session_hash': sha('process-' + name),
                    'key_hash': sha('process-round-' + name),
                    'app_id': app,
                }
                specs.append(spec)
                env = os.environ.copy()
                env.update({
                    'PYTHONPATH': root, 'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                    'RECEIPT_ROOT': root, 'DIR': tmp, 'OID': spec['operation_id'],
                    'SESSION': spec['session_hash'], 'ROUND': spec['key_hash'], 'APP': app,
                })
                processes.append(subprocess.Popen([sys.executable, '-B', '-c', child], env=env,
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            for process in processes:
                _, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr.decode())
            self._assert_isolated(tmp, specs, 10)

    def _assert_isolated(self, directory, specs, count):
        rows = receipts.load(directory)
        by_id = {}
        for row in rows:
            by_id.setdefault(row['operation_id'], []).append(row)
        self.assertEqual(set(by_id), {spec['operation_id'] for spec in specs})
        for spec in specs:
            matched = by_id[spec['operation_id']]
            self.assertEqual(len(matched), count)
            self.assertTrue(all(row['session_hash'] == spec['session_hash'] for row in matched))
            self.assertTrue(all(row['key_hash'] == spec['key_hash'] for row in matched))
            self.assertTrue(all(row['app_id'] == spec['app_id'] for row in matched))
            self.assertEqual(sorted(row['elapsed_ms'] for row in matched), list(range(count)))


class BridgeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.assertIs(bridge.receipts, receipts)
        support = patch.object(bridge.receipts, 'support_directory', return_value=self.directory)
        journal = patch('supervisor.bridge.Journal', side_effect=AssertionError('journal'))
        backend = patch('supervisor.bridge.MacBackend', side_effect=AssertionError('backend'))
        support.start()
        journal.start()
        backend.start()
        self.addCleanup(backend.stop)
        self.addCleanup(journal.stop)
        self.addCleanup(support.stop)

    def run_main(self, payload):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        output = io.StringIO()
        with patch('sys.stdin', type('Input', (), {'buffer': io.BytesIO(raw)})()), redirect_stdout(output):
            bridge.main()
        text = output.getvalue()
        return json.loads(text), text

    def assert_no_receipt_files(self):
        names = set(os.listdir(self.directory))
        self.assertNotIn(receipts.FILENAME, names)
        self.assertNotIn(receipts.LEGACY_NAME, names)

    def test_script_import_does_not_run_the_bridge(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, '-B', '-c',
                                    'import runpy; runpy.run_path("supervisor/bridge.py", run_name="bridge_import_check")'],
                                   cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assert_no_receipt_files()

    def test_inspect_judge_and_health_write_nothing(self):
        class InspectBackend:
            def __init__(self, request):
                self.stage = 'send'
                self.did_write = True

            def inspect(self):
                return {'code': 'ready', 'attempted': False, 'identity': True, 'input_count': 1,
                        'busy': False, 'send_exists': True, 'send_enabled': True,
                        'send_deferred': False, 'empty_composer': False}

        payload = {'mode': 'inspect', 'app_id': 'codex', 'key': 'SECRET KEY', 'id': 'codex:SECRET SESSION',
                   'title': 'SECRET TITLE', 'project': '/Users/secret/path', 'text': 'SECRET BODY'}
        with patch('supervisor.bridge.MacBackend', InspectBackend):
            result, raw = self.run_main(payload)
        self.assertEqual(result['code'], 'ready')
        self.assertNotIn('operation_id', result)
        self.assertNotIn('SECRET', raw)
        self.assert_no_receipt_files()

        with patch('supervisor.completion_judge.judge', return_value={'code': 'judgement_unknown', 'attempted': False}):
            result, raw = self.run_main({'mode': 'judge', 'title': 'SECRET TITLE', 'text': 'SECRET BODY'})
        self.assertEqual(result, {'code': 'judgement_unknown', 'attempted': False})
        self.assert_no_receipt_files()

        import types
        saved = {name: sys.modules.get(name) for name in ('aiwatch', 'aiwatch.mac', 'aiwatch.mac.ax')}
        ax = types.ModuleType('ax')
        ax.trusted = lambda: False
        mac = types.ModuleType('mac')
        mac.ax = ax
        package = types.ModuleType('aiwatch')
        package.mac = mac
        sys.modules['aiwatch'] = package
        sys.modules['aiwatch.mac'] = mac
        sys.modules['aiwatch.mac.ax'] = ax
        try:
            result, _ = self.run_main({'mode': 'health'})
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        self.assertEqual(result, {'code': 'permission_required', 'attempted': False})
        self.assert_no_receipt_files()

    def test_stdout_returns_the_operation_id_without_request_secrets(self):
        identifier = receipts.new_operation_id()
        secret_key = 'SECRET KEY round'
        secret_id = 'codex:SECRET SESSION'

        class Exploding:
            def __init__(self, request):
                raise RuntimeError('SECRET EXCEPTION TEXT /Users/secret/trace')

        payload = {'mode': 'send', 'app_id': 'codex', 'key': secret_key, 'id': secret_id,
                   'title': 'SECRET TITLE', 'project': '/Users/secret/path', 'text': 'SECRET BODY',
                   'operation_id': identifier}
        with patch('supervisor.bridge.MacBackend', Exploding):
            result, raw = self.run_main(payload)
        self.assertEqual(result['operation_id'], identifier)
        self.assertEqual(result['code'], 'bridge_error')
        self.assertFalse(result['attempted'])
        self.assertNotIn('SECRET', raw)
        log = (self.directory / receipts.FILENAME).read_text()
        legacy = json.loads((self.directory / receipts.LEGACY_NAME).read_text())
        self.assertNotIn('SECRET', log)
        self.assertNotIn('SECRET', json.dumps(legacy))
        self.assertNotIn('error_type', log)
        self.assertEqual(legacy['error_type'], 'RuntimeError')
        rows = receipts.read_operation(self.directory, identifier)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['stage'], 'preflight')
        self.assertEqual(rows[0]['code'], 'bridge_error')
        self.assertEqual(rows[0]['session_hash'], sha(secret_id))
        self.assertEqual(rows[0]['key_hash'], sha(secret_key))
        self.assertEqual(rows[0]['app_id'], 'codex')
        self.assertEqual(legacy['operation_id'], identifier)
        self.assertEqual(legacy['key_hash'], rows[0]['key_hash'])
        self.assertEqual(legacy['stage'], 'preflight')
        self.assertEqual(legacy['time'], rows[0]['time'])
        self.assertEqual(legacy['elapsed_ms'], rows[0]['elapsed_ms'])
        self.assertEqual(mode_of(self.directory / receipts.FILENAME), 0o600)
        self.assertEqual(mode_of(self.directory / receipts.LEGACY_NAME), 0o600)
        for key in ('code', 'attempted', 'stage', 'app_id', 'key_hash', 'time', 'elapsed_ms', 'operation_id'):
            self.assertIn(key, legacy)

    def test_invalid_operation_id_is_replaced_and_not_stored(self):
        result, raw = self.run_main({'mode': 'send', 'app_id': 'not-a-real-app', 'operation_id': 'SECRET OPERATION',
                                     'title': 'SECRET TITLE', 'text': 'SECRET BODY', 'key': 'SECRET KEY',
                                     'id': 'codex:SECRET SESSION', 'project': '/Users/secret/path'})
        self.assertEqual(result['code'], 'unsupported')
        self.assertNotIn('SECRET', raw)
        self.assertEqual(len(result['operation_id']), 32)
        text = (self.directory / receipts.FILENAME).read_text() + (self.directory / receipts.LEGACY_NAME).read_text()
        self.assertNotIn('SECRET', text)
        self.assertIn(result['operation_id'], text)
        row = receipts.read_operation(self.directory, result['operation_id'])[0]
        self.assertNotIn('app_id', row)
        self.assertEqual(row['code'], 'unsupported')
        self.assertFalse(row['attempted'])
        legacy = json.loads((self.directory / receipts.LEGACY_NAME).read_text())
        self.assertEqual(legacy['app_id'], '')  # Unknown input strings are never persisted.
        self.assertEqual(legacy['code'], 'unsupported')

    def test_later_app_overwrites_latest_file_and_keeps_the_operation_trace(self):
        codex_id = receipts.new_operation_id()
        grok_id = 'bbccddee-0011-4223-8445-66778899aabb'
        codex = {'mode': 'send', 'app_id': 'codex', 'operation_id': codex_id, 'id': 'codex:SECRET SESSION',
                 'key': 'SECRET KEY codex', 'title': 'SECRET TITLE', 'text': 'SECRET BODY',
                 'project': '/Users/secret/codex'}
        grok = {'mode': 'send', 'app_id': 'grok', 'operation_id': grok_id, 'id': 'grok:other-session',
                'key': 'grok-round', 'title': 'OTHER TITLE', 'text': 'OTHER BODY', 'project': '/Users/secret/grok'}
        began = time.monotonic()
        first = bridge._persist_receipts(codex, {'code': 'sent_pending_confirmation', 'attempted': True, 'sent_at': 321.5},
                                         SimpleNamespace(stage='send', did_write=True), began)
        second = bridge._persist_receipts(grok, {'code': 'target_unverified', 'attempted': False},
                                          SimpleNamespace(stage='route', did_write=False), began)
        self.assertEqual(first, codex_id)
        self.assertEqual(second, 'bbccddee00114223844566778899aabb')
        legacy = json.loads((self.directory / receipts.LEGACY_NAME).read_text())
        self.assertEqual(legacy['app_id'], 'grok')
        self.assertEqual(legacy['operation_id'], second)
        self.assertEqual(legacy['code'], 'target_unverified')
        self.assertEqual(legacy['stage'], 'route')
        self.assertEqual(legacy['sent_at'] if 'sent_at' in legacy else None, None)
        codex_rows = receipts.read_operation(self.directory, codex_id)
        grok_rows = receipts.read_operation(self.directory, grok_id)
        self.assertEqual(len(codex_rows), 1)
        self.assertEqual(codex_rows[0]['stage'], 'send')
        self.assertEqual(codex_rows[0]['app_id'], 'codex')
        self.assertTrue(codex_rows[0]['attempted'])
        self.assertEqual(codex_rows[0]['code'], 'sent_pending_confirmation')
        self.assertNotIn('sent_at', codex_rows[0])
        self.assertEqual(grok_rows[0]['stage'], 'route')
        self.assertEqual(grok_rows[0]['app_id'], 'grok')
        self.assertFalse(grok_rows[0]['attempted'])
        self.assertNotEqual(codex_rows[0]['session_hash'], grok_rows[0]['session_hash'])
        self.assertEqual(codex_rows[0]['session_hash'], sha('codex:SECRET SESSION'))
        blob = (self.directory / receipts.FILENAME).read_text()
        self.assertNotIn('SECRET', blob)
        self.assertNotIn('OTHER BODY', blob)
        self.assertNotIn('/Users/secret', blob)
        # A poisoned stage is not a place to hide a title.
        poisoned = bridge._persist_receipts(
            {'app_id': 'zcode', 'id': 'zcode:1', 'key': 'round', 'operation_id': receipts.new_operation_id()},
            {'code': 'ready', 'attempted': False}, SimpleNamespace(stage='SECRET STAGE', did_write=False), began)
        self.assertNotIn('SECRET', (self.directory / receipts.FILENAME).read_text())
        self.assertEqual(receipts.read_operation(self.directory, poisoned)[0]['stage'], 'preflight')
        receipts.record(self.directory, operation_id=codex_id, session_hash=sha('codex:SECRET SESSION'),
                        key_hash=sha('SECRET KEY codex'), app_id='codex', stage='new-round',
                        code='confirmed', attempted=True, time=1700000300, elapsed_ms=30)
        receipts.record(self.directory, operation_id=codex_id, session_hash=sha('codex:SECRET SESSION'),
                        key_hash=sha('SECRET KEY codex'), app_id='codex', stage='cancel',
                        code='cancelled', attempted=True, time=1700000301, elapsed_ms=31)
        self.assertEqual([row['stage'] for row in receipts.read_operation(self.directory, codex_id)],
                         ['send', 'new-round', 'cancel'])
        self.assertEqual([row['stage'] for row in receipts.read_operation(self.directory, second)], ['route'])


if __name__ == '__main__':
    unittest.main()
