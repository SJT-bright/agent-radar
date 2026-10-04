"""macOS 窗口枚举 / 前台控制 / 会话状态。

三条实测出来的规矩：
1. CGWindowList 的 kCGWindowName 对无标题窗口会给空串甚至残留旧值，
   标题与最小化状态都以该 pid 的 AX 窗口列表为准（kAXTitleAttribute / kAXMinimizedAttribute）。
2. layer != 0 的都是菜单栏、通知中心、Dock、输入法面板之类的外壳窗口，不是用户窗口。
3. 抢前台必须回读 frontmostApplication 校验：macOS 14 起
   NSApplicationActivateIgnoringOtherApps 已被系统忽略，调用方自己不是前台应用时
   activateWithOptions_ 会「返回 True 但什么也没发生」（本机实测，watchdog 正是这种后台进程），
   兜底要用 LaunchServices 的 `open -b`；锁屏时这些全都无效，只能走 ax_direct。
"""
from __future__ import annotations

import ctypes
import math
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import AppKit
import Quartz

from ..types import FocusReport, WindowInfo
from . import ax

AX_TIMEOUT = 0.5  # 列窗口时的单次 AX 超时：只为拿标题，不值得为它等 2 秒
AX_LIST_BUDGET_S = 3.0  # 一次 list_windows 花在 AX 上的总预算，超了就退化成用 CG 的名字

Rect = Tuple[int, int, int, int]

# kCGAnyInputEventType also changes for non-input WindowServer events (including
# starting the Python/AppKit helper). Only explicit human input types may yield
# desktop control. Our private events must be posted at the session entry;
# posting even private-source events at the HID entry resets these clocks.
_INPUT_EVENT_TYPES = (1, 2, 3, 4, 5, 6, 7, 10, 11, 12, 22, 25, 26, 27)
_CG: Optional[ctypes.CDLL] = None
_CG_TRIED = False


def _coregraphics() -> Optional[ctypes.CDLL]:
    global _CG, _CG_TRIED
    if _CG is None and not _CG_TRIED:
        _CG_TRIED = True
        try:
            lib = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            lib.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
            lib.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int32, ctypes.c_uint32]
            _CG = lib
        except Exception:
            _CG = None
    return _CG


# ---------------------------------------------------------------- 应用信息


def _running_apps() -> Dict[int, AppKit.NSRunningApplication]:
    out: Dict[int, AppKit.NSRunningApplication] = {}
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        out[int(app.processIdentifier())] = app
    return out


def app_info(pid: int) -> Tuple[str, str]:
    """pid → (本地化应用名, bundle id)；取不到时名字退回空串由调用方兜底。"""
    app = _running_apps().get(int(pid))
    if app is None:
        return ("", "")
    return (str(app.localizedName() or ""), str(app.bundleIdentifier() or ""))


def process_name(pid: int) -> str:
    return app_info(pid)[0]


def is_regular_app(pid: int) -> bool:
    app = _running_apps().get(int(pid))
    return bool(app) and int(app.activationPolicy()) == int(AppKit.NSApplicationActivationPolicyRegular)


def frontmost_pid() -> int:
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(app.processIdentifier()) if app else 0


# ---------------------------------------------------------------- 窗口枚举


def _cg_windows(visible_only: bool) -> List[Dict[str, Any]]:
    if visible_only:
        options = int(Quartz.kCGWindowListOptionOnScreenOnly) | int(Quartz.kCGWindowListExcludeDesktopElements)
    else:
        options = int(Quartz.kCGWindowListOptionAll)
    raw = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
    out: List[Dict[str, Any]] = []
    for entry in raw:
        bounds = entry.get("kCGWindowBounds") or {}
        rect = (
            int(round(float(bounds.get("X", 0)))),
            int(round(float(bounds.get("Y", 0)))),
            int(round(float(bounds.get("X", 0)) + float(bounds.get("Width", 0)))),
            int(round(float(bounds.get("Y", 0)) + float(bounds.get("Height", 0)))),
        )
        layer = int(entry.get("kCGWindowLayer", 0) or 0)
        alpha = float(entry.get("kCGWindowAlpha", 1.0) or 0.0)
        if layer != 0 or rect[2] <= rect[0] or rect[3] <= rect[1] or alpha <= 0.01:
            continue
        out.append(
            {
                "hwnd": int(entry.get("kCGWindowNumber", 0) or 0),
                "pid": int(entry.get("kCGWindowOwnerPID", 0) or 0),
                "owner": str(entry.get("kCGWindowOwnerName", "") or ""),
                "name": str(entry.get("kCGWindowName", "") or ""),
                "rect": rect,
                "onscreen": bool(entry.get("kCGWindowIsOnscreen", not visible_only)),
                "layer": layer,
                "alpha": alpha,
            }
        )
    return out


