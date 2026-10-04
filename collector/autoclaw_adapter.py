"""Read AutoClaw's own session lifecycle metadata, never the other OpenClaw home.

Bounded local reads; no gateway connection, credentials, writes, or messages.
Only short session titles and lifecycle timestamps leave the reader.
"""
from pathlib import Path
from datetime import datetime
import json
import math
import re
import subprocess
import time
try:
    from .failure_reasons import failure_reason
    from .autoclaw_titles import TitleReader
    from .transcript_scan import scan_blocks, complete_lines
except ImportError:
    from failure_reasons import failure_reason
    from autoclaw_titles import TitleReader
    from transcript_scan import scan_blocks, complete_lines

INDEX_LIMIT = 8 * 1024 * 1024
TAIL_LIMIT = 128 * 1024
TURN_LIMIT = 2 * 1024 * 1024
SESSION_LIMIT = 40
STALL_SECONDS = 300
FIELDS = ('sessionId', 'sessionFile', 'updatedAt', 'startedAt', 'endedAt',
          'status', 'abortedLastRun', 'autoclawUserStoppedAt', 'label', 'displayName', 'subject', 'title')


def epoch(value):
    try:
        if isinstance(value, str) and 'T' in value:
            number = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        else:
            number = float(value or 0)
            if number > 100_000_000_000:
                number /= 1000
        return number if math.isfinite(number) and number >= 0 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def live_pid(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    try:
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'comm='],
                                capture_output=True, text=True, timeout=0.5)
        command = result.stdout.strip()
        if result.returncode == 1 and not command:
            return False
        if result.returncode == 0 and ('openclaw' in command.lower() or '/AutoClaw.app/Contents/' in command):
            return True
        return None  # A failed probe or unfamiliar process name is not a death.
    except (OSError, subprocess.SubprocessError):
        return None


