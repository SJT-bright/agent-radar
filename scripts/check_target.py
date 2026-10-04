"""Read-only desktop compatibility probe; never activate, navigate, input or send."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = Path('/Applications/任务雷达.app/Contents/Resources')
for path in (RUNTIME / 'python', RUNTIME / 'watchdog', PROJECT / 'collector', PROJECT):
    sys.path.insert(0, str(path))

from supervisor.bridge import BUNDLES, Blocked, MacBackend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', choices=sorted(BUNDLES))
    parser.add_argument('session_id', help='Exact collector row ID, including the application prefix')
    args = parser.parse_args()
    if args.app == 'autoclaw':
        from collector.autoclaw_adapter import collect
    elif args.app == 'codex':
        from collector.codex_adapter import collect
    elif args.app in ('grok', 'qoder-cn', 'qoder'):
        from collector.extended_adapters import collect
    else:
        from collector.desktop_adapters import collect
    rows = [row for row in collect() if row['app_id'] == args.app and row['id'] == args.session_id]
    try:
        if len(rows) != 1:
            raise Blocked('target_unverified')
        result = MacBackend(dict(rows[0], mode='inspect')).inspect()
    except Blocked as error:
        result = dict(code=str(error), attempted=False)
    result.update(app_id=args.app, session_hash=hashlib.sha256(args.session_id.encode()).hexdigest())
    print(json.dumps(result))
    return 0 if result['code'] in ('ready', 'already_running') else 1


if __name__ == '__main__':
    sys.exit(main())
