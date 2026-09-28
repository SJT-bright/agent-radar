"""Bounded, on-demand reply reading. No cache, logging, writes or network.

Returned text is transient classifier input, never part of collector output.
An absent boundary, changed snapshot or unsupported schema returns None.
"""
from datetime import datetime
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
import sqlite3
try:
    from .transcript_scan import scan_blocks, complete_lines
except ImportError:
    from transcript_scan import scan_blocks, complete_lines

MAX_TEXT = 6000
TAIL_BYTES = 512 * 1024
DONE = {'completed', 'complete', 'done', 'success', 'succeeded'}


def epoch(value):
    try:
        number = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if isinstance(value, str) and 'T' in value else float(value or 0)
        if not math.isfinite(number) or number <= 0:
            return 0
        return number / 1000 if number > 100_000_000_000 else number
    except (ValueError, TypeError, OverflowError):
        return 0


def _same(a, b):
    return epoch(a) > 0 and abs(epoch(a) - epoch(b)) < 0.0005


@contextmanager
def _db(path):
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.25)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    try:
        yield con
    finally:
        con.close()


def _contained(path, root):
    path, root = path.resolve(), root.resolve()
    if not path.is_relative_to(root):
        raise ValueError('outside root')
    return path


def _lines(path, root, boundary):
    """Current-turn records in file order. Reads the tail first, then walks
    earlier blocks backwards until `boundary(row)` marks the turn start or
    the cap is reached, so long turns stay judgable on big logs."""
    path = _contained(path, root)
    before = path.stat()
    parsed_blocks = []
    with path.open('rb') as stream:
        for index, (start, end) in enumerate(scan_blocks(before.st_size, window=TAIL_BYTES)):
            stream.seek(start)
            data = stream.read(end - start)
            if index == 0 and not data.endswith(b'\n'):
                raise ValueError('partial record')
            block = [json.loads(line) for line in complete_lines(data, start > 0) if line.strip()]
            if not all(isinstance(row, dict) for row in block):
                raise ValueError('unexpected record')
            parsed_blocks.append(block)
            if any(boundary(row) for row in block):
                break
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('changed while reading')
    # Blocks were read tail-first; rows must go back to file order.
    return [row for block in reversed(parsed_blocks) for row in block]


def _text(content):
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                return None
            if part.get('type') in {'thinking', 'reasoning'}:
                continue
            if part.get('type') not in {'text', 'output_text'} or not isinstance(part.get('text'), str):
                return None
            parts.append(part['text'])
        text = '\n'.join(parts)
    else:
        return None
    text = text.strip()
    return text if text and len(text) <= MAX_TEXT else None


def _workbuddy(request, home, sid):
    root = home / ('.workbuddy-ai' if request['app_id'] == 'workbuddy-ai' else '.workbuddy')
    with _db(root / 'workbuddy.db') as db:
        row = db.execute('SELECT cwd,status,updated_at,last_activity_at FROM sessions WHERE id=? AND deleted_at IS NULL', (sid,)).fetchone()
    if not row or row['status'] not in DONE or row['cwd'] != request.get('project'):
        return None
    project = row['cwd']
    if not isinstance(project, str) or not project.startswith('/'):
        return None
    rows = _lines(root / 'projects' / project.strip('/').replace('/', '-') / (sid + '.jsonl'), root / 'projects',
                  lambda item: item.get('type') == 'message' and item.get('role') == 'user')
    start, last = 0, None
    for item in rows:
        kind, role = item.get('type'), item.get('role')
        # Native WorkBuddy adds file-history-snapshot bookkeeping without a
        # sessionId. It is not a chat event and carries no assistant reply.
        if kind == 'file-history-snapshot' and role is None and 'sessionId' not in item:
            continue
        if item.get('sessionId') != sid:
            return None  # Mixed chat sessions are not a usable transcript.
        if kind == 'message' and role == 'user':
            start, last = epoch(item.get('timestamp')), item
        elif kind == 'message' or kind in {'function_call', 'function_call_result', 'reasoning'}:
            last = item
    if not _same(start, request['started_at']) or not last or last.get('role') != 'assistant' or last.get('type') != 'message':
        return None
    stamp = epoch(last.get('timestamp'))
    if last.get('status') not in DONE or stamp < start or max(epoch(row['updated_at']), epoch(row['last_activity_at'])) < start:
        return None
    if isinstance(last.get('providerData'), dict) and last['providerData'].get('error'):
        return None
    return _text(last.get('content'))


