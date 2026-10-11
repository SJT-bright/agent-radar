"""Radar's narrow continuation bridge, using the supplied AI Watchdog mac core.

Fixed message by default; an optional, validated request `text` may replace it.
No OCR coordinates, broad window guessing, or daemon. The separate judge mode
uses only the fixed local Ollama model and cannot enter the send path.
stdout is bounded result codes; no AX tree, clipboard, or chat contents logged.
"""
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

MESSAGE = '刚才中断了，请继续'
MAX_TEXT = 2000
# Control characters other than \n (tab is left alone; surrounding whitespace
# is stripped before this check). DEL is included.
CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
BUNDLES = {'autoclaw': 'com.zhipuai.autoclaw', 'codex': 'com.openai.codex',
           'workbuddy': 'com.tencent.workbuddy.mac', 'workbuddy-ai': 'com.workbuddy.workbuddy-ai',
           'zcode': 'dev.zcode.app', 'grok': 'com.grokapp.desktop',
           'qoder-cn': 'com.qodercn.app', 'qoder': 'com.qoder.app'}
SEND_NAMES = {'发送', '发送消息', '发送提示', 'send', 'send message', 'send prompt', 'submit', '提交'}
STOP_NAMES = {'停止', '停止生成', '停止回复', 'stop', 'stop generating', 'stop response', '停止任务'}
GROK_SIDEBAR_PREFIX = '未读 — 后台回合已完成 '
GROK_SIDEBAR_AGE = re.compile(r'\s+(?:刚刚|现在|\d+\s*(?:秒钟|秒|分钟|小时|天|周|个月|年)前|昨天|前天|\d{1,2}:\d{2}|\d{1,2}月\d{1,2}日)(?: 进行中…)?$')
QODER_IDS = ('qoder', 'qoder-cn')
CHAT_COMPOSER_HINTS = {'grok': {'消息输入框', '随心输入'},
                       'qoder': {'发送任务消息'}, 'qoder-cn': {'发送任务消息'}}
CODEX_COMPOSER_HINTS = {'与 Codex 协作', 'Work with Codex'}
ZCODE_SEARCH_PLACEHOLDERS = {'搜索操作、任务或文件', 'Search actions, tasks, or files'}
ZCODE_COMPOSER_HINTS = {'提出后续修改要求', '继续输入以排队后续修改',
                        'Ask for follow-up changes', 'Keep typing to queue follow-up changes'}
# ZCode's rendered historical turns, separate from the sticky composer sibling.
# Only this exact renderer signature inside #conversation may be omitted.
ZCODE_TRANSCRIPT_CLASSES = {'@md/conversation:px-6', 'gap-5', 'pb-5'}
AUTOCLAW_SIDEBAR_AGE = re.compile(
    r'(?:有未查看回复|正在回复\.{3}|\d{1,2}:\d{2}|昨天|前天|\d{1,2}月\d{1,2}日|'
    r'\d+\s*(?:天|周|个月|年))')
AUTOCLAW_COMPOSER_HINTS = {'填写你的目标，AutoClaw会持续工作至完成目标...',
                          '发送给 AutoClaw', '输入“@”使用技能',
                          '请输入你想让 AutoClaw 对文件做什么'}


