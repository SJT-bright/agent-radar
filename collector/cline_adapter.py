"""Read Cline Desktop session metadata and lifecycle events, never chat bodies.

All databases are opened read-only; SQLite honors live WAL/locking. A session
creation timestamp and a running desktop process cannot prove an active turn.
"""
import sqlite3
import time
from pathlib import Path

try:
    from .codex_adapter import _timestamp, _text
    from .desktop_adapters import _processes
    from .failure_reasons import failure_reason
except ImportError:
    from codex_adapter import _timestamp, _text
    from desktop_adapters import _processes
    from failure_reasons import failure_reason

LIMIT = 40
FRESH_SECONDS = 180
APP_PATH = '/Cline.app/Contents/MacOS/'


def _connect(path):
    # 调用方已确认文件存在：普通连接 + query_only 严格只读，且允许热 WAL 恢复。
    db = sqlite3.connect(str(path), timeout=.2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    deadline = time.monotonic() + 1.2
    db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return db


class ClineCollector:
    def __init__(self, home=None, clock=time.time, process_supplier=_processes):
        self.root = (Path(home) if home is not None else Path.home()) / '.cline/data/db'
        self.clock = clock
        self.process_supplier = process_supplier
        self.last_errors = []
        self._last = []

    def collect(self):
        self.last_errors = []
        processes = self.process_supplier()
        gui_pid = next((pid for pid, path in processes.items()
                        if path.endswith(APP_PATH + 'cline-app')), None)
        if gui_pid is None:
            return []
        try:
            found = self._read(gui_pid, processes)
            self._last = found
            return found
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
            # 状态库被应用写入短暂加锁时，一次有界重试避免整轮降级。
            transient = (isinstance(error, sqlite3.OperationalError)
                         and ('locked' in str(error).lower() or 'busy' in str(error).lower()))
            if transient:
                time.sleep(0.25)
                try:
                    found = self._read(gui_pid, processes)
                    self._last = found
                    return found
                except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
                    pass
            self.last_errors = ['Cline：本地会话读取失败（' + type(error).__name__ + '）']
            return [{**row, 'status': 'unknown',
                     'status_reason': 'Cline 本地会话数据暂不可读，不能沿用旧状态'} for row in self._last]

    def _read(self, gui_pid, processes):
        if not (self.root / 'sessions.db').exists():
            return []
        db = _connect(self.root / 'sessions.db')
        try:
            sessions = list(db.execute('''SELECT session_id,pid,status,ended_at,updated_at,
                workspace_root,cwd,json_extract(metadata_json,'$.title') title
                FROM sessions WHERE source='desktop' AND is_subagent=0
                AND parent_session_id IS NULL
                ORDER BY updated_at DESC LIMIT ?''', (LIMIT,)))
        finally:
            db.close()
        event_path = self.root / 'hub-events-hub-production.db'
        events = _connect(event_path) if event_path.exists() else None
        now = self.clock()
        rows = []
        try:
            for session in sessions:
                sid = session['session_id']
                if not isinstance(sid, str) or not sid or len(sid) > 256:
                    continue
                start = terminal = activity = None
                if events is not None:
                    start = events.execute('''SELECT sequence,created_at FROM hub_events
                        WHERE session_id=? AND event='run.started'
                        ORDER BY sequence DESC LIMIT 1''', (sid,)).fetchone()
                    # Lifecycle metadata only: no envelope, prompt, deltas or reply text.
                    terminal = events.execute('''SELECT event,created_at,
                        json_extract(envelope_json,'$.payload.reason') reason,
                        substr(json_extract(envelope_json,'$.payload.error.message'),1,2048) error
                        FROM hub_events WHERE session_id=? AND sequence>=?
                        AND event IN ('run.completed','run.failed','run.cancelled','run.canceled','run.interrupted')
                        ORDER BY sequence DESC LIMIT 1''', (sid, start['sequence'] if start else 0)).fetchone()
                    activity = events.execute('''SELECT max(created_at) ts FROM hub_events
                        WHERE session_id=? AND sequence>=? AND event IN
                        ('run.started','assistant.delta','reasoning.delta','iteration.started',
                         'tool.started','tool.finished','capability.requested','capability.resolved')''',
                        (sid, start['sequence'] if start else 0)).fetchone()
                started = _timestamp(start['created_at']) if start else 0
                ended = _timestamp(terminal['created_at']) if terminal else 0
                last_activity = _timestamp(activity['ts']) if activity else 0
                updated = max(_timestamp(session['updated_at']), started, ended, last_activity)
                status = 'unknown'
                reason = '已读取 Cline 会话，但未找到明确的本轮运行证据'
                user_stopped = False
                if terminal:
                    stop = terminal['reason'] or terminal['event'].split('.')[-1]
                    if terminal['event'] == 'run.completed' and stop in ('completed', 'done', 'success'):
                        status, reason = 'completed', 'Cline run.completed 明确记录本轮完成'
                    elif stop in ('cancelled', 'canceled', 'stopped', 'aborted', 'interrupted') or terminal['event'] != 'run.completed':
                        status, reason = 'interrupted', failure_reason(terminal['error'], stop=stop)
                        user_stopped = stop in ('cancelled', 'canceled', 'stopped')
                    else:
                        reason = 'Cline 记录轮次结束，但结束原因尚不能识别'
                # A newer explicit session failure must not inherit a previous
                # successful turn's duration or imply the task ran continuously.
                registry_end = _timestamp(session['ended_at'])
                if session['status'] in ('failed', 'error', 'cancelled', 'canceled') and registry_end > ended:
                    status = 'interrupted'
                    reason = 'Cline 会话索引标记失败或取消；没有提供更具体的本轮终止原因'
                    started, ended = (started if not terminal else 0), registry_end
                    user_stopped = session['status'] in ('cancelled', 'canceled')
                elif not terminal:
                    worker = processes.get(session['pid'], '')
                    alive = APP_PATH in worker
                    fresh = -30 <= now - last_activity <= FRESH_SECONDS
                    if started and alive and fresh and session['status'] in ('running', 'active', 'busy', 'idle'):
                        status, reason = 'running', 'Cline 本轮已开始，执行进程存活且有近期运行事件'
                    elif session['status'] in ('waiting', 'waiting_for_approval', 'input_required') and alive and fresh:
                        status, reason = 'waiting', 'Cline 会话索引明确等待输入或确认，执行进程存活'
                    elif started:
                        reason = 'Cline 本轮缺少存活执行进程或近期事件，暂不能确认仍在运行'
                row = dict(id='cline:' + sid, app_id='cline', app_name='Cline',
                           title=_text(session['title']) or '未命名会话',
                           project=session['workspace_root'] or session['cwd'] or '',
                           status=status, status_reason=reason, evidence=reason,
                           updated_at=updated, source='local-db', target='', pid=gui_pid,
                           navigation_key=sid, navigation_title=_text(session['title']),
                           last_activity_at=ended or last_activity or updated, user_stopped=user_stopped)
                if started:
                    row.update(started_at=started, timing_basis='turn',
                               timing_reason='按 Cline run.started / 轮次结束事件计时；不是会话创建时间')
                    if ended >= started:
                        row['ended_at'] = ended
                else:
                    row['timing_reason'] = 'Cline 数据中没有这一轮的开始事件；会话创建时间不能代替本轮开始时间'
                rows.append(row)
        finally:
            if events is not None:
                events.close()
        return rows


_collector = ClineCollector()


def collect():
    return _collector.collect()


def diagnostics():
    return _collector.last_errors
