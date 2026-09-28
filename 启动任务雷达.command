#!/bin/zsh
set -euo pipefail
PROJECT_DIR="${0:A:h}"
if [[ -d "/Applications/任务雷达.app" ]]; then
  open "/Applications/任务雷达.app"
  exit 0
fi
if [[ ! -d "$PROJECT_DIR/build/任务雷达.app" ]]; then
  /bin/zsh "$PROJECT_DIR/scripts/build.sh"
fi
open "$PROJECT_DIR/build/任务雷达.app"
