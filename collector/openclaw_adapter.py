"""Bounded, read-only discovery of the independent local OpenClaw instance.

Only session_nodes and lifecycle metadata are queried. No auth/config tables,
message bodies, compressed transcripts, credentials, or gateway writes.
"""
import math
import re
import sqlite3
import time
import json
try:
    from . import openclaw_care
except ImportError:
    import openclaw_care
from pathlib import Path
try:
    from .failure_reasons import failure_reason
except ImportError:
    from failure_reasons import failure_reason

SESSION_LIMIT = 40
AGENT_LIMIT = 8
EVENT_LIMIT = 512
AGENT = re.compile(r'[A-Za-z0-9_-]{1,80}')
SID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
RUN = re.compile(r'[A-Za-z0-9_-]{1,128}')
FIELDS = ('sessionId', 'startedAt', 'endedAt', 'lastRunId', 'lifecycleRevision',
          'abortedLastRun', 'spawnedBy', 'parentSessionKey', 'subagentRole',
          'spawnDepth', 'acp', 'agentHarnessId', 'pluginOwnerId',
          'initializationPending', 'pendingFinalDelivery')


def stamp(value):
    if isinstance(value, bool):
        return 0.0
    try:
        value = float(value or 0)
        if value > 100_000_000_000:
            value /= 1000
        return value if math.isfinite(value) and value > 0 else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def valid_key(key, agent):
    return (isinstance(key, str) and 0 < len(key) <= 512 and
            key.startswith('agent:' + agent + ':') and
            not re.search(r'[\s\x00-\x1f\x7f]', key) and
            not re.match(r'^agent:[^:]+:(?:subagent|cron|acp|evolution-check):', key))


def parse_id(value):
    if not isinstance(value, str):
        return None
    parts = value.split(':')
    return tuple(parts[1:]) if (len(parts) == 3 and parts[0] == 'openclaw'
                                and AGENT.fullmatch(parts[1]) and SID.fullmatch(parts[2])) else None


