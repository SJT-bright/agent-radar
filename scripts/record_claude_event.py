#!/usr/bin/env python3
"""Record one Claude Code hook payload into this app's lifecycle file.

Stdout stays empty while recording: UserPromptSubmit would otherwise treat
stdout as conversation context, and a non-zero exit would block Stop.
This process never reads or writes Claude's settings. Review the text from
`--print-config` and merge it yourself if you want the hooks.
"""
from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path
from typing import BinaryIO, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collector import hook_events


def instructions() -> str:
    command = shlex.quote(sys.executable) + " " + shlex.quote(str(Path(__file__).resolve()))
    snippet = {
        "hooks": {
            name: [{"hooks": [{"type": "command", "timeout": 5, "command": command}]}]
            for name in ("UserPromptSubmit", "Stop", "StopFailure", "PermissionRequest")
        }
    }
    return (
        "不会修改 ~/.claude，也不会安装 hook。审查下面的说明后，如需启用，再手工合并进\n"
        "Claude Code 的 ~/.claude/settings.json。\n"
        "脚本只把 session_id、turn_id（官方字段 prompt_id）、event、timestamp、cwd 写入\n"
        "本应用自己的 ~/Library/Application Support/AgentRadar/claude-lifecycle.jsonl。\n"
        "prompt、tool_input、tool 参数、last_assistant_message 和 transcript_path 一律丢弃。\n"
        "必须同时接入 UserPromptSubmit，后续事件才有可核验的轮次；缺少轮次的事件会被拒绝。\n"
        "重复事件会去重。旧轮晚到和未来时间会被拒绝，不会改写当前轮。\n"
        "官方 Stop 在用户主动中断时不会触发，不能用空闲时间猜测中断。\n"
        "权限等待来自 PermissionRequest，记为等待权限，不是结束，也不是主动停止。\n"
        "Stop 记为正常结束；StopFailure 记为失败结束，不是主动停止。\n"
        "stop_hook_active 为 true 时 Stop 表示仍在继续，不记为结束。\n"
        "主动停止只接受显式 event=user_stopped，且不能同时带 hook_event_name。例如：\n"
        "printf '%s' '{\"event\":\"user_stopped\",\"session_id\":\"SID\",\"turn_id\":\"TURN\",\"cwd\":\"/path\",\"timestamp\":1700000000}' | "
        + command + "\n"
        + json.dumps(snippet, ensure_ascii=False, indent=2) + "\n"
    )


def read_payload(stream: BinaryIO) -> tuple[dict | None, str]:
    blob = stream.read(hook_events.MAX_STDIN + 1)
    if len(blob) > hook_events.MAX_STDIN:
        return None, "bounded"
    if not blob.strip():
        return None, "empty"
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None, "invalid"
    if not isinstance(data, dict):
        return None, "invalid"
    return data, "ok"


def main(argv: list[str] | None = None, stdin: BinaryIO | None = None,
         path: Path | None = None, now: float | None = None,
         stdout: TextIO | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    stdin = sys.stdin.buffer if stdin is None else stdin
    path = hook_events.default_path() if path is None else path
    now = time.time() if now is None else now
    stdout = sys.stdout if stdout is None else stdout
    try:
        if "--print-config" in argv:
            stdout.write(instructions())
            return 0
        payload, reason = read_payload(stdin)
        if reason != "ok" or payload is None:
            return 0
        hook_events.record(payload, path, now=now)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
