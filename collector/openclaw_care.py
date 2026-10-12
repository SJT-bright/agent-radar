"""App-owned, bounded OpenClaw care authorization and automatic-send budget."""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

LIMIT = 3
WINDOW_SECONDS = 3600
MAX_IDS = 40
MAX_BYTES = 65536
ID = re.compile(r'openclaw:[A-Za-z0-9_-]{1,80}:[0-9a-fA-F-]{36}')


def support_dir(home=None):
    explicit_home = home is not None
    home = Path(home) if explicit_home else Path.home()
    if os.name == 'nt':
        roaming = home / 'AppData/Roaming' if explicit_home else Path(os.environ.get('APPDATA', str(home / 'AppData/Roaming')))
        return roaming / 'AgentRadar'
    return home / 'Library/Application Support/AgentRadar'


def registry(home=None):
    path = support_dir(home) / 'selected-openclaw-sessions.json'
    try:
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            return set(), set()
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') != 1:
            return set(), set()
        ids = {x for x in data.get('ids', [])[:MAX_IDS] if isinstance(x, str) and ID.fullmatch(x)}
        care = {x for x in data.get('care_ids', [])[:MAX_IDS] if isinstance(x, str) and ID.fullmatch(x)}
        return ids, care & ids
    except (OSError, ValueError, TypeError, AttributeError):
        return set(), set()


class CareBudget:
    def __init__(self, home=None):
        self.path = support_dir(home) / 'openclaw-care-attempts.json'

    def load(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or self.path.stat().st_size > MAX_BYTES:
            raise ValueError('care state bounded')
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or len(data) > MAX_IDS:
            raise ValueError('care state invalid')
        for key, stamps in data.items():
            if not re.fullmatch(r'[0-9a-f]{64}', key) or not isinstance(stamps, list) or len(stamps) > LIMIT:
                raise ValueError('care state invalid')
            if any(isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t < float('inf') for t in stamps):
                raise ValueError('care state invalid')
        return data

    @staticmethod
    def key(session_id):
        return hashlib.sha256(session_id.encode('utf-8')).hexdigest()

    def retry_at(self, session_id, now):
        times = [t for t in self.load().get(self.key(session_id), []) if now - t < WINDOW_SECONDS]
        return min(times) + WINDOW_SECONDS if len(times) >= LIMIT else 0

    def reserve(self, session_id, now):
        # Caller holds the existing global Journal lock. A consumed request,
        # including an ambiguous timeout, must retain this budget slot.
        data = self.load()
        data = {k: [t for t in v if now - t < WINDOW_SECONDS] for k, v in data.items()}
        data = {k: v for k, v in data.items() if v}
        key = self.key(session_id)
        times = data.get(key, [])
        if len(times) >= LIMIT or (key not in data and len(data) >= MAX_IDS):
            raise ValueError('care budget exhausted')
        data[key] = times + [now]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=str(self.path.parent), prefix='.care-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
