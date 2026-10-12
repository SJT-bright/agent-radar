"""Resolve an explicitly local OpenClaw endpoint and native/WSL executable."""
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit
try:
    from collector.openclaw_care import support_dir
except ImportError:
    from openclaw_care import support_dir


def own_config(name, home=None):
    path = support_dir(home) / name
    try:
        if path.is_symlink() or path.stat().st_size > 16384:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def local_url(home=None):
    default = 'ws://127.0.0.1:18789' if os.name == 'nt' else 'ws://127.0.0.1:19189'
    raw = own_config('openclaw-local-gateway.json', home).get('url', default)
    try:
        parsed = urlsplit(raw)
        valid = (parsed.scheme == 'ws' and parsed.hostname in ('127.0.0.1', '::1', 'localhost')
                 and parsed.username is None and parsed.password is None and parsed.path in ('', '/')
                 and not parsed.query and not parsed.fragment and parsed.port is not None and 1 <= parsed.port <= 65535)
    except (ValueError, TypeError, AttributeError):
        valid = False
    return raw.rstrip('/') if valid else None


def platform_config(home=None):
    raw = own_config('platform-config.json', home)
    target = raw.get('openclaw_home')
    if target:
        if not isinstance(target, str) or len(target) > 4096 or '\0' in target:
            return {}
        if os.name == 'nt' and target.startswith('\\\\') and not target.lower().startswith(('\\\\wsl.localhost\\', '\\\\wsl$\\')):
            return {}
    return raw


def source_home(home=None):
    if home is not None:
        return Path(home)
    selected = platform_config().get('openclaw_home')
    if selected:
        path = Path(selected)
        if path.is_absolute():
            return path.parent if path.name == '.openclaw' else path
    return Path.home()


def cli_prefix():
    if os.name != 'nt':
        candidates = [Path.home() / '.local/bin/openclaw', Path('/opt/homebrew/bin/openclaw'), Path('/usr/local/bin/openclaw')]
        found = next((str(p) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
        return [found] if found else None
    config = platform_config()
    distro = config.get('wsl_distribution', '')
    if distro:
        binary = config.get('wsl_executable', '/usr/local/bin/openclaw')
        wsl = shutil.which('wsl.exe')
        if (not wsl or not isinstance(distro, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', distro)
                or not isinstance(binary, str) or len(binary) > 4096 or not binary.startswith('/')
                or Path(binary).name not in ('openclaw','openclaw.mjs')
                or any(c in binary for c in ('\0', '\r', '\n'))):
            return None
        return [wsl, '--distribution', distro, '--exec', binary]
    # Execute Node's module directly: a .cmd wrapper plus a user prompt would
    # introduce cmd.exe expansion/quoting. No prompt is ever passed to a shell.
    node = shutil.which('node.exe')
    roaming = Path(os.environ.get('APPDATA', str(Path.home() / 'AppData/Roaming')))
    roots = [roaming / 'npm/node_modules/openclaw', Path.home() / '.local/lib/node_modules/openclaw']
    module = next((p / 'openclaw.mjs' for p in roots if (p / 'openclaw.mjs').is_file()), None)
    return [node, str(module)] if node and module else None
