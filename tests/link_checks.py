#!/usr/bin/python3
"""不同软件真实链路测试：真实本地数据源 → 采集器行 → 跳转目标 → 系统注册的处理应用。

对每个被监控软件验证一条完整链路是否成立：
  1. 真实本地数据源（~/.codex、~/.zcode 等）被采集器读出——用
     collector.main.snapshot() 取一次完整快照，再逐软件校验行；
  2. 每一行的 id / project / target 满足该软件的不变量；
  3. 深链 target 的 scheme/host/path/query 逐段正确；
  4. 深链对应的处理应用确实安装于 /Applications，且 Info.plist 中的
     CFBundleIdentifier 与 CFBundleURLSchemes 与预期一致（plistlib 直读）。

只读边界（绝对约束）：
  - 不写入、修改或删除任何应用数据；不向任何应用安装钩子；
  - 不启动任何 .app（不调用 open、不 osascript 激活），不发通知；
  - 本脚本自身不直接打开任何数据库；快照由采集器以 mode=ro 只读连接取得，
    本脚本对磁盘只做 plist 读取与 os.path.isdir 存在性判断；
  - 不写任何文件（/tmp 也不需要）；
  - 不输出会话正文与标题，只输出计数、状态、行 id 等元数据。

结论分级：
  FAIL = 链路断言不成立（任一 FAIL 则退出码 1）；
  WARN = 链路成立但有老化信号（如工作区路径已不在磁盘上；退出码仍为 0）；
  INFO = 正常但值得记录的事实（应用未安装、本轮无行、AutoClaw 未注册
         URL scheme 等），不影响退出码。
"""
from __future__ import annotations

import os
import plistlib
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# collector/main.py 以平铺模块名导入兄弟适配器（import codex_adapter 等），
# 所以 collector 目录本身也必须在 sys.path 上，脚本才能从任意目录运行。
_COLLECTOR = str(ROOT / "collector")
if _COLLECTOR not in sys.path:
    sys.path.insert(0, _COLLECTOR)

from collector import main as collector_main  # noqa: E402

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

PASS, INFO, WARN, FAIL = "PASS", "INFO", "WARN", "FAIL"
RANK = {PASS: 0, INFO: 0, WARN: 1, FAIL: 2}

# app_id → (显示名, 处理应用, 期望 bundle id, 期望 scheme；None 表示不要求 scheme)
HANDLERS = [
    ("codex", "Codex", "ChatGPT.app", "com.openai.codex", "codex"),
    ("claude-code", "Claude Code", None, None, None),
    ("zcode", "ZCode", "ZCode.app", "dev.zcode.app", "zcode"),
    ("workbuddy", "WorkBuddy", "WorkBuddy.app", "com.tencent.workbuddy.mac", "workbuddy"),
    ("workbuddy-ai", "WorkBuddy AI", "WorkBuddy AI.app", "com.workbuddy.workbuddy-ai", "workbuddy-ai"),
    ("autoclaw", "AutoClaw", "AutoClaw.app", "com.zhipuai.autoclaw", None),
    ("qoder-cn", "Qoder CN", "Qoder CN.app", "com.qodercn.app", None),
    ("qoder", "Qoder", "Qoder.app", "com.qoder.app", None),
    ("grok", "Grok", "Grok.app", "com.grokapp.desktop", None),
    ("cline", "Cline", "Cline.app", "bot.cline.app", None),
]

# 各软件真实本地数据源（只做存在性判断，不打开内容）
SOURCES = {
    "codex": "~/.codex 的 state_*.sqlite 与 thread_history_1.sqlite",
    "claude-code": "~/.claude（projects/ 与 sessions/）",
    "zcode": "~/.zcode/v2/tasks-index.sqlite",
    "workbuddy": "~/.workbuddy/workbuddy.db",
    "workbuddy-ai": "~/.workbuddy-ai/workbuddy.db",
    "autoclaw": "~/.openclaw-autoclaw/agents",
    "qoder-cn": "~/Library/Application Support/com.qodercn.app.stable",
    "qoder": "~/Library/Application Support/com.qoder.app.stable",
    "grok": "~/Library/Application Support/com.grokapp.grok-app",
    "cline": "~/.cline/data/db",
}


