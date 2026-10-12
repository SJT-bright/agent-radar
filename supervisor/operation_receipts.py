"""Bounded operation receipts that survive the next app's recovery.

`last-recovery.json` stores only the newest result, so a later app erases the
trace of the one before it. This log keeps a capped history keyed by
operation_id. A record may contain only the fixed metadata whitelist:

operation_id, session_hash, key_hash, app_id, stage, code, attempted, time,
elapsed_ms.

Titles, paths, prompt or chat text, the request object and exception text are
rejected. Swift can pass a 32-hex operation_id (hyphens optional) minted at
queue time; the bridge echoes the id it stored on stdout. Later queued,
identity, readback, new-round, timeout and cancel events must reuse that id.
"""
import fcntl
import json
import math
import os
import re
import secrets
import threading
from pathlib import Path

MAX_OPERATIONS = 128
MAX_STAGES = 16
MAX_BYTES = 256 * 1024
MAX_READ = 1024 * 1024
MAX_LINE = 1024
MAX_ELAPSED_MS = 24 * 60 * 60 * 1000
FILENAME = 'operation-receipts.jsonl'
LEGACY_NAME = 'last-recovery.json'
LOCK_NAME = 'operation-receipts.lock'

APP_IDS = frozenset({
    'autoclaw', 'codex', 'workbuddy', 'workbuddy-ai', 'zcode', 'grok', 'qoder-cn', 'qoder', 'openclaw',
})
# Bridge stages plus the stages the controller records around the bridge call.
STAGES = frozenset({
    'queued', 'preflight', 'route', 'identity', 'write', 'readback',
    'send', 'new-round', 'timeout', 'cancel',
})
FIELDS = (
    'operation_id', 'session_hash', 'key_hash', 'app_id',
    'stage', 'code', 'attempted', 'time', 'elapsed_ms',
)
IDENTITY = ('session_hash', 'key_hash', 'app_id')
CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')
HASH = re.compile(r'[0-9a-f]{64}')
BARE_ID = re.compile(r'[0-9a-f]{32}')
UUID_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class ReceiptRejected(ValueError):
    """A receipt was refused. The message is a fixed code, never field text."""


def support_directory():
    return Path.home() / 'Library/Application Support/AgentRadar'


def new_operation_id():
    return secrets.token_hex(16)


