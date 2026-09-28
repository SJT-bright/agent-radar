"""Bundle just the supplied watchdog AX/input core and its installed PyObjC runtime."""
from pathlib import Path
import shutil
import sys

root = Path(__file__).resolve().parents[1]
out = Path(sys.argv[1])
upstream = root / 'vendor/watchdog'
sites = list((upstream / '.venv-mac/lib').glob('python*/site-packages'))
site = sites[0] if len(sites) == 1 else None
if site is None or not (site / 'objc').exists():
    raise SystemExit('缺少 PyObjC 运行环境，请在 vendor/watchdog 安装 requirements-mac.txt')
for relative in ('aiwatch/__init__.py', 'aiwatch/types.py', 'aiwatch/mac/__init__.py',
                 'aiwatch/mac/ax.py', 'aiwatch/mac/winops.py', 'aiwatch/mac/inject.py'):
    destination = out / 'watchdog' / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(upstream / relative, destination)
shutil.copytree(root / 'supervisor', out / 'supervisor', dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
# Only PyObjC frameworks; no pip, settings, histories, keys, OCR or old daemon.
for source in site.iterdir():
    if source.is_dir() and (source.name == 'objc' or source.name[:1].isupper() or source.name.startswith('pyobjc_')):
        shutil.copytree(source, out / 'python' / source.name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