def _id_head(value: str, limit: int = 64) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def _source_facts(app_id: str) -> list:
    """数据源存在性元数据（只 stat/glob 名字，不读内容）。"""
    home = Path.home()
    if app_id == "codex":
        states = sorted((home / ".codex").glob("state_*.sqlite"))
        history = (home / ".codex" / "thread_history_1.sqlite").is_file()
        return [("存在 state_%d 个，history 表 %s"
                 % (len(states), "存在" if history else "缺失"))]
    if app_id == "claude-code":
        base = home / ".claude"
        return [("projects/ %s、sessions/ %s"
                 % ("存在" if (base / "projects").is_dir() else "缺失",
                    "存在" if (base / "sessions").is_dir() else "缺失"))]
    paths = {
        "zcode": home / ".zcode" / "v2" / "tasks-index.sqlite",
        "workbuddy": home / ".workbuddy" / "workbuddy.db",
        "workbuddy-ai": home / ".workbuddy-ai" / "workbuddy.db",
    }
    if app_id in paths:
        return [("存在" if paths[app_id].is_file() else "缺失")]
    if app_id == "autoclaw":
        agents = home / ".openclaw-autoclaw" / "agents"
        try:
            count = sum(1 for p in agents.glob("*/sessions") if p.is_dir())
        except OSError:
            count = 0
        return [("agents 会话目录 %d 个" % count)]
    return []


def read_handler(app_name):
    """plistlib 直读 /Applications/<App>.app/Contents/Info.plist。"""
    info = {"app": app_name, "installed": False,
            "bundle_id": "", "schemes": [], "has_url_types": None}
    if app_name is None:
        return info
    plist = Path("/Applications") / app_name / "Contents" / "Info.plist"
    if not plist.is_file():
        return info
    try:
        with plist.open("rb") as stream:
            data = plistlib.load(stream)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        info["bundle_id"] = "<Info.plist 不可读：%s>" % type(exc).__name__
        return info
    info["installed"] = True
    bundle = data.get("CFBundleIdentifier")
    info["bundle_id"] = bundle if isinstance(bundle, str) else ""
    types = data.get("CFBundleURLTypes")
    if isinstance(types, list):
        info["has_url_types"] = True
        for entry in types:
            if isinstance(entry, dict):
                schemes = entry.get("CFBundleURLSchemes")
                if isinstance(schemes, list):
                    info["schemes"].extend(
                        s for s in (str(x) for x in schemes) if s)
    else:
        info["has_url_types"] = False
    return info


def _handler_desc(handler, want_scheme):
    if handler["app"] is None:
        return "无（CLI，target 刻意留空）"
    if not handler["installed"]:
        return "未安装(%s)" % handler["app"]
    base = handler["bundle_id"] or "<无 CFBundleIdentifier>"
    if not want_scheme:
        return base + "(无 URL scheme 要求)"
    if want_scheme in handler["schemes"]:
        return "%s(%s)" % (base, want_scheme)
    return "%s(缺 scheme %s)" % (base, want_scheme)


# ---------- 逐行不变量校验：每个函数返回 [(级别, 原因)] ----------

def _codex_row(row):
    out = []
    rid = str(row.get("id", ""))
    if not rid.startswith("codex:"):
        return [(FAIL, "id 缺少 codex: 前缀（id=%s）" % _id_head(rid))]
    thread = rid[len("codex:"):]
    if not UUID_RE.match(thread):
        return [(FAIL, "thread id 不是合法 UUID（id=%s）" % _id_head(rid))]
    target = str(row.get("target", ""))
    expected = "codex://threads/" + quote(thread, safe="")
    if target != expected:
        return [(FAIL, "target 与 codex://threads/<uuid> 不一致（id=%s）"
                % _id_head(rid))]
    parts = urlsplit(target)
    if parts.scheme != "codex":
        out.append((FAIL, "深链 scheme 应为 codex（id=%s）" % _id_head(rid)))
    if parts.netloc.lower() != "threads":
        out.append((FAIL, "深链 host 应为 threads（id=%s）" % _id_head(rid)))
    if parts.path != "/" + thread:
        out.append((FAIL, "深链 path 应为 /<uuid>（id=%s）" % _id_head(rid)))
    if parts.query:
        out.append((FAIL, "深链不应有 query（id=%s）" % _id_head(rid)))
    if parts.fragment:
        out.append((FAIL, "深链不应有 fragment（id=%s）" % _id_head(rid)))
    return out