def canonical_operation_id(value):
    """Return 32 lowercase hex, or None when the value is not an operation id."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if BARE_ID.fullmatch(text):
        return text
    if UUID_ID.fullmatch(text):
        return text.replace('-', '')
    return None


def sanitize(fields):
    """Copy only whitelist fields. Extra keys are an error, not silent data."""
    if not isinstance(fields, dict) or set(fields) - set(FIELDS):
        raise ReceiptRejected('unexpected_field')
    operation_id = canonical_operation_id(fields.get('operation_id'))
    if operation_id is None:
        raise ReceiptRejected('operation_id')
    stage = fields.get('stage')
    if stage not in STAGES:
        raise ReceiptRejected('stage')
    code = fields.get('code')
    if not isinstance(code, str) or CODE.fullmatch(code) is None:
        raise ReceiptRejected('code')
    attempted = fields.get('attempted')
    if type(attempted) is not bool:
        raise ReceiptRejected('attempted')
    stamp = fields.get('time')
    if (isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp)
            or stamp <= 0 or stamp >= 10 ** 11):
        raise ReceiptRejected('time')
    elapsed = fields.get('elapsed_ms')
    if type(elapsed) is not int or elapsed < 0 or elapsed > MAX_ELAPSED_MS:
        raise ReceiptRejected('elapsed_ms')
    record = {
        'operation_id': operation_id,
        'stage': stage,
        'code': code,
        'attempted': attempted,
        'time': stamp,
        'elapsed_ms': elapsed,
    }
    for field in IDENTITY:
        if field not in fields:
            continue
        value = fields[field]
        if field == 'app_id':
            if value not in APP_IDS:
                raise ReceiptRejected('app_id')
        elif not isinstance(value, str) or HASH.fullmatch(value) is None:
            raise ReceiptRejected(field)
        record[field] = value
    return record


def load(directory):
    """Return valid records. Missing files and bad lines yield no extras."""
    path = Path(directory) / FILENAME
    try:
        if not path.is_file():
            return []
    except OSError:
        return []
    return _load(path)


def read_operation(directory, operation_id):
    """Events for one id only. Invalid ids and other operations contribute nothing."""
    canonical = canonical_operation_id(operation_id)
    if canonical is None:
        return []
    return [row for row in load(directory) if row['operation_id'] == canonical]


def record(directory, **fields):
    """Append one stage for an operation. Conflicting identity does not write."""
    clean = sanitize(fields)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _thread_lock(directory):
        lock_path = directory / LOCK_NAME
        with lock_path.open('a') as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                path = directory / FILENAME
                try:
                    if path.exists() and path.stat().st_size > MAX_READ:
                        raise ReceiptRejected('unreadable')
                except OSError:
                    raise ReceiptRejected('unreadable')
                existing = _load(path)
                _reject_conflict(existing, clean)
                updated = _bound(existing + [clean])
                if not any(item is clean for item in updated):
                    raise ReceiptRejected('capacity')
                _replace(directory, FILENAME, _encode(updated))
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return dict(clean)


def write_legacy(directory, payload):
    """Atomically replace the single latest-recovery file. Caller owns its shape."""
    if not isinstance(payload, dict):
        raise ReceiptRejected('payload')
    data = json.dumps(payload).encode()
    _replace(Path(directory), LEGACY_NAME, data)


def _thread_lock(directory):
    key = str(Path(directory).resolve())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _reject_conflict(existing, new):
    for row in existing:
        if row['operation_id'] != new['operation_id']:
            continue
        for field in IDENTITY:
            if field in row and field in new and row[field] != new[field]:
                raise ReceiptRejected('operation_conflict')


def _bound(records):
    by_id = {}
    order = {}
    for index, record in enumerate(records):
        by_id.setdefault(record['operation_id'], []).append(record)
        order[id(record)] = index
    for oid, rows in list(by_id.items()):
        if len(rows) > MAX_STAGES:
            by_id[oid] = rows[-MAX_STAGES:]

    def rank(oid):
        rows = by_id[oid]
        return (max(row['time'] for row in rows), max(order[id(row)] for row in rows))

    chosen = []
    used = 0
    for oid in sorted(by_id, key=rank, reverse=True):
        if len(chosen) >= MAX_OPERATIONS:
            break
        rows = by_id[oid]
        weight = len(_encode(rows))
        if used + weight > MAX_BYTES:
            if chosen:
                break
            while len(rows) > 1 and len(_encode(rows)) > MAX_BYTES:
                rows = rows[1:]
            by_id[oid] = rows
            weight = len(_encode(rows))
            if weight > MAX_BYTES or weight == 0:
                return []
        chosen.append(oid)
        used += weight
    kept = {oid: {id(row) for row in by_id[oid]} for oid in chosen}
    return [record for record in records
            if record['operation_id'] in kept and id(record) in kept[record['operation_id']]]


def _encode(records):
    lines = []
    for record in records:
        item = {key: record[key] for key in FIELDS if key in record}
        lines.append(json.dumps(item, sort_keys=True, separators=(',', ':')))
    if not lines:
        return b''
    return ('\n'.join(lines) + '\n').encode('ascii')


def _load(path):
    records = []
    try:
        if not path.is_file():
            return records
        if path.stat().st_size > MAX_READ:
            return records
        with path.open('rb') as handle:
            while True:
                line = handle.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    while not line.endswith(b'\n'):
                        line = handle.readline(MAX_LINE)
                        if not line:
                            break
                    continue
                parsed = _parse_line(line.strip())
                if parsed is not None:
                    records.append(parsed)
    except OSError:
        return records
    return records


def _parse_line(line):
    if not line:
        return None
    try:
        value = json.loads(line.decode('utf-8'))
    except (UnicodeError, ValueError, AttributeError):
        return None
    if not isinstance(value, dict) or set(value) - set(FIELDS):
        return None
    try:
        return sanitize(value)
    except ReceiptRejected:
        return None


def _replace(directory, name, data):
    if name not in (FILENAME, LEGACY_NAME):
        raise ReceiptRejected('filename')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / name
    # Unique temp so two writers cannot replace a half-written file.
    temp = directory / ('.' + name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with temp.open('wb') as handle:
            os.chmod(temp, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return path