class AutoClawCollector:
    def __init__(self, home=None, clock=time.time, pid_checker=live_pid):
        self.root = (Path(home) if home is not None else Path.home()) / '.openclaw-autoclaw'
        self.clock, self.pid_checker = clock, pid_checker
        self.cache = {}
        self.previous = []
        self.last_errors = []
        self.title_reader = TitleReader(home)
        self.navigation_titles = {}

    def _index(self, path):
        stat = path.stat()
        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        key = str(path)
        cached = self.cache.get(key)
        if cached and cached[0] == signature:
            return cached[1]
        if stat.st_size > INDEX_LIMIT:
            raise ValueError('index limit')
        with path.open('rb') as f:
            raw = json.loads(f.read(INDEX_LIMIT + 1))
        if not isinstance(raw, dict):
            raise ValueError('index type')
        values = [dict({k: value[k] for k in FIELDS if k in value}, navigation_key=key)
                  for key, value in raw.items() if isinstance(value, dict) and value.get('sessionId')
                  and not re.match(r'^agent:[A-Za-z0-9_-]+:(?:subagent|cron|evolution-check):', key)]
        self.cache[key] = (signature, values)
        return values

    def _transcript(self, path):
        """Retain no prompts, tool arguments, results, or system reminders."""
        try:
            stat = path.stat()
            key = str(path)
            signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
            cached = self.cache.get(key)
            if cached and cached[0] == signature:
                return cached[1]
            with path.open('rb') as f:
                head = f.read(TAIL_LIMIT).splitlines()
            meta = {'activity': 0, 'user_at': 0, 'terminal_at': 0,
                    'timing_reason': '会话日志没有本轮开始事件'}
            for line in head:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get('type') == 'session' and isinstance(event.get('cwd'), str):
                    meta['project'] = event['cwd']
                message = event.get('message')
                if isinstance(message, dict) and message.get('role') == 'user' and not meta.get('title'):
                    content = message.get('content', '')
                    if isinstance(content, list):
                        content = '\n'.join(str(x.get('text', '')) for x in content if isinstance(x, dict))
                    if not isinstance(content, str):
                        continue
                    # AutoClaw wraps the authored request in these exact markers.
                    matches = re.findall(r'<<<AUTOCLAW_USER_AUTHORED_REQUEST_START>>>\s*\n(.*?)\n\s*<<<AUTOCLAW_USER_AUTHORED_REQUEST_END>>>', content, re.S)
                    authored = matches[-1] if matches else (content if '<system-reminder>' not in content else '')
                    attachments = re.findall(r'\[Attached file: (.*?) \([^\]]*?\) — [^\]]*\]', authored)
                    request = re.sub(r'\[Attached file: .*? \([^\]]*?\) — [^\]]*\]', '', authored).strip()
                    authored = request or ('附件：' + '、'.join(attachments) if attachments else authored)
                    if authored.strip():
                        meta['title'] = ' '.join(authored.split())[:100]

            def absorb(lines):
                for line in lines:
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    message = event.get('message')
                    if not isinstance(message, dict):
                        continue
                    stamp = epoch(event.get('timestamp') or message.get('timestamp'))
                    role = message.get('role')
                    if role in {'user', 'assistant', 'toolResult'}:
                        meta['activity'] = max(meta['activity'], stamp)
                    if role == 'user':
                        meta['user_at'] = max(meta['user_at'], stamp)
                    stop = message.get('stopReason')
                    if role == 'assistant' and stop in {'stop', 'end_turn', 'error', 'aborted'}:
                        meta['terminal_at'] = stamp
                        meta['terminal'] = 'completed' if stop in {'stop', 'end_turn'} else 'interrupted'
                        meta['terminal_reason'] = (failure_reason(message.get('errorMessage') or message.get('error'), stop)
                                                   if meta['terminal'] == 'interrupted' else '模型回复记录了正常结束标记')

            # One turn's tool traffic can push its user message past the tail
            # window; walk earlier blocks backwards until the turn is found.
            blocks = scan_blocks(stat.st_size, window=TURN_LIMIT)
            with path.open('rb') as f:
                for offset, (start, end) in enumerate(blocks):
                    f.seek(start)
                    data = f.read(end - start)
                    absorb(complete_lines(data, drop_first=start > 0))
                    if offset == 0:
                        meta['timing_reason'] = ('日志已回溯扫描仍未包含本轮开始事件' if stat.st_size > TURN_LIMIT
                                                 else '会话日志没有本轮开始事件')
                    if meta['user_at']:
                        break
            if len(self.cache) > 128:
                self.cache.clear()
            self.cache[key] = (signature, meta)
            return meta
        except (OSError, ValueError) as error:
            reason = ('未找到对应的会话日志' if isinstance(error, FileNotFoundError) else
                      '系统拒绝读取会话日志' if isinstance(error, PermissionError) else '会话日志暂不可读')
            return {'timing_reason': reason + '，无法取得本轮开始时间'}

    def _row(self, entry, directory, now):
        sid = entry.get('sessionId')
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', sid):
            return None
        path = directory / (sid + '.jsonl')
        # Ignore redirected sessionFile paths and symlinks escaping this agent.
        if not path.resolve().is_relative_to(directory.resolve()):
            return None
        meta = self._transcript(path)
        start = max(epoch(entry.get('startedAt')), meta.get('user_at', 0))
        updated = epoch(entry.get('updatedAt'))
        activity = max(meta.get('activity', 0), start)
        ended = epoch(entry.get('endedAt'))
        raw = entry.get('status')
        terminal = meta.get('terminal') if meta.get('terminal_at', 0) >= start and meta.get('terminal_at', 0) > 0 else None
        pid, lock_start = None, 0
        lock_exists, lock_readable, lock_dead = False, True, False
        try:
            lock = path.with_suffix('.jsonl.lock')
            lock_exists = lock.exists()
            if lock_exists:
                if lock.stat().st_size > 4096:
                    raise ValueError('lock limit')
                data = json.loads(lock.read_bytes())
                candidate = data.get('pid')
                lock_start = epoch(data.get('createdAt'))
                alive = self.pid_checker(candidate)
                lock_dead = alive is False
                if alive is True:
                    pid = candidate
        except (OSError, ValueError, AttributeError):
            lock_readable = False
        # AutoClaw renews/recreates locks within one long turn. Lock creation is
        # NOT a turn start or a progress heartbeat. Keep the lifecycle timestamp.
        if pid and raw == 'running' and lock_start > meta.get('terminal_at', 0) + 1:
            terminal = None
        if (raw in {'failed', 'killed', 'timeout'} or entry.get('abortedLastRun') is True) and max(updated, ended) >= start:
            status = 'interrupted'
            evidence = 'AutoClaw 明确记录：' + str(raw or 'abortedLastRun')
            ended = ended or updated
        elif raw == 'done' and ended >= start and ended > 0:
            status, evidence = 'completed', 'AutoClaw 明确记录本轮结束'
        elif terminal:
            status, evidence = terminal, 'AutoClaw 最新回复明确记录终止原因'
            ended = meta['terminal_at']
        elif raw == 'running' or pid:
            if pid:
                status = 'running' if -30 <= now - activity <= STALL_SECONDS else 'stalled'
                evidence = ('本轮会话锁与存活进程匹配' if status == 'running' else
                            '会话锁进程仍在，但超过 5 分钟无会话事件；可能仍在等待长任务')
            elif lock_exists and lock_readable and lock_dead:
                status, evidence = 'interrupted', 'AutoClaw 会话锁仍在，但对应执行进程已退出'
                ended = activity  # Last confirmed event, not an invented crash time.
            elif now - max(activity, updated) > 15:
                status, evidence = 'stalled', '索引仍标运行，但未找到可核验的本轮执行进程；疑似中断'
            else:
                status, evidence = 'unknown', 'AutoClaw 正在切换会话状态，等待执行进程确认'
        else:
            status, evidence = 'unknown', '缺少 AutoClaw 当前轮次状态证据'
        title = next((entry.get(k) for k in ('label', 'displayName', 'subject', 'title') if entry.get(k)), None)
        title = title or meta.get('title') or ('会话 ' + sid[:8])
        row = dict(id='autoclaw:' + sid, app_id='autoclaw', app_name='AutoClaw',
                   title=str(title)[:160], project=meta.get('project', ''), status=status,
                   evidence=evidence, updated_at=max(updated, activity, ended),
                   source='local-session', target='')
        reason = evidence
        if status == 'interrupted':
            if terminal == 'interrupted':
                reason = meta.get('terminal_reason', evidence)
            elif lock_dead:
                reason = '会话锁对应的执行进程已退出；没有日志能够确认进程退出原因'
            else:
                reason = failure_reason(stop=raw or 'aborted')
        row['status_reason'] = reason
        key = entry.get('navigation_key', '')
        row['user_stopped'] = bool(start and epoch(entry.get('autoclawUserStoppedAt')) >= start)
        if row['user_stopped'] and status == 'interrupted':
            row['status_reason'] = 'AutoClaw 记录本轮由用户主动停止'
        if re.fullmatch(r'agent:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+', key):
            row['navigation_key'] = key
            if key in self.navigation_titles:
                row['navigation_title'] = self.navigation_titles[key]
                row['title'] = self.navigation_titles[key]
        row['timing_reason'] = ('从本轮用户消息或明确开始事件计算' if start else
                                meta.get('timing_reason', 'AutoClaw 没有提供本轮开始时间'))
        if start:
            row.update(started_at=start, timing_basis='turn')
            if status == 'interrupted' and lock_dead and raw == 'running':
                row['timing_basis'] = 'last-confirmed'
                row['timing_reason'] = '进程已退出但缺少退出时间，计时截止到最后确认事件'
        if status in {'completed', 'interrupted'} and ended >= start and ended:
            row['ended_at'] = ended
        if activity:
            row['last_activity_at'] = activity
        # Gateway PID is not a GUI application PID: do not hand it to activation.
        return row

    def collect(self):
        self.last_errors = []
        now, rows = self.clock(), []
        self.navigation_titles = self.title_reader.read()
        try:
            directories = sorted((self.root / 'agents').glob('*/sessions'))[:16]
            candidates = []
            for directory in directories:
                index = directory / 'sessions.json'
                if index.exists():
                    candidates.extend((entry, directory) for entry in self._index(index))
            candidates.sort(key=lambda pair: (pair[0].get('status') == 'running', epoch(pair[0].get('updatedAt'))), reverse=True)
            for entry, directory in candidates[:SESSION_LIMIT]:
                row = self._row(entry, directory, now)
                if row:
                    rows.append(row)
            self.previous = rows
            return rows
        except (OSError, ValueError, TypeError) as error:
            self.last_errors = ['AutoClaw：会话索引读取失败（' + type(error).__name__ + '）']
            return [dict(row, status='unknown', evidence='AutoClaw 索引暂不可读；保留上次会话，当前状态待确认',
                         status_reason='AutoClaw 会话索引本轮读取失败，缓存不能证明当前状态')
                    for row in self.previous]


_collector = AutoClawCollector()

def collect():
    return _collector.collect()

def diagnostics():
    return list(_collector.last_errors)