def _zcode_adapter_link(project: str) -> str:
    """镜像 collector/desktop_adapters.py 的 _zcode_workspace_link 规则。"""
    if not project.startswith("/") or len(project) > 512:
        return ""
    segments = project.split("/")[1:]
    if any(not part or part in {".", ".."} for part in segments):
        return ""
    if any(ch in project for ch in ("\0", "\n", "\r")):
        return ""
    return "zcode://workspace/open?path=" + quote(project, safe="")


def _zcode_row(row):
    out = []
    target = str(row.get("target", ""))
    project = str(row.get("project", ""))
    expected = _zcode_adapter_link(project)
    task_valid = (project.startswith("/") and len(project) <= 512
                  and ".." not in project.split("/"))
    if expected:
        if target != expected:
            return [(FAIL, "target 与 zcode://workspace/open?path=<quote> 不一致"
                    "（id=%s）" % _id_head(str(row.get("id", ""))))], 0
        parts = urlsplit(target)
        segments_ok = (parts.scheme == "zcode"
                       and parts.netloc.lower() == "workspace"
                       and parts.path == "/open" and not parts.fragment)
        params = parse_qs(parts.query)
        raw = parts.query[len("path="):] if parts.query.startswith("path=") else None
        decoded = unquote(raw) if raw is not None else None
        if (not segments_ok or decoded != project
                or params.get("path") != [project]):
            out.append((FAIL, "深链 scheme/host/path/query 逐段校验失败"
                        "（id=%s）" % _id_head(str(row.get("id", "")))))
            return out, 0
        if os.path.isdir(project):
            return out, 1  # 完整链路：深链正确且工作区仍在磁盘上
        out.append((WARN, "工作区路径已不在磁盘上（链接老化信号）：%s"
                    % _id_head(project, 120)))
        return out, 0
    if target != "":
        out.append((FAIL, "project 非法但 target 非空（id=%s）"
                    % _id_head(str(row.get("id", "")))))
    elif task_valid:
        out.append((INFO, "project 含空段/'.'/段或控制字符，按适配器规则留空"
                    " target（id=%s）" % _id_head(str(row.get("id", "")))))
    return out, 0


def _workbuddy_row(row):
    out = []
    rid = str(row.get("id", ""))
    if not rid.startswith("workbuddy:"):
        return [(FAIL, "id 缺少 workbuddy: 前缀（id=%s）" % _id_head(rid))]
    session = rid[len("workbuddy:"):]
    if not session:
        return [(FAIL, "session id 为空（id=%s）" % _id_head(rid))]
    target = str(row.get("target", ""))
    expected = "workbuddy://chat/" + quote(session, safe="")
    if target != expected:
        return [(FAIL, "target 与 workbuddy://chat/<quote(session_id)> 不一致"
                "（id=%s）" % _id_head(rid))]
    parts = urlsplit(target)
    if (parts.scheme != "workbuddy" or parts.netloc.lower() != "chat"
            or parts.path != "/" + quote(session, safe="")
            or parts.query or parts.fragment):
        out.append((FAIL, "深链 scheme/host/path 逐段校验失败（id=%s）"
                    % _id_head(rid)))
    return out


def _empty_target_row(row, expect_note):
    target = str(row.get("target", ""))
    if target != "":
        return [(FAIL, "%s，实际 target=%r（id=%s）"
                % (expect_note, target[:60], _id_head(str(row.get("id", "")))))]
    return []


ROW_CHECKS = {
    "codex": _codex_row,
    "zcode": _zcode_row,
    "workbuddy": _workbuddy_row,
    "workbuddy-ai": lambda row: _empty_target_row(
        row, "WorkBuddy AI 的 target 应为空（CN 版深链与经典版冲突，项目刻意留空）"),
    "autoclaw": lambda row: _empty_target_row(
        row, "AutoClaw 的 target 应为空（无深链 scheme）"),
    "claude-code": lambda row: _empty_target_row(
        row, "Claude Code 的 target 应为空（claude-cli:// 为 OAuth 用途，项目刻意留空）"),
    "qoder-cn": lambda row: _empty_target_row(
        row, "Qoder CN 的 target 应为空（跳转为应用切换）"),
    "qoder": lambda row: _empty_target_row(
        row, "Qoder 的 target 应为空（跳转为应用切换）"),
    "grok": lambda row: _empty_target_row(
        row, "Grok 的 target 应为空（跳转为应用切换）"),
    "cline": lambda row: _empty_target_row(
        row, "Cline 的 target 应为空（跳转为应用切换）"),
}