def _codex(request, home, sid):
    root = home / '.codex'
    databases = sorted(root.glob('state_*.sqlite'), key=lambda p: int(p.stem.split('_')[-1]) if p.stem.split('_')[-1].isdigit() else -1, reverse=True)
    if not databases:
        return None
    with _db(databases[0]) as db:
        row = db.execute('SELECT rollout_path,cwd FROM threads WHERE id=? AND archived=0', (sid,)).fetchone()
    if not row or row['cwd'] != request.get('project'):
        return None
    path = Path(row['rollout_path'])
    rows = _lines(path, root / 'sessions',
                  lambda item: item.get('type') == 'event_msg' and isinstance(item.get('payload'), dict)
                  and item['payload'].get('type') == 'task_started')
    start, turn, ended, last, text = 0, None, False, None, None
    for item in rows:
        payload = item.get('payload')
        if not isinstance(payload, dict):
            continue
        kind, subtype = item.get('type'), payload.get('type')
        if kind == 'session_meta' and payload.get('id') != sid:
            return None
        if kind == 'event_msg' and subtype == 'task_started':
            start, turn, ended, last, text = epoch(item.get('timestamp')), payload.get('turn_id'), False, None, None
        elif kind == 'event_msg' and subtype in {'task_complete', 'task_failed', 'turn_aborted', 'request_user_input', 'exec_approval_request', 'apply_patch_approval_request'}:
            if subtype != 'task_complete' or not turn or payload.get('turn_id') != turn:
                ended = False
                last = 'blocked'
            else:
                ended = True
        elif kind == 'response_item':
            if subtype == 'message':
                last = payload.get('role')
                # Final channel is required; intermediate commentary is not a completion report.
                text = _text(payload.get('content')) if last == 'assistant' and payload.get('phase', payload.get('channel')) in {'final', 'final_answer'} else None
            elif subtype in {'function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output'}:
                last, text = 'tool', None
    if not _same(start, request['started_at']) or not ended or last != 'assistant':
        return None
    return text


def _autoclaw(request, home, sid):
    root = home / '.openclaw-autoclaw' / 'agents'
    key = request.get('navigation_key')
    match = re.fullmatch(r'agent:([A-Za-z0-9_-]+):[A-Za-z0-9_-]+', key or '')
    if not match:
        return None
    directory = _contained(root / match[1] / 'sessions', root)
    index = directory / 'sessions.json'
    if index.stat().st_size > 8 * 1024 * 1024:
        return None
    data = json.loads(index.read_bytes())
    entry = data.get(key) if isinstance(data, dict) else None
    if not isinstance(entry, dict) or entry.get('sessionId') != sid or entry.get('abortedLastRun'):
        return None
    if entry.get('status') in {'running', 'failed', 'killed', 'timeout'} or epoch(entry.get('autoclawUserStoppedAt')) >= request['started_at']:
        return None
    path = _contained(directory / (sid + '.jsonl'), directory)
    with path.open('rb') as stream:
        header = json.loads(stream.readline(16385))
    if not isinstance(header, dict) or header.get('type') != 'session' or header.get('id') != sid or header.get('cwd') != request.get('project'):
        return None
    rows = _lines(path, directory,
                  lambda item: isinstance(item.get('message'), dict) and item['message'].get('role') == 'user')
    start, last, last_stamp = 0, None, 0
    for item in rows:
        message = item.get('message')
        if item.get('type') == 'session' and item.get('id', sid) != sid:
            return None
        if not isinstance(message, dict):
            continue
        if message.get('role') == 'user':
            start = epoch(item.get('timestamp') or message.get('timestamp'))
        if message.get('role') in {'user', 'assistant', 'toolResult'}:
            last = message
            last_stamp = epoch(item.get('timestamp') or message.get('timestamp'))
    if not _same(start, request['started_at']) or epoch(entry.get('startedAt')) > start or not last:
        return None
    if last_stamp < start or last.get('role') != 'assistant' or last.get('stopReason') not in {'stop', 'end_turn'}:
        return None
    return _text(last.get('content'))


def read_reply(request, home=None):
    """Return just this completed round's complete final reply, or None."""
    try:
        home = Path(home) if home is not None else Path.home()
        app = request.get('app_id')
        if app not in {'codex', 'workbuddy', 'workbuddy-ai', 'autoclaw'}:
            return None
        if request.get('source') not in {'local-session', 'local-log', 'local-db'}:
            return None
        prefix = app + ':'
        identity = request.get('id')
        if not isinstance(identity, str) or not identity.startswith(prefix):
            return None
        sid = identity[len(prefix):]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', sid) or not epoch(request.get('started_at')):
            return None
        function = _codex if app == 'codex' else _autoclaw if app == 'autoclaw' else _workbuddy
        return function(request, home, sid)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, OverflowError):
        return None
