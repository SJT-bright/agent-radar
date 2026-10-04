"""Bounded, read-only Qoder desktop and Grok lifecycle adapters.

Only session metadata and lifecycle fields are retained. No credentials, network
requests, chat exports, or app UI interaction are needed by these collectors.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote
from uuid import UUID

try:
    from .codex_adapter import _timestamp, _text
    from .desktop_adapters import _processes
except ImportError:
    from codex_adapter import _timestamp, _text
    from desktop_adapters import _processes

LIMIT = 40
TAIL_BYTES = 512 * 1024
FRESH_SECONDS = 180


def _uuid(value):
    try:
        return str(UUID(str(value))) == str(value).lower()
    except (ValueError, TypeError, AttributeError):
        return False


def _json(path, limit):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('metadata limit')
    return json.loads(data)


def _connect(path):
    # 调用方已确认文件存在：普通连接 + query_only 严格只读，且允许热 WAL
    # 恢复——只读 URI 会在查询期报 unable to open（应用正在写库时）。
    connection = sqlite3.connect(str(path), timeout=.15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    deadline = time.monotonic() + .8
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return connection


class ExtendedCollector:
    def __init__(self, home=None, clock=time.time, process_supplier=_processes):
        self.home = Path(home) if home is not None else Path.home()
        self.clock = clock
        self.process_supplier = process_supplier
        self.last_errors = []
        self._last = {}
        self._tails = {}

    def collect(self):
        self.last_errors = []
        now = self.clock()
        try:
            processes = self.process_supplier()
        except Exception:
            processes = {}
        rows = []
        for app_id, name, folder in [('qoder-cn', 'Qoder CN', 'com.qodercn.app.stable'),
                                     ('qoder', 'Qoder', 'com.qoder.app.stable'),
                                     ('grok', 'Grok', 'com.grokapp.grok-app')]:
            pid = next((pid for pid, executable in processes.items()
                        if f'/{name}.app/Contents/MacOS/' in executable), None)
            if pid is None:
                for row in self._last.get(app_id, []):
                    rows.append({**row, 'pid': None, 'status': 'unknown',
                                 'status_reason': '对应应用进程已退出或暂不可确认，状态留在后台'})
                continue
            root = self.home / 'Library/Application Support' / folder
            if not root.exists():
                self._last.pop(app_id, None)
                continue
            try:
                found = (self._grok(root, pid, now) if app_id == 'grok'
                         else self._qoder(root, app_id, name, pid, now))
                self._last[app_id] = found
                rows.extend(found)
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
                # 应用写入自己的状态库时会短暂加锁；一次有界重试避免
                # 整轮降级成 unknown 造成面板闪烁。
                transient = (isinstance(error, sqlite3.OperationalError)
                             and ('locked' in str(error).lower() or 'busy' in str(error).lower()))
                if transient:
                    time.sleep(0.25)
                    try:
                        found = (self._grok(root, pid, now) if app_id == 'grok'
                                 else self._qoder(root, app_id, name, pid, now))
                        self._last[app_id] = found
                        rows.extend(found)
                        continue
                    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
                        pass
                self.last_errors.append(f'{name}：会话状态读取失败（{type(error).__name__}）')
                for row in self._last.get(app_id, []):
                    rows.append({**row, 'status': 'unknown',
                                 'status_reason': '本地状态源暂不可读，不能沿用旧运行状态'})
        return rows

    def _qoder(self, root, app_id, name, pid, now):
        db = root / 'main.sqlite'
        if not db.exists():
            return []
        connection = _connect(db)
        try:
            sessions = list(connection.execute('''SELECT session_id,title,cwd,updated_at
                FROM chat_sessions WHERE archived=0 AND deleted_at IS NULL
                AND session_kind='standard' AND owner_session_id IS NULL
                ORDER BY updated_at DESC LIMIT ?''', (LIMIT,)))
            metadata = {}
            for session in sessions:
                sid = session['session_id']
                row = connection.execute('''SELECT turn_id,status,created_at,updated_at,
                    json_extract(payload_json,'$.role') role,
                    json_extract(payload_json,'$.turnStartedAt') started,
                    json_extract(payload_json,'$.completedAt') ended
                    FROM chat_session_messages WHERE session_id=?
                    AND json_extract(payload_json,'$.role') IN ('assistant','user')
                    ORDER BY sequence DESC LIMIT 1''', (sid,)).fetchone()
                metadata[sid] = dict(row) if row else {}
        finally:
            connection.close()
        active = {}
        buffer = root / 'chat-session-turn-payload-buffer.sqlite'
        if buffer.exists():
            connection = _connect(buffer)
            try:
                for session in sessions:
                    sid = session['session_id']
                    row = connection.execute('''SELECT turn_id,turn_started_at,
                        max(created_at) last_activity FROM active_turn_projection_items
                        WHERE session_id=? GROUP BY turn_id,turn_started_at
                        ORDER BY last_activity DESC LIMIT 1''', (sid,)).fetchone()
                    if row:
                        active[sid] = dict(row)
                    pending = connection.execute('''SELECT turn_id,message_status status,
                        created_at,updated_at,json_extract(message_json,'$.role') role,
                        json_extract(message_json,'$.turnStartedAt') started,
                        json_extract(message_json,'$.completedAt') ended
                        FROM pending_turn_projections WHERE session_id=?
                        AND json_extract(message_json,'$.role') IN ('assistant','user')
                        ORDER BY updated_at DESC LIMIT 1''', (sid,)).fetchone()
                    if pending and _timestamp(pending['updated_at']) > _timestamp(metadata[sid].get('updated_at')):
                        metadata[sid] = dict(pending)
            finally:
                connection.close()
        result = []
        for session in sessions:
            sid = session['session_id']
            if not _uuid(sid):
                continue
            msg, live = metadata[sid], active.get(sid, {})
            started = _timestamp(msg.get('started'))
            ended = _timestamp(msg.get('ended'))
            updated = max(_timestamp(session['updated_at']), _timestamp(msg.get('updated_at')))
            status, reason = 'unknown', '会话可读取，但缺少当前轮次的明确运行证据'
            row_user_stopped = False
            if msg.get('role') == 'assistant':
                status = {'completed': 'completed', 'interrupted': 'interrupted',
                          'failed': 'interrupted', 'canceled': 'interrupted',
                          'waiting-user': 'waiting'}.get(msg.get('status'), 'unknown')
                reason = 'Qoder 本地回复投影状态：' + str(msg.get('status', 'unknown'))
                # 按结局分流：用户取消标 user_stopped；failed 是崩溃类失败，
                # 不得套用「未记录触发者」阻断词，否则真实中断永远无法恢复。
                if msg.get('status') == 'canceled':
                    reason = 'Qoder 记录本轮由用户主动取消'
                    row_user_stopped = True
                elif msg.get('status') == 'failed':
                    reason = 'Qoder 明确记录本轮失败：应用记录错误终止，未提供可识别的具体原因'
                elif status == 'interrupted':
                    reason += '；未记录触发者，暂不能判定为意外停止'
            live_start = _timestamp(live.get('turn_started_at'))
            activity = _timestamp(live.get('last_activity'))
            terminal_same_turn = (msg.get('role') == 'assistant'
                                 and msg.get('status') in {'completed', 'interrupted', 'failed', 'canceled'}
                                 and msg.get('turn_id') == live.get('turn_id'))
            if live_start and live_start >= max(started, ended) and not terminal_same_turn:
                started, ended, updated = live_start, 0, max(updated, activity)
                # A canceled previous projection must not mark a newer live
                # turn as user-stopped. Its later terminal record decides anew.
                row_user_stopped = False
                if pid and -30 <= now - activity <= FRESH_SECONDS:
                    status, reason = 'running', 'Qoder 活动轮次缓冲区有近期执行记录，且对应应用进程存活'
                else:
                    status, reason = 'unknown', '活动轮次缺少近期执行记录或应用进程已退出，不能确认仍在运行'
            row = dict(id=f'{app_id}:{sid}', app_id=app_id, app_name=name,
                       title=_text(session['title']), project=session['cwd'], status=status,
                       evidence=reason, status_reason=reason, updated_at=updated,
                       source='local-db', target='', pid=pid,
                       navigation_title=_text(session['title']), navigation_key=sid)
            if row_user_stopped:
                row['user_stopped'] = True
            if started:
                row.update(started_at=started, timing_basis='turn',
                           timing_reason='按 Qoder 活动轮次或回复投影的开始时间计算')
            if ended >= started > 0 and status not in {'running', 'waiting'}:
                row['ended_at'] = ended
            row['last_activity_at'] = updated
            result.append(row)
        return result

    def _events(self, path):
        stat = path.stat()
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        key = str(path)
        if key in self._tails and self._tails[key][0] == signature:
            return self._tails[key][1].copy()
        with path.open('rb') as stream:
            offset = max(0, stat.st_size - TAIL_BYTES)
            stream.seek(offset)
            lines = stream.read(TAIL_BYTES).splitlines()
        if offset:
            lines = lines[1:]
        state = {'status': 'unknown', 'activity': 0}
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            stamp, kind = _timestamp(event.get('ts')), event.get('type')
            if not stamp:
                continue
            state['activity'] = max(state['activity'], stamp)
            if kind == 'turn_started':
                state.update(status='running', started=stamp, ended=0, reason='Grok turn_started')
                state.pop('user_stopped', None)
            elif kind == 'turn_ended':
                outcome = event.get('outcome')
                status = ('completed' if outcome == 'completed' else 'interrupted'
                          if outcome in {'failed', 'error', 'canceled', 'cancelled', 'interrupted', 'aborted'} else 'unknown')
                # 按结局分流：显式取消标 user_stopped；failed/error 是崩溃类
                # 失败，不套用「触发者待确认」阻断词，否则真实中断无法恢复。
                if outcome in {'canceled', 'cancelled'}:
                    state.update(status=status, ended=stamp, user_stopped=True,
                                 reason='Grok 明确记录本轮由用户主动取消')
                elif outcome in {'failed', 'error'}:
                    state.update(status=status, ended=stamp,
                                 reason='Grok 明确记录本轮失败：应用记录错误终止，未提供可识别的具体原因')
                else:
                    state.update(status=status, ended=stamp,
                                 reason='Grok 明确记录本轮完成' if status == 'completed'
                                 else 'Grok 明确记录本轮结束但未完成；中断触发者待确认')
            elif kind == 'permission_requested' and not state.get('ended'):
                state.update(status='waiting', reason='Grok 等待工具权限确认')
            elif kind in {'permission_resolved', 'tool_started', 'tool_completed', 'phase_changed'} and not state.get('ended'):
                if kind == 'phase_changed' and event.get('phase') in {'idle', 'completed', 'stopped'}:
                    continue
                state.update(status='running', ended=0, reason='Grok 执行阶段事件持续更新')
        if len(self._tails) >= LIMIT * 2:
            self._tails.pop(next(iter(self._tails)))
        self._tails[key] = (signature, state.copy())
        return state

    def _grok(self, root, pid, now):
        index = root / 'sessions_index.json'
        if not index.exists():
            return []
        sessions = _json(index, 4 * 1024 * 1024)
        projects = _json(root / 'projects.json', 2 * 1024 * 1024)
        if not isinstance(sessions, list) or not isinstance(projects, list):
            raise ValueError('metadata shape')
        project_map = {p['id']: p.get('path', '') for p in projects if isinstance(p, dict) and 'id' in p}
        candidates = [s for s in sessions if isinstance(s, dict) and not s.get('archived')
                      and _uuid(s.get('id')) and _uuid(s.get('agentSessionId'))
                      and isinstance(project_map.get(s.get('projectId')), str)
                      and project_map[s['projectId']].startswith('/')]
        candidates.sort(key=lambda s: _timestamp(s.get('updatedAt')), reverse=True)
        result = []
        session_root = self.home / '.grok/sessions'
        for session in candidates[:LIMIT]:
            sid, agent = session.get('id'), session.get('agentSessionId')
            project = project_map.get(session.get('projectId'), '')
            if not _uuid(sid) or not _uuid(agent) or not isinstance(project, str) or not project.startswith('/'):
                continue
            path = session_root / quote(project, safe='') / agent / 'events.jsonl'
            if not path.resolve().is_relative_to(session_root.resolve()):
                continue
            try:
                state = self._events(path)
            except (OSError, ValueError):
                state = {'status': 'unknown', 'activity': 0, 'reason': 'Grok 会话索引可读，轮次事件暂不可读'}
            status, reason = state['status'], state.get('reason', '缺少明确轮次事件')
            if status in {'running', 'waiting'} and (not pid or not -30 <= now - state['activity'] <= FRESH_SECONDS):
                status, reason = 'unknown', 'Grok 轮次缺少新鲜执行证据或应用已退出，不能沿用历史运行状态'
            row = dict(id='grok:' + sid, app_id='grok', app_name='Grok', title=_text(session.get('title')),
                       project=project, status=status, evidence=reason, status_reason=reason,
                       updated_at=max(_timestamp(session.get('updatedAt')), state['activity']),
                       source='local-log', target='', pid=pid, last_activity_at=state['activity'])
            if state.get('started'):
                row.update(started_at=state['started'], timing_basis='turn',
                           timing_reason='按 Grok turn_started / turn_ended 事件计算')
            if state.get('ended'):
                row['ended_at'] = state['ended']
            if state.get('user_stopped'):
                row['user_stopped'] = True
            result.append(row)
        return result


_collector = ExtendedCollector()


def collect():
    return _collector.collect()


def diagnostics():
    return list(_collector.last_errors)