class OpenClawCollector:
    def __init__(self, home=None, clock=time.time):
        try:
            from supervisor.openclaw_runtime import source_home
        except ImportError:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from supervisor.openclaw_runtime import source_home
        self.home = source_home(home)
        self.care_home = Path(home) if home is not None else None
        self.root = self.home / '.openclaw' / 'agents'
        self.clock = clock
        self.last_errors = []
        self._error_metadata = None

    def _rows(self, database, agent, session_id=None, include_children=False):
        # Do not follow redirects or other OpenClaw profiles (including AutoClaw).
        if not database.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('database redirect')
        from urllib.parse import quote
        # Encode the native filename without a URI authority, including WSL UNC.
        uri = 'file:' + quote(str(database), safe='/') + '?mode=ro'
        con = sqlite3.connect(uri, uri=True, timeout=.3)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA query_only=ON')
            deadline = time.monotonic() + 2.0
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            con.execute('BEGIN')
            columns = ', '.join("json_extract(entry_json, '$." + f + "') AS " + f for f in FIELDS)
            sql = ('SELECT session_key,current_session_id,status,updated_at,label,display_name,' + columns +
                   ' FROM session_nodes WHERE entry_valid=1 AND json_valid(entry_json) AND archived_at IS NULL')
            params = []
            if session_id:
                sql += ' AND current_session_id=?'
                params.append(session_id)
            # Exclude child/automation/external-runtime work BEFORE the limit.
            if not include_children:
                sql += (" AND json_extract(entry_json,'$.spawnedBy') IS NULL"
                        " AND json_extract(entry_json,'$.parentSessionKey') IS NULL"
                        " AND json_extract(entry_json,'$.subagentRole') IS NULL"
                        " AND coalesce(json_extract(entry_json,'$.spawnDepth'),0)=0")
            sql += (" AND json_extract(entry_json,'$.acp') IS NULL"
                    " AND json_extract(entry_json,'$.pluginOwnerId') IS NULL"
                    " AND coalesce(json_extract(entry_json,'$.agentHarnessId'),'openclaw')='openclaw'"
                    " AND session_key NOT GLOB 'agent:*:cron:*'"
                    " AND session_key NOT GLOB 'agent:*:subagent:*'"
                    " AND session_key NOT GLOB 'agent:*:acp:*'"
                    " AND session_key NOT GLOB 'agent:*:evolution-check:*'"
                    ' ORDER BY updated_at DESC LIMIT ?')
            params.append(SESSION_LIMIT + 1 if not session_id else 3)
            records = con.execute(sql, params).fetchall()
            if session_id and len(records) != 1:
                return []  # One stable ID mapped to several keys is ambiguous.
            rows = []
            for entry in records[:SESSION_LIMIT]:
                try:
                    row = self._row(con, entry, agent)
                    if row:
                        rows.append(row)
                except (ValueError, TypeError, OverflowError):
                    self.last_errors.append('OpenClaw 一条会话元数据异常，已跳过')
            return rows
        finally:
            con.close()

    def _row(self, con, entry, agent):
        sid, key = entry['current_session_id'], entry['session_key']
        if (not isinstance(sid, str) or not SID.fullmatch(sid) or
                entry['sessionId'] != sid or not valid_key(key, agent)):
            return None
        now = self.clock()
        # Read fixed lifecycle fields inside SQLite, never the whole event JSON.
        events = con.execute("""SELECT run_id,created_at,
            json_extract(event_json,'$.type') AS kind,
            json_extract(event_json,'$.workspaceDir') AS cwd,
            json_extract(event_json,'$.data.status') AS outcome,
            json_extract(event_json,'$.data.stopReason') AS stop,
            json_extract(event_json,'$.data.externalAbort') AS external_abort,
            json_extract(event_json,'$.data.timedOut') AS timed_out,
            json_extract(event_json,'$.data.aborted') AS aborted
            FROM trajectory_runtime_events WHERE session_id=? AND json_valid(event_json)
            AND json_extract(event_json,'$.type') IN ('session.started','session.ended')
            ORDER BY created_at DESC,seq DESC LIMIT ?""", (sid, EVENT_LIMIT)).fetchall()
        # Announce/delegate settlement traces are not a fresh user turn.
        events = [e for e in events if isinstance(e['run_id'], str) and RUN.fullmatch(e['run_id'])]
        start, end, run_id, project = 0.0, 0.0, '', ''
        status, reason, stopped = 'unknown', '尚未找到可核验的 OpenClaw 本轮事件', False
        if events:
            latest = events[0]
            run_id = latest['run_id']
            same = [e for e in events if e['run_id'] == run_id]
            begins = [e for e in same if e['kind'] == 'session.started' and stamp(e['created_at']) <= now + 2]
            if begins:
                # OpenClaw can reuse a run ID across explicit execution
                # restarts. The index describes its latest execution segment.
                start = max(stamp(e['created_at']) for e in begins)
                cwd = next((e['cwd'] for e in begins if isinstance(e['cwd'], str) and e['cwd'].startswith('/')), '')
                project = cwd if len(cwd) <= 4096 and '\0' not in cwd else ''
                terminal = next((e for e in same if e['kind'] == 'session.ended' and
                                 start <= stamp(e['created_at']) <= now + 2), None)
                # A newer indexed run or a different last run invalidates old events.
                newer = stamp(entry['startedAt']) > start + 2
                conflicting = entry['lastRunId'] not in (None, '', run_id)
                if not newer and not conflicting:
                    if terminal and entry['status'] != 'running':
                        end = stamp(terminal['created_at'])
                        stopped = terminal['external_abort'] == 1
                        if stopped:
                            status, reason = 'interrupted', 'OpenClaw 明确记录本轮由外部主动停止'
                        elif terminal['timed_out'] == 1:
                            status, reason = 'interrupted', 'OpenClaw 明确记录本轮执行超时'
                        elif terminal['outcome'] in ('error', 'failed') or terminal['stop'] == 'error':
                            status, reason = 'interrupted', self._provider_reason(run_id)
                        elif terminal['aborted'] == 1 or terminal['stop'] == 'aborted':
                            status, reason = 'interrupted', 'OpenClaw 中止事件未记录触发者，触发者待确认'
                        elif terminal['outcome'] in ('success', 'completed') and terminal['stop'] in ('stop', 'end_turn'):
                            status, reason = 'completed', 'OpenClaw 同一轮次明确记录正常结束'
                    elif not terminal and entry['status'] == 'running':
                        # No invented interruption timeout. Stale activity is unknown.
                        age = now - max(start, stamp(entry['updated_at']))
                        if -2 <= age <= 300:
                            status, reason = 'running', 'OpenClaw 会话索引与本轮开始事件一致'
                        else:
                            reason = 'OpenClaw 本轮状态缺少新鲜证据，保持待确认'
                    elif terminal:
                        reason = 'OpenClaw 结束事件与索引运行状态冲突，可能仍在等待子任务'
        if entry['initializationPending'] or entry['pendingFinalDelivery']:
            status, reason = 'waiting', 'OpenClaw 正在等待初始化或最终交付，不自动恢复'
        title = entry['label'] or entry['display_name'] or ('OpenClaw · ' + key.split(':', 2)[-1])
        if not isinstance(title, str):
            return None
        row = dict(id='openclaw:' + agent + ':' + sid, app_id='openclaw', app_name='OpenClaw',
                   title=' '.join(title.split())[:160], project=project, status=status,
                   evidence=reason, status_reason=reason, source='local-db',
                   updated_at=max(stamp(entry['updated_at']), start, end),
                   target='',
                   navigation_key=key, user_stopped=stopped,
                   lifecycle_revision=entry['lifecycleRevision'], run_id=run_id,
                   manual_only=bool(entry['spawnedBy'] or entry['parentSessionKey'] or entry['subagentRole'] or entry['spawnDepth']))
        if start:
            row.update(started_at=start, timing_basis='turn', last_activity_at=max(start, end),
                       timing_reason='按原会话同一 run ID 最新执行段的开始和结束事件计算')
        if end:
            row['ended_at'] = end
        if row['id'] in openclaw_care.registry(self.care_home)[1]:
            row['continuous_authorized'] = True
            try:
                row['care_retry_at'] = openclaw_care.CareBudget(self.care_home).retry_at(row['id'], now)
            except (OSError, ValueError, TypeError):
                row['care_retry_at'] = now + openclaw_care.WINDOW_SECONDS
        return row

    def collect(self, session_id=None, include_children=False):
        self.last_errors = []
        self._error_metadata = None
        parsed = parse_id(session_id) if session_id else None
        if session_id and not parsed:
            return []
        paths = ([self.root / parsed[0] / 'agent' / 'openclaw-agent.sqlite'] if parsed else
                 sorted(self.root.glob('*/agent/openclaw-agent.sqlite'))[:AGENT_LIMIT])
        rows = []
        selected = self.selected_ids()
        for path in paths:
            agent = path.parent.parent.name
            if not path.is_file() or not AGENT.fullmatch(agent):
                continue
            try:
                # Only an exact user-authorized lookup can include a child;
                # provider-wide background discovery never starts child work.
                rows.extend(self._rows(path, agent, parsed[1] if parsed else None,
                                       include_children=bool(parsed and (include_children or session_id in selected))))
            except (OSError, sqlite3.Error, ValueError):
                self.last_errors.append('OpenClaw 本机会话索引或轮次事件暂不可读，状态待确认')
        if not session_id:
            for selected_id in selected:
                # User-selected children stay visible but never enter automatic supervision.
                pair = parse_id(selected_id)
                if pair:
                    path = self.root / pair[0] / 'agent/openclaw-agent.sqlite'
                    if path.is_file():
                        try:
                            rows.extend(self._rows(path, pair[0], pair[1], include_children=True))
                        except (OSError, sqlite3.Error, ValueError):
                            self.last_errors.append('OpenClaw 已选原会话暂不可读')
        unique = {row['id']: row for row in rows}
        priority = lambda r: (r['id'] in selected, r['updated_at'])
        return sorted(unique.values(), key=priority, reverse=True)[:SESSION_LIMIT]

    def selected_ids(self):
        return sorted(v for v in openclaw_care.registry(self.care_home)[0] if parse_id(v))

    def _provider_reason(self, run_id):
        # Only the gateway's fixed execution-error metadata lines are used.
        # The bounded map retains derived reasons, never log lines or error text.
        if self._error_metadata is None:
            self._error_metadata = {}
            path = self.home / ('.openclaw/logs/gateway.log' if __import__('os').name == 'nt' else 'Library/Logs/openclaw/gateway.log')
            try:
                with path.open('rb') as handle:
                    size = path.stat().st_size
                    handle.seek(max(0, size - 512 * 1024))
                    raw = handle.read(512 * 1024)
                if size > 512 * 1024:
                    raw = raw.split(b'\n', 1)[-1]
                for line in reversed(raw.decode('utf-8', errors='replace').splitlines()):
                    if len(line) > 4096 or 'isError=true' not in line:
                        continue
                    match = re.search(r'runId[=:]\s*([A-Za-z0-9_-]{1,128})\b', line)
                    if match and match[1] not in self._error_metadata:
                        derived = failure_reason(line)
                        if derived != '应用记录错误终止，未提供可识别的具体原因':
                            self._error_metadata[match[1]] = 'OpenClaw：' + derived
                    if len(self._error_metadata) >= 256:
                        break
            except OSError:
                pass
        return self._error_metadata.get(run_id, 'OpenClaw 明确记录本轮执行失败')


_collector = OpenClawCollector()


def collect():
    return _collector.collect()


def collect_session(session_id, include_children=False):
    rows = _collector.collect(session_id, include_children=include_children)
    return rows[0] if len(rows) == 1 else None


def diagnostics():
    return list(_collector.last_errors)