def check_app(app_id, label, bundle, want_bundle, want_scheme, rows):
    findings = []
    handler = read_handler(bundle)
    handler_desc = _handler_desc(handler, want_scheme)

    # 处理应用核验
    if bundle is not None and handler["installed"]:
        if want_bundle and handler["bundle_id"] != want_bundle:
            findings.append((WARN, "CFBundleIdentifier=%r 与预期 %r 不一致"
                             % (handler["bundle_id"], want_bundle)))
        if want_scheme and want_scheme not in handler["schemes"]:
            level = FAIL if any(str(r.get("target", "")) for r in rows) else WARN
            findings.append((level, "应用已安装但未注册 URL scheme %s（现有：%s）"
                             % (want_scheme, ",".join(handler["schemes"]) or "无")))
        if want_scheme is None and handler["has_url_types"] is False:
            findings.append((INFO, "Info.plist 无 CFBundleURLTypes（%s 未注册"
                             "任何 URL scheme；其 target 本就为空，属正常）" % bundle))
    if bundle is None:
        findings.append((INFO, "CLI 软件无 .app 处理应用，仅校验 target 留空"))
    elif not handler["installed"]:
        findings.append((INFO, "处理应用未安装于 /Applications：%s" % bundle))
        for row in rows:
            if str(row.get("target", "")):
                findings.append((FAIL, "行 %s 的 target 需要处理应用 %s，但应用未安装"
                                 % (_id_head(str(row.get("id", ""))), bundle)))

    # 逐行不变量
    deep_links = 0
    full_chain = 0
    check = ROW_CHECKS[app_id]
    for row in rows:
        if str(row.get("target", "")):
            deep_links += 1
        if app_id == "zcode":
            row_findings, hit = check(row)
            full_chain += hit
        else:
            row_findings = check(row)
        findings.extend(row_findings)

    if not rows:
        findings.append((INFO, "本轮快照无该软件的行"))
    for note in _source_facts(app_id):
        findings.append((INFO, "数据源 %s：%s" % (SOURCES[app_id], note)))
    statuses = {}
    for row in rows:
        key = str(row.get("status", "?"))
        statuses[key] = statuses.get(key, 0) + 1
    if rows:
        findings.append((INFO, "状态计数：" + " ".join(
            "%s=%d" % (k, statuses[k]) for k in sorted(statuses))))

    verdict = FAIL if any(lv == FAIL for lv, _ in findings) else \
        WARN if any(lv == WARN for lv, _ in findings) else PASS
    line = "%-13s %-5s 行=%d 深链=%d 处理器=%s" % (
        label, verdict, len(rows), deep_links, handler_desc)
    if app_id == "zcode" and rows:
        line += " 落盘=%d" % full_chain
    print(line)
    for level, message in findings:
        print("    %-4s %s" % (level, message))
    return verdict


def main() -> int:
    print("== 不同软件真实链路测试 ==")
    try:
        snap = collector_main.snapshot()
    except Exception as exc:  # 快照失败即链路第一步断裂
        print("FAIL 采集器快照失败：%s" % type(exc).__name__)
        return 1
    rows = [r for r in snap.get("sessions", []) if isinstance(r, dict)]
    errors = [str(e) for e in snap.get("errors", []) if e]
    by_app = {}
    for row in rows:
        by_app.setdefault(str(row.get("app_id", "")), []).append(row)
    print("快照会话总数=%d 采集诊断=%d 条" % (len(rows), len(errors)))
    for message in errors:
        print("    诊断 INFO %s" % message)

    worst = PASS
    for app_id, label, bundle, want_bundle, want_scheme in HANDLERS:
        verdict = check_app(app_id, label, bundle, want_bundle,
                            want_scheme, by_app.get(app_id, []))
        worst = max(worst, verdict, key=lambda lv: RANK[lv])
    for unknown in sorted(set(by_app) - {h[0] for h in HANDLERS}):
        print("%-13s FAIL  行=%d 处理器=-（未知 app_id，适配器口径漂移）"
              % (unknown, len(by_app[unknown])))
        worst = FAIL

    fails = 1 if worst == FAIL else 0
    print("== 汇总：最差结论=%s → 退出码 %d ==" % (worst, fails))
    return fails


if __name__ == "__main__":
    sys.exit(main())
