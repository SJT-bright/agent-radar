#!/bin/zsh
# 不同软件真实链路测试的薄封装：定位项目根并运行 tests/link_checks.py。
set -euo pipefail
PROJECT_DIR="${0:A:h:h}"
cd "$PROJECT_DIR"
exec /usr/bin/python3 tests/link_checks.py "$@"