class Blocked(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.retry_after = retry_after


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Journal:
    MAX_RECORDS = 10000
    MAX_BYTES = 4 * 1024 * 1024

    @staticmethod
    def _hash(value):
        return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None

    @staticmethod
    def _start(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0

    def __init__(self, directory=None):
        self.directory = directory or Path.home() / 'Library/Application Support/AgentRadar'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'continuation-attempts.json'
        self.lock = (self.directory / 'continuation.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise Blocked('another_recovery')
        self.rows, self.latest, self.legacy = [], {}, set()
        if self.path.exists():
            try:
                if self.path.stat().st_size > self.MAX_BYTES:
                    raise Blocked('journal_capacity')
                data = json.loads(self.path.read_text())
                if isinstance(data, dict) and data.get('version') != 2:
                    raise ValueError()
                self.rows = data.get('rows') if isinstance(data, dict) else data
                self.latest = data.get('latest', {}) if isinstance(data, dict) else {}
                legacy = data.get('legacy', []) if isinstance(data, dict) else []
                if not isinstance(legacy, list) or any(not self._hash(k) for k in legacy):
                    raise ValueError()
                self.legacy = set(legacy)
                if not isinstance(self.latest, dict) or not isinstance(self.rows, list):
                    raise ValueError()
                for sid, mark in self.latest.items():
                    if (not self._hash(sid) or not isinstance(mark, dict)
                            or not self._hash(mark.get('key')) or not self._start(mark.get('start'))):
                        raise ValueError()
                for row in self.rows:
                    if (not isinstance(row, dict) or not isinstance(row.get('time'), (int, float))
                            or isinstance(row['time'], bool) or not math.isfinite(row['time'])):
                        raise ValueError()
                    key, sid = row.get('key'), row.get('session')
                    start = row.get('start')
                    if 'start' in row and not self._start(start):
                        raise ValueError()
                    if not self._hash(key):
                        # Only an unambiguous legacy plaintext stable key may
                        # reconstruct a watermark. Actual v1 keys were hashed.
                        match = re.fullmatch(r'(.+):([0-9]{12,16})', key or '') if isinstance(key, str) else None
                        if not match or sid not in (match[1], digest(match[1])):
                            raise ValueError()
                        start = int(match[2]) / 1000.0
                        if not self._start(start):
                            raise ValueError()
                        key, sid = digest(key), digest(match[1])
                        row.update(key=key, session=sid, start=start)
                    if not self._hash(sid):
                        raise ValueError()
                    if self._start(start):
                        mark = self.latest.get(sid)
                        if mark is None or start > mark['start']:
                            self.latest[sid] = {'key': key, 'start': start}
                for row in self.rows:
                    mark = self.latest.get(row['session'])
                    covered = mark and (row['key'] == mark['key'] or (
                        self._start(row.get('start')) and row['start'] <= mark['start']))
                    if not covered:
                        # A digest cannot be reversed into session+round. Keep
                        # it forever rather than guessing from attempt time.
                        self.legacy.add(row['key'])
                if len(self.latest) + len(self.legacy) > self.MAX_RECORDS:
                    raise Blocked('journal_capacity')
            except Blocked:
                self.lock.close()
                raise
            except (OSError, ValueError, TypeError):
                self.lock.close()
                raise Blocked('journal_unreadable')

    def check(self, request):
        now = time.time()
        sid = digest(request['id'])
        start = request.get('started_at')
        valid_start = self._start(start)
        rage = request.get('automation_mode') == 'rage'
        if rage and not valid_start:
            raise Blocked('invalid_request')
        mark = self.latest.get(sid)
        if digest(request['key']) in self.legacy or any(r['key'] == digest(request['key']) for r in self.rows) or (mark and (
                mark['key'] == digest(request['key']) or (valid_start and start <= mark['start']))):
            raise Blocked('already_attempted')
        needs_record = sid not in self.latest if valid_start else digest(request['key']) not in self.legacy
        if needs_record and len(self.latest) + len(self.legacy) >= self.MAX_RECORDS:
            # Never evict a latest-round watermark and silently permit replay.
            raise Blocked('journal_capacity')
        recent = [r for r in self.rows if now - r['time'] < 3600]
        same = [r for r in recent if r['session'] == sid]
        if rage:
            # A verified NEW interrupted round must not wait behind the prior
            # round's optimization. The durable watermark above still forbids
            # replay, and the global lock still serializes desktop input.
            seconds = 5 if request.get('kind') == 'followup' else 0
            waits = [seconds - (now - r['time']) for r in same]
        else:
            waits = [300 - (now - r['time']) for r in same]
            if len(recent) >= 8:
                waits.append(3600 - (now - sorted(r['time'] for r in recent)[-8]))
            if len(same) >= 2:
                waits.append(3600 - (now - sorted(r['time'] for r in same)[-2]))
        wait = max(waits, default=0)
        if wait > 0:
            raise Blocked('cooldown', max(1, math.ceil(wait)))

    def reserve(self, request):
        self.check(request)
        now = time.time()
        sid = digest(request['id'])
        # Recent attempts support cooldowns; durable per-session watermarks
        # protect old rounds even after the bounded detail ring is compacted.
        rows = [r for r in self.rows if now - r['time'] < 7 * 86400][-4095:]
        row = {'key': digest(request['key']), 'session': sid, 'time': now}
        latest, legacy = dict(self.latest), set(self.legacy)
        start = request.get('started_at')
        if self._start(start):
            row['start'] = start
            latest[sid] = {'key': row['key'], 'start': start}
        else:
            legacy.add(row['key'])
        rows.append(row)
        data = json.dumps({'version': 2, 'rows': rows, 'latest': latest, 'legacy': sorted(legacy)}).encode()
        if len(latest) + len(legacy) > self.MAX_RECORDS or len(data) > self.MAX_BYTES:
            raise Blocked('journal_capacity')
        temp = self.path.with_suffix('.tmp')
        with temp.open('wb') as f:
            os.chmod(temp, 0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(self.path)  # Persist BEFORE touching the composer, including crashes.
        self.rows, self.latest, self.legacy = rows, latest, legacy


def resolve_text(request):
    """The one text this round may write: the optional custom `text`, or MESSAGE.

    Missing, empty, or whitespace-only falls back to the fixed MESSAGE. Returns
    None when the request must not send anything (fail closed): a non-string
    `text`, text over MAX_TEXT, or text containing control characters.
    """
    # 狂暴中断恢复同样接受已校验的自定义文本（按软件提示词）；
    # 缺失、空白或非法仍按下方规则回落 MESSAGE / fail closed。
    text = request.get('text')
    if text is None:
        return MESSAGE
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return MESSAGE
    if len(text) > MAX_TEXT or CONTROL_CHARS.search(text):
        return None
    return text


def validate_snapshot(snapshot, expected=MESSAGE):
    if not snapshot.get('identity'):
        raise Blocked('target_unverified')
    if snapshot.get('busy'):
        raise Blocked('already_running')
    if snapshot.get('input_count') != 1 or snapshot.get('value') is None:
        raise Blocked('composer_unreadable')
    if expected is not None:
        # 回读侧统一换行归一化：部分 contenteditable 以 \r\n 报告 AXValue。
        if canonical_text(snapshot['value']) != canonical_text(expected):
            raise Blocked('draft_present' if not expected else 'input_changed')


def canonical_text(value):
    return value.replace('\r\n', '\n').replace('\r', '\n')


def perform(request, backend, journal):
    # Reject invalid custom text before anything else: no write, no journal row,
    # no attempt slot consumed (same non-attempt shape as draft_present).
    text = resolve_text(request)
    if text is None:
        raise Blocked('text_invalid')
    journal.check(request)
    backend.guard(fresh=True)
    backend.route()
    backend.guard(fresh=True)
    snapshot = backend.snapshot()
    validate_snapshot(snapshot, None if request.get('overwrite_draft') is True else '')
    # Codex replaces its empty composer's send control with Resume/voice.
    # Only the known, empty primary editor may defer this check until paste.
    deferred = request.get('app_id') == 'codex' and snapshot.get('send_deferred')
    if not snapshot.get('send_exists') and not deferred:
        raise Blocked('send_unavailable')
    if request.get('mode') == 'check':
        return {'code': 'ready', 'attempted': False,
                'send_deferred': bool(deferred)}
    backend.guard(fresh=True)
    # Guard and snapshot are repeated immediately before the first write.
    validate_snapshot(backend.snapshot(), snapshot['value'])
    journal.reserve(request)
    backend.write(text)
    backend.guard(fresh=True, after_write=True)
    snapshot = backend.snapshot()
    # Readback must be compared against the exact text this round wrote.
    validate_snapshot(snapshot, text)
    if not snapshot.get('send_enabled'):
        raise Blocked('send_unavailable')
    backend.send()  # At most one press. No blind Enter or retry after ambiguity.
    return {'code': 'sent_pending_confirmation', 'attempted': True}


def grok_title_variants(title):
    """Only observed Grok display edits: terminal punctuation and one omitted 的."""
    base = title.strip().rstrip('。！？!?.,，．').strip()
    variants = {base}
    for index, char in enumerate(base):
        if char == '的':
            variants.add(base[:index] + base[index + 1:])
    return variants


def grok_visible_title_matches(visible, title, sidebar=False):
    if sidebar:
        if visible.startswith(GROK_SIDEBAR_PREFIX):
            visible = visible[len(GROK_SIDEBAR_PREFIX):]
        visible = GROK_SIDEBAR_AGE.sub('', visible)
    return visible.strip() in grok_title_variants(title)


def grok_target(request, root=None):
    """Bind stable desktop ID to unique full title and project, never snippets."""
    root = root or Path.home() / 'Library/Application Support/com.grokapp.grok-app'
    def read(name, limit):
        path = root / name
        for attempt in range(2):
            if path.stat().st_size > limit:
                raise Blocked('target_unverified')
            try:
                value = json.loads(path.read_text())
            except ValueError:
                # Grok 重写索引的半截内容：0.15 秒后重读一次再判定。
                if attempt == 0:
                    time.sleep(0.15)
                    continue
                raise Blocked('target_unverified')
            if not isinstance(value, list):
                raise Blocked('target_unverified')
            return [r for r in value if isinstance(r, dict)]
        raise Blocked('target_unverified')
    try:
        sessions = read('sessions_index.json', 4 * 1024 * 1024)
        projects = read('projects.json', 2 * 1024 * 1024)
        matches = [r for r in sessions if 'grok:' + str(r.get('id', '')) == request['id'] and not r.get('archived')]
        if len(matches) != 1:
            raise Blocked('target_unverified')
        row = matches[0]
        title = row.get('title')
        if not isinstance(title, str) or not title or title != request['title']:
            raise Blocked('target_unverified')
        aliases = grok_title_variants(title)
        if len([r for r in sessions if not r.get('archived') and isinstance(r.get('title'), str)
                and aliases.intersection(grok_title_variants(r['title']))]) != 1:
            raise Blocked('target_unverified')
        project = [p for p in projects if p.get('id') == row.get('projectId')]
        if len(project) != 1 or not isinstance(project[0].get('name'), str) or not project[0]['name']:
            raise Blocked('target_unverified')
        project = project[0]
        if len([p for p in projects if p.get('name') == project['name']]) != 1:
            raise Blocked('target_unverified')
        if not isinstance(project.get('path'), str) or not project['path'].startswith('/'):
            raise Blocked('target_unverified')
        return title, project['name'], project['path']
    except (OSError, ValueError, KeyError, TypeError):
        raise Blocked('target_unverified')


def zcode_target(request, root=None):
    """Bind the indexed task ID to a unique title within its desktop project."""
    path = root or Path.home() / '.zcode/v2/tasks-index.sqlite'
    try:
        with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=0.5) as db:
            db.execute('PRAGMA query_only=ON')
            rows = db.execute('''SELECT workspace_key, workspace_path, task_id, title
                                 FROM tasks WHERE deleted = 0 AND archived = 0''').fetchall()
            matches = [(key, project, task, title) for key, project, task, title in rows
                       if 'zcode:' + hashlib.sha256(str(key).encode()).hexdigest()[:12] + ':' + str(task)
                       == request.get('id')]
            global_count = db.execute('SELECT COUNT(*) FROM tasks WHERE title=?',
                                      (matches[0][3],)).fetchone()[0] if len(matches) == 1 else 0
        if len(matches) != 1:
            raise Blocked('target_unverified')
        _, project, _, title = matches[0]
        if (not isinstance(project, str) or not project.startswith('/') or
                not isinstance(title, str) or not title or
                project != request.get('project') or title != request.get('title')):
            raise Blocked('target_unverified')
        # ZCode's search and header expose no task ID. Project + title must
        # select one indexed task; the project display name must also be unique.
        if sum(other_project == project and other_title == title
               for _, other_project, _, other_title in rows) != 1:
            raise Blocked('session_title_ambiguous')
        name = Path(project).name
        if (not name or sum(Path(other_project).name == name and other_project != project
                            for _, other_project, _, _ in rows) != 0):
            raise Blocked('project_name_ambiguous')
        # Search results may expose the project only in a composite label;
        # accept exact title-only matching only when no indexed task, even
        # archived or deleted, shares the title.
        globally_unique_title = global_count == 1
        return title, name, project, globally_unique_title
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise Blocked('target_unverified')


def qoder_target(request, root=None):
    """Bind the selected Qoder edition's UUID, task and workspace metadata."""
    app_id = request.get('app_id') or request.get('id', '').partition(':')[0]
    sid = request.get('navigation_key')
    if (app_id not in QODER_IDS or not isinstance(sid, str) or
            not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', sid) or
            request.get('id') != app_id + ':' + sid):
        raise Blocked('target_unverified')
    folder = 'com.qoder.app.stable' if app_id == 'qoder' else 'com.qodercn.app.stable'
    path = root or Path.home() / 'Library/Application Support' / folder / 'main.sqlite'
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.5) as db:
            db.execute('PRAGMA query_only=ON')
            rows = db.execute('''SELECT title,cwd FROM chat_sessions WHERE session_id=?
                AND archived=0 AND deleted_at IS NULL AND session_kind='standard'
                AND owner_session_id IS NULL''', (sid,)).fetchall()
            # Qoder can rename a workspace independently of its directory.
            # Resolve the displayed name through this exact session's workspace
            # ID; a filesystem basename alone falsely rejects such tasks.
            workspace_name = None
            if 'workspace_id' in {column[1] for column in db.execute('PRAGMA table_info(chat_sessions)')}:
                workspace = db.execute('SELECT workspace_id FROM chat_sessions WHERE session_id=?', (sid,)).fetchone()
                if workspace and workspace[0]:
                    names = db.execute('''SELECT name FROM workspaces WHERE workspace_id=?
                        AND archived=0 AND deleted_at IS NULL''', (workspace[0],)).fetchall()
                    if len(names) != 1 or not isinstance(names[0][0], str) or not names[0][0].strip():
                        raise Blocked('target_unverified')
                    workspace_name = names[0][0]
        if len(rows) != 1:
            raise Blocked('target_unverified')
        title, project = rows[0]
        if (not isinstance(title, str) or title != request.get('title') or
                title != request.get('navigation_title') or not isinstance(project, str) or
                not project.startswith('/') or project != request.get('project') or
                not Path(project).name):
            raise Blocked('target_unverified')
        return sid, title, workspace_name or Path(project).name, project
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise Blocked('target_unverified')


def qoder_chat_url(sid, app_id='qoder-cn'):
    if app_id not in QODER_IDS:
        raise Blocked('target_unverified')
    scheme = 'qoder-app' if app_id == 'qoder' else 'qoder-cn-app'
    return scheme + '://renderer/index.html?workbenchScope=primary#/chat/' + sid + '?surface=conversation'


class MacBackend:
    def __init__(self, request):
        import AppKit
        import Quartz
        from aiwatch.mac import ax, inject, winops
        self.ax, self.inject, self.winops = ax, inject, winops
        self.AppKit, self.Quartz = AppKit, Quartz
        self.request = request
        self.bundle = BUNDLES[request['app_id']]
        self.title = request.get('navigation_title') or request['title']
        self.window = self.root = self.box = self.send_button = None
        self.did_write = False
        self.stage = 'preflight'
        self.grok_identity = grok_target(request) if request['app_id'] == 'grok' else None
        self.zcode_identity = zcode_target(request) if request['app_id'] == 'zcode' else None
        self.qoder_identity = qoder_target(request) if request['app_id'] in QODER_IDS else None

    def guard(self, fresh=False, after_write=False):
        if not self.ax.trusted():
            raise Blocked('permission_required')
        state = self.Quartz.CGSessionCopyCurrentDictionary()
        if not state or not state.get(self.Quartz.kCGSessionOnConsoleKey) or state.get('CGSSessionScreenIsLocked'):
            raise Blocked('locked')
        # Our private events enter at the session tap, below the HID clocks.
        # A real user action still interrupts us, including during paste/readback.
        if self.winops.user_active(2):
            raise Blocked('user_active')
        if self.window and self.winops.frontmost_pid() != self.window.pid:
            raise Blocked('focus_changed')
        if fresh:
            app = self.request['app_id']
            if app == 'autoclaw':
                from autoclaw_adapter import collect
            elif app == 'codex':
                from codex_adapter import collect
            elif app in ('grok', *QODER_IDS):
                from extended_adapters import collect
            else:
                from desktop_adapters import collect
            row = next((r for r in collect() if r['id'] == self.request['id']), None)
            expected = ({'completed'} if self.request.get('automation_mode') == 'rage' else {'completed', 'idle'}) if self.request.get('kind') == 'followup' else {'interrupted'}
            if not row or row.get('status') not in expected or row.get('started_at') != self.request.get('started_at'):
                raise Blocked('state_changed')
            # The controller binds the reviewed project and route, not just the
            # session ID. Recheck them immediately before writing and sending.
            for field in ('project', 'source', 'target'):
                if field in self.request and row.get(field) != self.request[field]:
                    raise Blocked('target_unverified')
            if row.get('user_stopped'):
                raise Blocked('user_stopped')
            if app == 'grok' and (grok_target(self.request) != self.grok_identity or row.get('project') != self.grok_identity[2]):
                raise Blocked('target_unverified')
            if app == 'zcode' and (zcode_target(self.request) != self.zcode_identity or
                                    row.get('project') != self.zcode_identity[2]):
                raise Blocked('target_unverified')
            if app == 'qoder-cn' and (qoder_target(self.request) != self.qoder_identity or
                                       row.get('navigation_key') != self.qoder_identity[0]):
                raise Blocked('target_unverified')
            reason = row.get('status_reason', '')
            if any(s in reason for s in ('用户主动', '用户取消', '用户停止', '主动停止', '手动停止', 'user canceled', 'user cancelled', 'user stopped')):
                raise Blocked('user_stopped')
            restricted = ('认证', '访问权限', '上下文超过')
            if self.request.get('manual_retry') is not True:
                restricted += ('中止事件', '触发者')
            if any(s in reason for s in restricted):
                raise Blocked('manual_resolution')
            limited = any(s in reason for s in ('429', '配额', '频率'))
            if limited and not self.request.get('allow_rate_limit'):
                raise Blocked('manual_resolution')
            if self.request.get('allow_rate_limit') and not limited:
                raise Blocked('state_changed')
            if app == 'autoclaw' and (row.get('navigation_key') != self.request.get('navigation_key') or row.get('navigation_title') != self.title):
                raise Blocked('target_unverified')

    def nodes(self):
        ax = self.ax
        queue, out, deadline = [(self.root, 0, False)], [], time.monotonic() + 6
        zcode = self.request['app_id'] == 'zcode'
        while queue:
            if len(out) > 6000 or time.monotonic() > deadline:
                raise Blocked('tree_incomplete')
            node, depth, conversation = queue.pop()
            if depth > 55:
                raise Blocked('tree_incomplete')
            role = ax.role(node)
            if role in ('AXMenuBar', 'AXMenu', 'AXMenuExtra'):
                continue
            out.append((node, role))
            if zcode and role == 'AXGroup':
                conversation |= ax.get_attr(node, 'AXDOMIdentifier', '') == 'conversation'
                classes = ax.get_attr(node, 'AXDOMClassList', []) or []
                if (conversation and not isinstance(classes, str)
                        and ZCODE_TRANSCRIPT_CLASSES.issubset(classes)):
                    # Full transcript/code token trees can exceed 6000 nodes
                    # before reaching the editor. They are not identity, search,
                    # busy, send or queue controls. Never accept a truncated
                    # control tree; unknown layouts keep the existing limits.
                    continue
            queue.extend((c, depth + 1, conversation) for c in reversed(ax.children(node)))
        return out

    def label(self, node):
        return self.ax.element_name(node).strip()

    def heading_title(self, node):
        name = self.label(node)
        if name in ('', '1'):
            name = ''.join(self.label(child) for child in self.ax.children(node)).strip()
        return name

    def search_boxes(self, nodes):
        return [node for node, role in nodes if role == 'AXComboBox' and
                self.ax.placeholder(node) in ZCODE_SEARCH_PLACEHOLDERS]

    def current_identity(self, nodes):
        ax = self.ax
        if self.request['app_id'] in QODER_IDS:
            sid, title, project_name, _ = self.qoder_identity
            left, top, _, _ = self.window.rect
            urls = [str(ax.get_attr(node, 'AXURL', '')) for node, role in nodes if role == 'AXWebArea']
            headings = []
            for node, role in nodes:
                rect = ax.rect_of(node) if role == 'AXHeading' else None
                if (rect and ax.get_attr(node, 'AXValue', 1) in (1, '1')
                        and top <= rect[1] < top + 150 and rect[0] > left + 50
                        and rect[3] - rect[1] >= 10):
                    headings.append(self.heading_title(node))
            contexts = [node for node, role in nodes if role == 'AXGroup'
                        and self.label(node) == '当前任务上下文']
            projects = [self.label(child) for parent in contexts for child in ax.children(parent)
                        if ax.role(child) == 'AXGroup']
            return (urls.count(qoder_chat_url(sid, self.request['app_id'])) == 1 and headings == [title]
                    and len(contexts) == 1 and projects.count(project_name) == 1)
        if self.request['app_id'] == 'zcode':
            left, top, _, _ = self.window.rect
            headings = []
            for node, role in nodes:
                # AX controls can disappear between reads during navigation.
                # Capture geometry once, including the header used below.
                rect = ax.rect_of(node) if role == 'AXHeading' else None
                if (rect and ax.get_attr(node, 'AXValue', 1) in (1, '1')
                        and top <= rect[1] < top + 100 and rect[0] > left + 50
                        and rect[3] - rect[1] >= 10):
                    headings.append((node, rect))
            if len(headings) != 1 or self.heading_title(headings[0][0]) != self.title:
                return False
            header = headings[0][1]
            projects = []
            for node, role in nodes:
                rect = ax.rect_of(node) if role == 'AXButton' else None
                if (rect and rect[3] - rect[1] >= 10
                        and left + 30 < rect[0] < header[0]
                        and abs(rect[1] - header[1]) < 32):
                    projects.append(self.label(node))
            name = self.zcode_identity[1]
            return sum(value == name or value.startswith(name + ' · ') for value in projects) == 1
        headers = []
        for node, role in nodes:
            rect = ax.rect_of(node) if role in ('AXHeading', 'AXStaticText') else None
            if not rect:
                continue
            # Only the conversation's top heading, never a title inside a
            # transcript or matching sidebar text. AutoClaw uses AXHeading 1.
            left, top, right, bottom = self.window.rect
            margin = 50 if self.request['app_id'] == 'autoclaw' else 170
            if not (top <= rect[1] < top + 150 and rect[0] > left + margin
                    and rect[3] - rect[1] >= 10):
                continue
            if role == 'AXHeading' and ax.get_attr(node, 'AXValue', 1) in (1, '1'):
                headers.append(self.heading_title(node))
            elif self.request['app_id'] != 'autoclaw' and role == 'AXStaticText':
                if self.label(node) == self.title:
                    headers.append(self.title)
        if self.request['app_id'] == 'grok':
            # Grok AX exposes heading plus child text; count only headings and
            # require the active project selector (not a sidebar project label).
            headings = []
            selectors = []
            for node, role in nodes:
                rect = ax.rect_of(node)
                if not rect:
                    continue
                name = self.label(node)
                if (role == 'AXHeading' and ax.get_attr(node, 'AXValue', 1) in (1, '1')
                        and self.window.rect[1] <= rect[1] < self.window.rect[1] + 150
                        and rect[0] > self.window.rect[0] + 50 and rect[3] - rect[1] >= 10):
                    headings.append(self.heading_title(node))
                if (role == 'AXPopUpButton' and rect[0] > self.window.rect[0] + 50
                        and self.window.rect[1] + (self.window.rect[3] - self.window.rect[1]) * .5 <= rect[1]
                        and rect[3] <= self.window.rect[3] and rect[3] - rect[1] >= 10):
                    selectors.append(name)
            return (len(headings) == 1 and grok_visible_title_matches(headings[0], self.title)
                    and selectors.count(self.grok_identity[1]) == 1)
        return headers.count(self.title) == 1

    def sidebar_match(self, name):
        if name == self.title:
            return True
        if self.request['app_id'] == 'workbuddy-ai' and name.startswith(self.title + ' '):
            # WorkBuddy AI 5.5.2 exposes the exact title followed by an age
            # such as '1m', '47m', or '2d'. Never accept arbitrary suffixes or title prefixes.
            return bool(re.fullmatch(r'\d+[md]', name[len(self.title):].strip()))
        if self.request['app_id'] == 'grok':
            return grok_visible_title_matches(name, self.title, sidebar=True)
        if self.request['app_id'] == 'autoclaw' and name.startswith(self.title + ' '):
            rest = name[len(self.title):].strip()
            return bool(AUTOCLAW_SIDEBAR_AGE.fullmatch(rest))
        return False

    def bind_window(self):
        ax = self.ax
        apps = self.AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(self.bundle)
        if len(apps) != 1:
            raise Blocked('app_unavailable')
        pid = int(apps[0].processIdentifier())
        app = ax.app_element(pid)
        windows = ax.get_attr(app, 'AXWindows', []) or []
        usable = [(w, ax.rect_of(w)) for w in windows]
        usable = [(w, r) for w, r in usable if r and r[2] - r[0] > 350 and r[3] - r[1] > 250]
        if len(usable) != 1:
            raise Blocked('window_ambiguous')
        from aiwatch.types import WindowInfo
        self.root, rect = usable[0]
        self.window = WindowInfo(0, ax.title(self.root), pid, self.bundle, rect, class_name=self.bundle)
        return apps[0]

    def inspect(self):
        # Observe the current window without activating/navigating it. No draft,
        # journal, send slot or recovery receipt is changed by this diagnostic.
        if not self.ax.trusted():
            raise Blocked('permission_required')
        self.bind_window()
        current = self.snapshot()
        try:
            validate_snapshot(current, None)
            code = 'ready' if current.get('send_exists') or current.get('send_deferred') else 'send_unavailable'
        except Blocked as error:
            code = str(error)
        return dict(code=code, attempted=False, identity=current['identity'],
                    input_count=current['input_count'], busy=current['busy'],
                    send_exists=current['send_exists'], send_enabled=current['send_enabled'],
                    send_deferred=bool(current.get('send_deferred')),
                    empty_composer=current['value'] == '')

    def route(self):
        ax = self.ax
        app = self.bind_window()
        self.stage = 'route'
        self.focus_window(app)
        target = self.request.get('target', '')
        if self.request['app_id'] in ('codex', 'workbuddy'):
            pattern = r'codex://threads/[A-Za-z0-9_-]+' if self.request['app_id'] == 'codex' else r'workbuddy://chat/[A-Za-z0-9_-]+'
            if not re.fullmatch(pattern, target):
                raise Blocked('target_unverified')
            url = self.AppKit.NSURL.URLWithString_(target)
            handler = self.AppKit.NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(url)
            if not handler or self.AppKit.NSBundle.bundleWithURL_(handler).bundleIdentifier() != self.bundle:
                raise Blocked('target_unverified')
            self.guard()
            self.AppKit.NSWorkspace.sharedWorkspace().openURL_(url)
            nodes = self.navigation_nodes(1.5 if self.request['app_id'] == 'codex' else 0.6)
        elif self.request['app_id'] == 'zcode':
            nodes = self.nodes()
            search_open = bool(self.search_boxes(nodes))
            if search_open or not self.current_identity(nodes):
                nodes = self.route_zcode_search(nodes)
            if not self.current_identity(nodes):
                raise Blocked('header_unverified')
        elif self.request['app_id'] in QODER_IDS:
            nodes = self.nodes()
            if not self.current_identity(nodes):
                sid, title, _, _ = self.qoder_identity
                nodes = self.expand_qoder_workspace(nodes)
                matches = [node for node, role in nodes if role == 'AXLink'
                           and str(ax.get_attr(node, 'AXURL', '')) == qoder_chat_url(sid, self.request['app_id'])
                           and self.label(node) == '任务“' + title + '”，Agent：nav.chat'
                           and 'AXPress' in ax.action_names(node)]
                if len(matches) != 1:
                    raise Blocked('target_unverified')
                self.guard()
                if not ax.press(matches[0]):
                    raise Blocked('target_unverified')
                nodes = self.navigation_nodes(1.5)
            if not self.current_identity(nodes):
                raise Blocked('target_unverified')
        else:
            nodes = self.nodes()
        if not self.current_identity(nodes):
            if self.request['app_id'] == 'autoclaw':
                nodes = self.expand_autoclaw_sidebar(nodes)
            elif self.request['app_id'] == 'grok':
                nodes = self.expand_grok_project(nodes)
            matches = self.sidebar_candidates(nodes)
            if len(matches) != 1:
                raise Blocked('sidebar_target_ambiguous' if matches else 'sidebar_target_missing')
            self.guard()
            if not ax.press(matches[0]):
                raise Blocked('target_unverified')
            nodes = self.navigation_nodes(1.5)
        if not self.current_identity(nodes):
            raise Blocked('header_unverified')

    def sidebar_candidates(self, nodes):
        ax = self.ax
        matches = []
        for node, role in nodes:
            rect = ax.rect_of(node) if role == 'AXButton' else None
            if (rect and self.sidebar_match(self.label(node))
                    and rect[0] < self.window.rect[0] + 300 and rect[3] - rect[1] >= 10
                    and 'AXPress' in ax.action_names(node)):
                matches.append(node)
        return matches

    def expand_autoclaw_sidebar(self, nodes):
        # Only reversible, uniquely named navigation controls. Multiple agents'
        # "show more" buttons are ambiguous and remain for the user to expand.
        for names in ({'展开侧边栏', 'Expand sidebar'}, {'展示更多', 'Show more'}):
            if self.sidebar_candidates(nodes):
                break
            controls = []
            for node, role in nodes:
                rect = self.ax.rect_of(node) if role == 'AXButton' else None
                if (rect and self.label(node) in names and self.ax.enabled(node)
                        and rect[0] < self.window.rect[0] + 300
                        and 'AXPress' in self.ax.action_names(node)):
                    controls.append(node)
            if len(controls) != 1:
                continue
            self.guard()
            if not self.ax.press(controls[0]):
                raise Blocked('sidebar_target_missing')
            nodes = self.navigation_nodes(1.5)
        return nodes

    def expand_grok_project(self, nodes):
        # The exact indexed project is the only collapsed group we may open.
        # Never toggle an already-expanded group or guess between duplicate names.
        if self.sidebar_candidates(nodes):
            return nodes
        controls = []
        for node, role in nodes:
            rect = self.ax.rect_of(node) if role == 'AXButton' else None
            if (rect and self.label(node) == self.grok_identity[1]
                    and self.ax.get_attr(node, 'AXExpanded') is False
                    and rect[0] < self.window.rect[0] + 300
                    and 'AXPress' in self.ax.action_names(node)):
                controls.append(node)
        if len(controls) == 1:
            self.guard()
            if not self.ax.press(controls[0]):
                raise Blocked('sidebar_target_missing')
            nodes = self.navigation_nodes(1.5)
        return nodes

    def expand_qoder_workspace(self, nodes):
        sid, _, name, _ = self.qoder_identity
        target_url = qoder_chat_url(sid, self.request['app_id'])
        for label in ('展开工作目录 ' + name, '展示 ' + name + ' 的更多任务'):
            if any(role == 'AXLink' and str(self.ax.get_attr(node, 'AXURL', '')) == target_url
                   for node, role in nodes):
                break
            controls = [node for node, role in nodes if role == 'AXButton'
                        and self.label(node) == label and self.ax.enabled(node)
                        and 'AXPress' in self.ax.action_names(node)]
            if len(controls) != 1:
                continue
            self.guard()
            if not self.ax.press(controls[0]):
                raise Blocked('sidebar_target_missing')
            nodes = self.navigation_nodes(1.5)
        return nodes

    def route_zcode_search(self, nodes, allow_workspace_return=True):
        ax = self.ax
        left, top, _, _ = self.window.rect
        boxes = self.search_boxes(nodes)
        if not boxes:
            search = []
            for node, role in nodes:
                rect = ax.rect_of(node) if role == 'AXButton' else None
                if (rect and self.label(node).split(' ')[0] in ('搜索', 'Search')
                        and rect[0] < left + 300 and rect[1] < top + 150):
                    search.append(node)
            if not search and allow_workspace_return:
                # Settings/statistics has no task search. Return only through
                # the explicit header control, then reacquire all UI handles.
                back = []
                for node, role in nodes:
                    rect = ax.rect_of(node) if role == 'AXButton' else None
                    if (rect and self.label(node) in ('返回工作区', 'Back to workspace')
                            and ax.enabled(node) and 'AXPress' in ax.action_names(node)
                            and left <= rect[0] < left + 300 and top <= rect[1] < top + 150):
                        back.append(node)
                if len(back) == 1:
                    self.guard()
                    if not ax.press(back[0]):
                        raise Blocked('search_unavailable')
                    nodes = self.navigation_nodes(1.5)
                    if self.current_identity(nodes):
                        return nodes
                    return self.route_zcode_search(nodes, allow_workspace_return=False)
            if len(search) != 1:
                raise Blocked('search_unavailable')
            self.guard()
            if not ax.press(search[0]):
                raise Blocked('search_unavailable')
        deadline = time.monotonic() + 2
        while True:
            self.guard()
            nodes = self.nodes()
            boxes = self.search_boxes(nodes)
            if len(boxes) == 1:
                break
            if time.monotonic() >= deadline:
                raise Blocked('search_unavailable')
            time.sleep(0.05)
        self.write_search_query(boxes[0])
        deadline = time.monotonic() + 4
        while True:
            self.guard()
            nodes = self.nodes()
            box = self.checked_search_box(nodes, {self.title})
            if box is None:
                if time.monotonic() >= deadline:
                    raise Blocked('search_input_changed')
                time.sleep(0.05)
                continue
            result = self.zcode_search_result(nodes)
            if result is not None:
                break
            if time.monotonic() >= deadline:
                raise Blocked('search_result_missing')
            time.sleep(0.05)
        self.guard()
        if not ax.press(result):
            raise Blocked('search_unavailable')
        return self.navigation_nodes(1.5)

    def zcode_search_result(self, nodes):
        ax = self.ax
        left, top, right, bottom = self.window.rect
        matches = []
        for node, role in nodes:
            if role != 'AXMenuItem' or not ax.enabled(node) or 'AXPress' not in ax.action_names(node):
                continue
            rect = ax.rect_of(node)
            if (not rect or rect[2] - rect[0] < 20 or rect[3] - rect[1] < 10 or
                    not (left <= rect[0] < rect[2] <= right and top <= rect[1] < rect[3] <= bottom)):
                continue
            if self.zcode_search_match(node):
                matches.append((rect[1], rect[0], node))
        if not matches:
            return None
        # Content search can list multiple message hits from the same task.
        # Only a globally unique indexed title proves those hits share a task;
        # genuine duplicate titles retain the ambiguity check.
        if len(matches) > 1 and not self.zcode_identity[3]:
            raise Blocked('search_result_ambiguous')
        return min(matches, key=lambda item: item[:2])[2]

    def write_search_query(self, box):
        # AXValue can change Electron's visible field without firing the search
        # handler. Paste into the unique declared search field, never a composer.
        ax = self.ax
        self.guard()
        baseline = ax.get_attr(box, 'AXValue')
        if not isinstance(baseline, str):
            raise Blocked('search_input_changed')
        # Rebind by the unique declared field, not an Electron AX handle that
        # can be replaced when the command panel renders. Reuse an exact query.
        box = self.wait_search_box({baseline})
        if baseline == self.title:
            return
        for attempt in range(2):
            self.guard()
            box = self.wait_search_box({baseline})
            rect = ax.rect_of(box)
            self.inject.click_at((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
            if self.wait_search_box({baseline}, focused=True, timeout=0.6, retry_focus=True) is not None:
                break
            if attempt == 1:
                raise Blocked('search_input_changed')
        self.guard()
        self.inject.hotkey(0)
        self.wait_search_box({baseline}, focused=True)
        pb = self.AppKit.NSPasteboard.generalPasteboard()
        saved = []
        for item in pb.pasteboardItems() or []:
            clone = self.AppKit.NSPasteboardItem.alloc().init()
            for kind in item.types():
                data = item.dataForType_(kind)
                if data is not None:
                    clone.setData_forType_(data, kind)
            saved.append(clone)
        pb.clearContents()
        pb.setString_forType_(self.title, self.AppKit.NSPasteboardTypeString)
        count = pb.changeCount()
        try:
            self.guard()
            self.wait_search_box({baseline}, focused=True)
            self.inject.hotkey(9)
            # After paste no more keys are sent: exact readback is sufficient
            # even if focus moves to a result during a React/AX refresh.
            self.wait_search_box({baseline, '', self.title}, expected=self.title, timeout=2)
        finally:
            if pb.changeCount() == count:
                pb.clearContents()
                if saved:
                    pb.writeObjects_(saved)

    def checked_search_box(self, nodes, values):
        boxes = self.search_boxes(nodes)
        if len(boxes) > 1:
            raise Blocked('search_input_changed')
        if not boxes:
            return None
        box = boxes[0]
        rect = self.ax.rect_of(box)
        left, top, right, bottom = self.window.rect
        if (not self.ax.enabled(box) or not rect or rect[2] - rect[0] < 20 or
                rect[3] - rect[1] < 10 or
                not (left <= rect[0] < rect[2] <= right and top <= rect[1] < rect[3] <= bottom)):
            raise Blocked('search_unavailable')
        value = self.ax.get_attr(box, 'AXValue')
        if not isinstance(value, str) or value not in values:
            raise Blocked('search_input_changed')
        return box

    def wait_search_box(self, values, focused=False, expected=None, timeout=0.6, retry_focus=False):
        deadline = time.monotonic() + timeout
        while True:
            self.guard()
            box = self.checked_search_box(self.nodes(), values)
            if (box is not None and (not focused or self.ax.get_attr(box, 'AXFocused', False))
                    and (expected is None or self.ax.get_attr(box, 'AXValue') == expected)):
                return box
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if focused and box is not None and retry_focus:
                    return None
                raise Blocked('search_input_changed')
            time.sleep(min(0.05, remaining))

    def zcode_search_match(self, node):
        ax = self.ax
        stack = [(child, 1) for child in reversed(ax.children(node))]
        texts, visited = [], 0
        while stack:
            child, depth = stack.pop()
            visited += 1
            if visited > 256 or depth > 8:
                return False
            if ax.role(child) == 'AXStaticText':
                texts.append(child)
            stack.extend((item, depth + 1) for item in reversed(ax.children(child)))
        # Current ZCode exposes the title as child text but folds the project
        # into the menu item's composite label. A globally unique indexed
        # title can select a unique result; route() then checks both header
        # and project button before the composer is touched.
        # The first text is the result's task title. Later text can be a message
        # snippet mentioning our title inside an entirely different task.
        labels = [self.label(child) for child in texts]
        if not labels:
            return False
        title = labels[0]
        first = ax.rect_of(texts[0])
        if first and first[3] - first[1] >= 10:
            # Multi-word queries split the truncated title into highlighted
            # spans, including whitespace leaves. Reassemble only its first
            # line; never include the separate message-snippet or project row.
            parts = []
            for child in texts:
                rect = ax.rect_of(child)
                if not rect:
                    return False
                if rect[3] - rect[1] >= 10 and abs(rect[1] - first[1]) > 3:
                    break
                value = ax.get_attr(child, 'AXValue')
                parts.append(value if isinstance(value, str) else ax.element_name(child))
            title = ''.join(parts).strip()
        return title == self.title and (self.zcode_identity[1] in labels or self.zcode_identity[3])

    def navigation_nodes(self, timeout):
        # Fast apps need no fixed sleep. Slow navigation retains the existing
        # grace period and must still expose the exact conversation identity.
        deadline = time.monotonic() + timeout
        while True:
            self.guard()
            nodes = self.nodes()
            remaining = deadline - time.monotonic()
            ready = self.current_identity(nodes)
            if ready and self.request['app_id'] in ('zcode', 'codex'):
                # Navigation may render a new header before its editor mounts.
                # A matching title alone must not end the navigation wait.
                current = self.snapshot(nodes)
                ready = (current['identity'] and current['input_count'] == 1
                         and current['value'] is not None
                         and (current['send_exists'] or current.get('send_deferred')))
            if ready or remaining <= 0:
                return nodes
            time.sleep(min(0.05, remaining))

    def focus_window(self, app):
        # Do not use watchdog focus(): its last resort minimizes a live window.
        # Restore only an already-minimized target; never hide/close/quit an app.
        if self.ax.get_attr(self.root, 'AXMinimized', False):
            self.ax.set_attr(self.root, 'AXMinimized', False)
        app.activateWithOptions_(int(self.AppKit.NSApplicationActivateIgnoringOtherApps))
        self.ax.raise_window(self.root)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if self.winops.frontmost_pid() == self.window.pid:
                return
            time.sleep(0.1)
        raise Blocked('focus_changed')

    def snapshot(self, nodes=None):
        ax = self.ax
        nodes = self.nodes() if nodes is None else nodes
        if self.request['app_id'] == 'grok' and any(self.label(n) in ('已断开', '发生应用错误', '应用发生错误') or 'Minified React error' in self.label(n) for n, role in nodes if role in ('AXStaticText', 'AXHeading')):
            raise Blocked('app_ui_unavailable')
        boxes, buttons, busy = [], [], False
        left, top, right, bottom = self.window.rect
        for node, role in nodes:
            if role not in ('AXTextArea', 'AXTextField', 'AXButton', 'AXGroup'):
                continue
            rect = ax.rect_of(node)
            margin = 50 if self.request['app_id'] in ('autoclaw', 'zcode', 'grok', *QODER_IDS) else 170
            if not rect or rect[0] < left + margin or rect[1] < top + (bottom - top) * 0.50:
                continue
            name = self.label(node).lower()
            if role == 'AXButton':
                busy |= name in STOP_NAMES and ax.enabled(node)
                if name in SEND_NAMES:
                    buttons.append(node)
            elif role in ('AXTextArea', 'AXTextField') or (role == 'AXGroup' and ax.role_description(node) in ('文本输入区', '文本编辑区', 'text entry area')):
                if self.request['app_id'] == 'codex' and not self.codex_primary_composer(node):
                    continue
                if self.request['app_id'] == 'autoclaw' and not self.autoclaw_composer(node):
                    continue
                if self.request['app_id'] == 'zcode' and not ZCODE_COMPOSER_HINTS.intersection(self.composer_hints(node)):
                    continue
                hints = CHAT_COMPOSER_HINTS.get(self.request['app_id'])
                if hints and not hints.intersection(self.composer_hints(node)):
                    continue
                if ax.enabled(node) and ('search' not in ax.placeholder(node).lower()) and '搜索' not in ax.placeholder(node):
                    boxes.append(node)
        value = None
        self.box = boxes[0] if len(boxes) == 1 else None
        codex_primary = (self.request['app_id'] == 'codex' and self.box is not None
                         and self.codex_primary_composer(self.box))
        if self.request['app_id'] == 'codex' and not codex_primary:
            # A renderer replacement between reads invalidates the editor;
            # never fall back to buttons elsewhere in the window.
            boxes, buttons, self.box = [], [], None
        if codex_primary:
            layout = ax.get_attr(self.box, 'AXParent')
            buttons = [node for node in buttons if self.in_codex_layout(node, layout)]
            busy = any(role == 'AXButton' and self.label(node).lower() in STOP_NAMES
                       and ax.enabled(node) and self.in_codex_layout(node, layout)
                       for node, role in nodes)
        self.send_button = buttons[0] if len(buttons) == 1 else None
        if self.box is not None:
            err, raw = ax.get(self.box, 'AXValue')
            if err == 0 and isinstance(raw, str):
                value = raw.strip("\ufeff\u200b")
                # Some Electron textareas expose their declared placeholder as
                # AXValue. Only normalize an EXACT declared placeholder match.
                placeholder = ax.placeholder(self.box)
                if placeholder and value == placeholder:
                    value = ''
        deferred = bool(codex_primary and value == '' and not buttons)
        return dict(identity=self.current_identity(nodes), input_count=len(boxes), value=value,
                    busy=busy, send_exists=self.send_button is not None,
                    send_deferred=deferred,
                    send_enabled=self.send_button is not None and ax.enabled(self.send_button))

    def in_codex_layout(self, node, layout):
        # Scope submit/stop controls to this primary composer, not a transcript
        # widget, another pane or a queued message's action.
        rect = self.ax.rect_of(node)
        if not rect or rect[2] - rect[0] < 10 or rect[3] - rect[1] < 10:
            return False
        for _ in range(8):
            node = self.ax.get_attr(node, 'AXParent')
            if node is None:
                return False
            if node == layout:
                return True
        return False

    def codex_primary_composer(self, node):
        # Observed native AX signature, bounded to the editor's direct layout
        # parent. Never use a search/comment editor or an arbitrary empty field.
        ax = self.ax
        classes = ax.get_attr(node, 'AXDOMClassList', []) or []
        if (ax.role(node) != 'AXTextArea' or isinstance(classes, str)
                or 'ProseMirror' not in classes
                or not CODEX_COMPOSER_HINTS.intersection(self.composer_hints(node))):
            return False
        parent = ax.get_attr(node, 'AXParent')
        if parent is None or ax.role(parent) != 'AXGroup':
            return False
        parent_classes = ax.get_attr(parent, 'AXDOMClassList', []) or []
        editor_rect, parent_rect = ax.rect_of(node), ax.rect_of(parent)
        left, top, right, bottom = self.window.rect
        return (not isinstance(parent_classes, str)
                and any(str(c).startswith('_ComposerLayoutBody_') for c in parent_classes)
                and editor_rect is not None and parent_rect is not None
                and left <= parent_rect[0] < parent_rect[2] <= right
                and top <= parent_rect[1] < parent_rect[3] <= bottom
                and parent_rect[0] <= editor_rect[0] < editor_rect[2] <= parent_rect[2]
                and parent_rect[1] <= editor_rect[1] < editor_rect[3] <= parent_rect[3])

    def autoclaw_composer(self, node):
        # Goal review forms can expose many editable textareas. Only the actual
        # chat composer's declared hint may select the field to overwrite.
        hints = self.composer_hints(node)
        return any(hint in AUTOCLAW_COMPOSER_HINTS or
                   re.fullmatch(r'发送给 [^\n]{1,80}', hint) for hint in hints)

    def composer_hints(self, node):
        return [self.ax.placeholder(node)] + [str(self.ax.get_attr(node, name, '') or '').strip()
                for name in ('AXTitle', 'AXDescription', 'AXHelp')]

    def write(self, message):
        original = self.snapshot()
        validate_snapshot(original, None if self.request.get('overwrite_draft') is True else '')
        self.guard(fresh=True)
        self.stage = 'write'
        self.did_write = True
        # WorkBuddy's contenteditable accepts AXValue visually but does not
        # notify its editor model. Always use real paste for these editions.
        if self.request.get('app_id') not in ('codex', 'workbuddy', 'workbuddy-ai', 'grok', 'zcode'):
            self.ax.set_attr(self.box, 'AXValue', message)
            time.sleep(0.2)
        snapshot = self.snapshot()
        validate_snapshot(snapshot, None)

        if canonical_text(snapshot['value']) == canonical_text(message) and snapshot.get('send_enabled'):
            return
        # Electron editors may reject AXValue or accept it without updating
        # React state. Replace the verified editor selection using native paste.
        if canonical_text(snapshot['value']) not in (canonical_text(original['value']), canonical_text(message)):
            raise Blocked('input_changed')
        self.guard(fresh=True, after_write=True)
        self.focus_composer()
        self.guard(fresh=True, after_write=True)
        validate_snapshot(self.snapshot(), snapshot['value'])
        if not self.ax.get_attr(self.box, 'AXFocused', False):
            raise Blocked('focus_changed')
        self.inject.hotkey(0)  # Real keydown updates Electron's editor selection.
        time.sleep(0.1)
        self.guard(fresh=True, after_write=True)
        if not self.ax.get_attr(self.box, 'AXFocused', False):
            raise Blocked('focus_changed')
        self.paste_verified(message)

    def focus_composer(self):
        rect = self.ax.rect_of(self.box)
        if not rect or rect[2] - rect[0] < 20 or rect[3] - rect[1] < 10:
            raise Blocked('composer_unreadable')
        # AXFocused alone does not update WorkBuddy's editor selection. Click
        # INSIDE the unique, verified AX editor; never infer screen coordinates.
        self.inject.click_at((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
        if not self.ax.get_attr(self.box, 'AXFocused', False):
            raise Blocked('focus_changed')

    def paste_verified(self, message):
        # Electron can read the pasteboard asynchronously. Keep our text until
        # the editor AND send button acknowledge it, not a fixed 120 ms delay.
        pb = self.AppKit.NSPasteboard.generalPasteboard()
        saved = []
        for item in pb.pasteboardItems() or []:
            clone = self.AppKit.NSPasteboardItem.alloc().init()
            for kind in item.types():
                data = item.dataForType_(kind)
                if data is not None:
                    clone.setData_forType_(data, kind)
            saved.append(clone)
        pb.clearContents()
        pb.setString_forType_(message, self.AppKit.NSPasteboardTypeString)
        count = pb.changeCount()
        try:
            self.inject.hotkey(9)  # Cmd+V in the verified, clicked editor.
            deadline = time.monotonic() + 2
            while True:
                self.guard(after_write=True)
                current = self.snapshot()
                validate_snapshot(current, None)
                matched = canonical_text(current['value']) == canonical_text(message)
                if matched and current.get('send_enabled'):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Blocked('send_unavailable' if matched else 'input_failed')
                time.sleep(min(0.05, remaining))
        finally:
            if pb.changeCount() == count:
                pb.clearContents()
                if saved:
                    pb.writeObjects_(saved)

    def send(self):
        self.stage = 'send'
        if self.winops.frontmost_pid() != self.window.pid or not self.ax.press(self.send_button):
            raise Blocked('send_unconfirmed')
        # One send press only. Leave the application's conversation/message
        # queue in arrival order, including any newly exposed insert control.


def main():
    began = time.monotonic()
    backend = None
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise Blocked('invalid_request')
        request = json.loads(raw)
        if request.get('mode') == 'judge':
            try:
                try:
                    from completion_judge import judge
                except ImportError:
                    from supervisor.completion_judge import judge
                result = judge(request)
            except Exception:
                result = {'code': 'judgement_unknown', 'attempted': False}
            print(json.dumps(result))
            return  # No AX, Journal, recovery receipt or send on this path.
        if request.get('mode') == 'health':
            from aiwatch.mac import ax
            print(json.dumps({'code': 'ready' if ax.trusted() else 'permission_required', 'attempted': False}))
            return
        if request.get('app_id') not in BUNDLES or request.get('mode') not in ('send', 'check', 'inspect'):
            raise Blocked('unsupported')
        if not all(isinstance(request.get(k), str) and request[k] for k in ('key', 'id', 'title')):
            raise Blocked('invalid_request')
        backend = MacBackend(request)
        result = backend.inspect() if request.get('mode') == 'inspect' else perform(request, backend, Journal())
    except Blocked as e:
        result = {'code': str(e), 'attempted': bool(backend and backend.did_write)}
        if e.retry_after is not None:
            result['retry_after'] = e.retry_after
    except Exception as e:
        trace = e.__traceback__
        while trace and trace.tb_next:
            trace = trace.tb_next
        # Code location only: never include the exception's text, local values,
        # external filenames, clipboard contents or conversation details.
        result = {'code': 'bridge_error', 'attempted': bool(backend and backend.did_write),
                  'error_type': type(e).__name__,
                  'error_function': trace.tb_frame.f_code.co_name if trace else '',
                  'error_line': trace.tb_lineno if trace else 0}
    if isinstance(locals().get('request'), dict) and request.get('mode') == 'inspect':
        print(json.dumps(result))
        return
    # Bounded local receipt contains no titles, drafts, paths or conversation text.
    try:
        directory = Path.home() / 'Library/Application Support/AgentRadar'
        directory.mkdir(parents=True, exist_ok=True)
        receipt = dict(result, time=time.time(), stage=getattr(backend, 'stage', 'preflight'),
                       elapsed_ms=round((time.monotonic() - began) * 1000))
        receipt['app_id'] = request.get('app_id', '')
        if isinstance(request.get('key'), str):
            receipt['key_hash'] = digest(request['key'])
        path = directory / 'last-recovery.json'
        path.write_text(json.dumps(receipt))
        path.chmod(0o600)
    except Exception:
        pass
    print(json.dumps(result))


if __name__ == '__main__':
    main()