def _ax_windows_by_pid(pids: Sequence[int]) -> Dict[int, List[Dict[str, Any]]]:
    """给每个 pid 取一次 AX 窗口清单，整体受 AX_LIST_BUDGET_S 约束。

    某个 app 挂住时 AX 调用会走满超时，预算用尽后剩下的窗口直接用 CG 的名字，
    不能让一个僵尸应用把整轮扫描拖长十几秒。
    """
    out: Dict[int, List[Dict[str, Any]]] = {}
    if not ax.trusted():
        return out
    deadline = time.time() + AX_LIST_BUDGET_S
    for pid in pids:
        if time.time() > deadline:
            break
        items = ax.window_summaries(pid, timeout=AX_TIMEOUT, build_tree=False)
        if items:
            out[pid] = items
    return out


def _match_ax_window(rect: Sequence[int], items: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按「重叠面积 / 两者较大面积」挑同一个窗口，阈值 0.6。

    只用重叠面积会被大量外壳窗口误配（Chrome 最小化后那几张 1800x41 的
    缩略图 / 菜单栏影子会整片落在真窗口的矩形里），所以还要求两者面积接近。
    """
    area = max(1, (rect[2] - rect[0]) * (rect[3] - rect[1]))
    best, best_ratio = None, 0.0
    for info in items:
        other = info["rect"]
        if not other:
            continue
        other_area = max(1, (other[2] - other[0]) * (other[3] - other[1]))
        hit = _overlap(rect, other)
        if not hit:
            continue
        ratio = hit / float(max(area, other_area))
        if ratio > best_ratio:
            best, best_ratio = info, ratio
    return best if best_ratio >= 0.6 else None


def list_windows(visible_only: bool = True) -> List[WindowInfo]:
    """枚举用户可见的顶层窗口（layer 0、有面积、alpha > 0）。"""
    entries = _cg_windows(visible_only)
    ax_map = _ax_windows_by_pid(sorted({e["pid"] for e in entries}))
    names: Dict[int, Tuple[str, str]] = {}
    out: List[WindowInfo] = []
    for entry in entries:
        pid = entry["pid"]
        if pid not in names:
            names[pid] = app_info(pid)
        name, bundle = names[pid]
        title, minimized = entry["name"], not entry["onscreen"]
        info = _match_ax_window(entry["rect"], ax_map.get(pid, []))
        if info is not None:
            title = info["title"] or title
            minimized = info["minimized"]
        out.append(
            WindowInfo(
                hwnd=entry["hwnd"],
                title=title,
                pid=pid,
                process=name or entry["owner"],
                rect=entry["rect"],
                visible=entry["onscreen"],
                minimized=minimized,
                class_name=bundle,
            )
        )
    out.sort(key=lambda w: -w.width * w.height)
    return out


def _overlap(a: Sequence[int], b: Sequence[int]) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


def find_windows(
    process: str = "",
    title_re: str = "",
    min_size: int = 120,
    include_minimized: bool = True,
    visible_only: bool = True,
) -> List[WindowInfo]:
    """按进程名 / 标题正则筛窗口；非最小化、面积大的排前面（对齐 Windows 版语义）。"""
    pat_proc = re.compile(process, re.I) if process else None
    pat_title = re.compile(title_re, re.I) if title_re else None
    result: List[WindowInfo] = []
    for w in list_windows(visible_only=visible_only):
        if w.minimized:
            if not include_minimized:
                continue
        elif w.width < min_size or w.height < min_size:
            continue
        if pat_proc and not (pat_proc.search(w.process) or pat_proc.search(w.class_name)):
            continue
        if pat_title and not pat_title.search(w.title):
            continue
        result.append(w)
    result.sort(key=lambda w: (w.minimized, -w.width * w.height))
    return result


# ---------------------------------------------------------------- 前台控制


def _ax_window(window: WindowInfo) -> Optional[Any]:
    return ax.window_for(window.pid, window.rect, window.title, timeout=AX_TIMEOUT, build_tree=False)


def raise_window(window: WindowInfo) -> bool:
    """只做 AXRaise：在本应用内部提到最前，不抢系统前台。"""
    win = _ax_window(window)
    return ax.raise_window(win) if win is not None else False


def set_minimized(window: WindowInfo, flag: bool) -> bool:
    win = _ax_window(window)
    if win is None:
        return False
    return ax.set_minimized(win, flag)


def restore(window: WindowInfo) -> bool:
    """取消最小化并试着激活；返回窗口是否已经不在最小化态。"""
    win = _ax_window(window)
    if win is not None and bool(ax.get_attr(win, "AXMinimized", False, timeout=AX_TIMEOUT)):
        ax.set_minimized(win, False)
    _activate_pid(window.pid)
    time.sleep(0.2)
    win = _ax_window(window)
    if win is None:
        return False
    return not bool(ax.get_attr(win, "AXMinimized", False, timeout=AX_TIMEOUT))


def _activate_pid(pid: int) -> bool:
    """普通 activate。macOS 14 起 NSApplicationActivateIgnoringOtherApps 已被系统忽略，
    调用方自己不是前台应用时这一步会「返回 True 但什么都不发生」。"""
    app = _running_apps().get(int(pid))
    if app is None:
        return False
    try:
        app.unhide()
        return bool(app.activateWithOptions_(int(AppKit.NSApplicationActivateIgnoringOtherApps)))
    except Exception:
        return False


def _activate_via_launchservices(pid: int) -> bool:
    """后台常驻进程抢前台的唯一可行招：让 LaunchServices 去唤起它。

    实测本机的 python 进程不是前台应用，activateWithOptions_ / AXFrontmost 都被静默忽略，
    只有 `open -b <bundleid>` 能把目标应用真正提到前台（异步，要稍等）。
    不用 osascript：它要 Apple Events 授权，还会卡两分钟等一个弹框。
    """
    app = _running_apps().get(int(pid))
    if app is None:
        return False
    bundle = str(app.bundleIdentifier() or "")
    name = str(app.localizedName() or "")
    argv = ["/usr/bin/open", "-b", bundle] if bundle else (["/usr/bin/open", "-a", name] if name else None)
    if not argv:
        return False
    try:
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=6.0).returncode == 0
    except Exception:
        return False


def focus(window: WindowInfo, verify: bool = True, settle: float = 0.3) -> FocusReport:
    """抢前台并回读校验，逐级用招：activate → +AXRaise → 取消最小化 → open 唤起 → 最小化再还原。"""
    target = int(window.hwnd)
    if _running_apps().get(int(window.pid)) is None:
        return FocusReport(False, "no-such-pid", frontmost_pid(), target)

    def ok() -> bool:
        return is_foreground(window)

    def settled(strategy: str) -> Optional[FocusReport]:
        """等到前台真的换过来为止，换了就回报用的哪一招。"""
        for _ in range(8):
            if ok():
                return FocusReport(True, strategy, frontmost_window_id(), target)
            time.sleep(0.15)
        return None

    if ok():
        return FocusReport(True, "already", target, target)

    _activate_pid(window.pid)
    strategy = "activate"
    report = settled(strategy)
    if report:
        return report

    win = _ax_window(window)
    if win is not None:
        ax.raise_window(win)
        ax.frontmost(window.pid, True)
    strategy = "activate+axraise"
    time.sleep(settle)
    report = settled(strategy)
    if report:
        return report

    if window.minimized:
        restore(window)
        strategy = "unminimize"
        report = settled(strategy)
        if report:
            return report

    if _activate_via_launchservices(window.pid):
        strategy = "launchservices-open"
        report = settled(strategy)
        if report:
            return report
        win = _ax_window(window)
        if win is not None:
            ax.raise_window(win)

    if not verify:
        return FocusReport(True, strategy, frontmost_window_id(), target)

    # 最小化再还原：macOS 会把「允许设前台」的权限重新发给这个应用，
    # 别的程序死攥着前台时往往只有这招管用（Windows 版实测同样如此）
    win = _ax_window(window)
    if win is not None and ax.set_minimized(win, True):
        time.sleep(0.25)
        ax.set_minimized(win, False)
        _activate_pid(window.pid)
        _activate_via_launchservices(window.pid)
        strategy = "minimize-restore"
        report = settled(strategy)
        if report:
            return report

    return FocusReport(ok(), strategy, frontmost_window_id(), target)


def is_foreground(window: WindowInfo) -> bool:
    """前台应用 + （能判定时）前台窗口都对得上才算。FocusReport.foreground 就是这里回读的 id。"""
    if frontmost_pid() != int(window.pid):
        return False
    focused = ax.focused_window(window.pid)
    if focused is None:
        return True
    rect = ax.rect_of(focused, timeout=AX_TIMEOUT)
    if not rect:
        return True
    return _overlap(rect, window.rect) > 0


def frontmost_window_id() -> int:
    """当前真正在最前的用户窗口 id（CGWindowList 是从前往后排的）。

    整屏 / 独立 Space 的应用不在 layer 0，第一次筛不到就再不限层数扫一遍，
    否则 FocusReport.foreground 会误报 0。
    """
    pid = frontmost_pid()
    if not pid:
        return 0
    for entry in _cg_windows(True):
        if entry["pid"] == pid:
            return entry["hwnd"]
    raw = Quartz.CGWindowListCopyWindowInfo(
        int(Quartz.kCGWindowListOptionOnScreenOnly), Quartz.kCGNullWindowID
    ) or []
    for entry in raw:
        if int(entry.get("kCGWindowOwnerPID", 0) or 0) == pid:
            return int(entry.get("kCGWindowNumber", 0) or 0)
    return 0


# ---------------------------------------------------------------- 会话 / 显示器


def session_locked() -> bool:
    """锁屏判定：未锁时字典里**没有** CGSSessionScreenIsLocked 这个键，锁上时为 1。"""
    try:
        session = Quartz.CGSessionCopyCurrentDictionary()
    except Exception:
        return False
    if not session:
        return False
    try:
        return int(session.get("CGSSessionScreenIsLocked", 0) or 0) == 1
    except Exception:
        return False


def display_asleep() -> bool:
    """主显示器是否休眠（合盖 / 关屏）。这时截屏和键鼠投递都不可用。"""
    try:
        return bool(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()))
    except Exception:
        return False


def multi_display_bounds() -> List[Tuple[int, int, int, int, int]]:
    """所有活跃显示器：[(display_id, left, top, right, bottom)]，全局点坐标。"""
    err, ids, count = Quartz.CGGetActiveDisplayList(0, None, None)
    if err != 0 or not count:
        return []
    err, ids, _count = Quartz.CGGetActiveDisplayList(int(count), None, None)
    out: List[Tuple[int, int, int, int, int]] = []
    for did in ids or []:
        b = Quartz.CGDisplayBounds(int(did))
        out.append(
            (
                int(did),
                int(round(b.origin.x)),
                int(round(b.origin.y)),
                int(round(b.origin.x + b.size.width)),
                int(round(b.origin.y + b.size.height)),
            )
        )
    return out


def display_union() -> Rect:
    rects = multi_display_bounds()
    if not rects:
        b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return (int(b.origin.x), int(b.origin.y), int(round(b.origin.x + b.size.width)), int(round(b.origin.y + b.size.height)))
    return (
        min(r[1] for r in rects),
        min(r[2] for r in rects),
        max(r[3] for r in rects),
        max(r[4] for r in rects),
    )


def display_id_at(rect: Optional[Sequence[int]] = None) -> int:
    """包含某个区域（按中心点）的显示器 id，默认主显示器。"""
    main = int(Quartz.CGMainDisplayID())
    if not rect:
        return main
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    for cand in multi_display_bounds():
        if cand[1] <= cx < cand[3] and cand[2] <= cy < cand[4]:
            return cand[0]
    return main


def scale_at(rect: Optional[Sequence[int]] = None) -> float:
    """某个区域所在显示器的「像素 / 点」倍数（Retina 上是 2.0）。

    不能用 CGDisplayPixelsWide —— 它对 Retina 返回的也是点数（本机 1800），
    要拿 display mode 的 pixelWidth 除 bounds.width 才得到 2.0。
    """
    did = display_id_at(rect)
    try:
        points = float(Quartz.CGDisplayBounds(did).size.width)
        mode = Quartz.CGDisplayCopyDisplayMode(did)
        pixels = float(Quartz.CGDisplayModeGetPixelWidth(mode)) if mode else points
    except Exception:
        return 1.0
    return pixels / points if points else 1.0


# ---------------------------------------------------------------- 真人让路


def idle_seconds() -> float:
    """硬件键鼠事件距今秒数；读取失败或异常值按 0 秒处理。

    只检查按键、修饰键、鼠标移动/按下/抬起/拖动和滚轮。所有事件的计时
    还会被 Python/AppKit 启动及窗口系统事件重置，不能用来判断用户在操作。
    私有注入投递到 session 入口，避免重置 HID 计时；若投到 HID 入口仍会
    污染该计时。前台和目标身份仍另行核验，不跳过注入后的真人输入保护。
    """
    try:
        lib = _coregraphics()
        if lib is None:
            return 0.0
        elapsed = [float(lib.CGEventSourceSecondsSinceLastEventType(
            int(Quartz.kCGEventSourceStateHIDSystemState), kind)) for kind in _INPUT_EVENT_TYPES]
        if any(not math.isfinite(value) or value < 0 for value in elapsed):
            return 0.0
        return min(elapsed)
    except Exception:
        return 0.0


def user_active(within_s: float) -> bool:
    return idle_seconds() < float(within_s)


# ---------------------------------------------------------------- 自检


def _self_check(argv: Sequence[str]) -> int:
    import os

    print("== aiwatch.mac.winops 自检 ==  自身 pid=%d" % os.getpid())
    print("辅助功能: %s  锁屏: %s  主屏休眠: %s" % (ax.trusted(), session_locked(), display_asleep()))
    pid = frontmost_pid()
    print("前台: pid=%d %r  前台窗口 id=%d" % (pid, app_info(pid)[0], frontmost_window_id()))
    print("显示器: %s\n并集: %s  Retina 倍数: %s" % (multi_display_bounds(), display_union(), scale_at()))

    t0 = time.time()
    wins = list_windows(True)
    print("\nlist_windows(visible_only=True) → %d 个，%.0fms" % (len(wins), (time.time() - t0) * 1000))
    for w in wins[:12]:
        print("   %s | bundle=%s visible=%s minimized=%s" % (w, w.class_name, w.visible, w.minimized))
    t0 = time.time()
    all_wins = list_windows(False)
    print("list_windows(visible_only=False) → %d 个（%.0fms），其中最小化 %d" % (len(all_wins), (time.time() - t0) * 1000, sum(1 for w in all_wins if w.minimized)))
    for w in all_wins:
        if w.minimized:
            print("   最小化:", w)
    print("\nfind_windows(process='ChatGPT|AutoClaw|OpenClaw|Hermes|Kimi|Doubao') →")
    for w in find_windows(process="ChatGPT|AutoClaw|OpenClaw|Hermes|Kimi|Doubao"):
        print("   ", w)
    print("\n真人空闲 %.1fs → user_active(5)=%s" % (idle_seconds(), user_active(5)))

    def pick(pattern: str) -> Optional[WindowInfo]:
        for w in all_wins:
            if re.search(pattern, w.process, re.I) or re.search(pattern, w.title, re.I):
                return w
        return None

    if "--focus" in argv:
        idx = argv.index("--focus")
        target = pick(argv[idx + 1] if len(argv) > idx + 1 else "Finder|访达")
        if target is None:
            print("--focus 没匹配到窗口")
        else:
            before = frontmost_pid()
            rep = focus(target)
            print("--focus %s\n  %s\n  前台 pid %d→%d，is_foreground=%s"
                  % (target, rep, before, frontmost_pid(), is_foreground(target)))
            _activate_pid(before)
            time.sleep(0.3)
            if frontmost_pid() != before:
                _activate_via_launchservices(before)
                time.sleep(0.5)
            print("  归还原前台 pid=%d %r → 现在 %d %r"
                  % (before, app_info(before)[0], frontmost_pid(), app_info(frontmost_pid())[0]))
    if "--minimize" in argv:
        idx = argv.index("--minimize")
        target = pick(argv[idx + 1] if len(argv) > idx + 1 else "Finder|访达")
        if target is None:
            print("--minimize 没匹配到窗口")
        else:
            print("--minimize %s" % target)
            print("  置为最小化:", set_minimized(target, True))
            time.sleep(0.5)
            print("  最小化后 onscreen 列表里还有它吗:", any(w.hwnd == target.hwnd for w in list_windows(True)))
            print("  全量列表里的状态:", pick(target.title))
            print("  restore=%s → %s" % (restore(target), pick(target.title)))
    if not any(a in argv for a in ("--focus", "--minimize")):
        print("\n（写操作要显式开：--focus <正则> / --minimize <正则>，默认只读）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_self_check(sys.argv[1:]))
