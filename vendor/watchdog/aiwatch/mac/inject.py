"""macOS 事件注入与剪贴板（CGEvent + NSPasteboard）。

坐标约定：click_at(x, y) 收的是**全局坐标、左上角原点、点**（与 Backend 契约一致）。
CGEvent 的鼠标坐标系正好就是这个（Quartz 内部按主屏翻转），不需要换算。

两条与 Windows 版不同的关键经验（都是实测出来的）：
  1. 中文只能用 CGEventKeyboardSetUnicodeString：它把 UTF-16 码元直接塞进事件，
     绕过输入法；用 keycode 打字在中文输入法下会变成拼音串。
     而且在 pyobjc 上只有自由函数 CGEventKeyboardSetUnicodeString(ev, 码元数, str) 可用：
     事件对象上没有同名方法，长度参数是 UTF-16 码元数而不是 len(str)，
     传 bytes/list[int] 会被拒（详见 _send_chunk 的注释）。
  2. 发送键是 Return（keycode 36）而不是 Windows 的 VK_RETURN。
     豆包等应用同时绑了 ⌘Enter，但回车是通用约定。

本模块只负责执行，不判锁屏：锁屏时系统会静默丢弃 CGEvent 投递，
「现在能不能注入」由 mac/backend.py 用 session_locked() 决定，避免两边逻辑打架。
"""
from __future__ import annotations

import os
import time
from typing import Any, List, Optional, Sequence, Tuple

Quartz: Any = None
AppKit: Any = None
Foundation: Any = None


def _load() -> bool:
    global Quartz, AppKit, Foundation
    if Quartz is not None:
        return True
    try:
        import AppKit as _a
        import Foundation as _f
        import Quartz as _q
    except Exception:
        return False
    Quartz, AppKit, Foundation = _q, _a, _f
    return True


# 虚拟键码（macOS 用 kVK_*，与 Windows VK_* 完全不同）
K_RETURN = 36
K_A = 0
K_V = 9
K_C = 8
K_DELETE = 51  # Forward Delete（⌘A + Delete 清空选择框）
K_SHIFT = 56   # 单独按下不产生字符、不触发快捷键，自检拿它探路最安全

_MAX_CHUNK = 20  # 单次 UnicodeString 承载的码元上限，过长会被系统截断

# user_active 关心的事件类型：真人按键、动鼠标、点触摸板都算
_ACTIVITY_EVENTS = (
    "kCGEventKeyDown", "kCGEventKeyUp", "kCGEventLeftMouseDown",
    "kCGEventRightMouseDown", "kCGEventOtherMouseDown", "kCGEventMouseMoved",
)


_SRC: Any = None


def src() -> Any:
    """所有合成事件都必须挂在一个私有事件源上。

    本机实测（TextEdit，⌘S 后读盘为证）：源传 None、kCGEventSourceStateCombinedSessionState、
    kCGEventSourceStateHIDSystemState 时，**不带修饰键的按键会被系统整条丢弃**
    （空格/回车/字母全进不去，但 ⌘V/⌘S 这类组合键照旧生效）；
    换成 kCGEventSourceStatePrivate 后普通按键立刻落进文档。
    """
    global _SRC
    if _SRC is None:
        _load()
        _SRC = Quartz.CGEventSourceCreate(
            getattr(Quartz, "kCGEventSourceStatePrivate", -1)
        )
    return _SRC


def post(event: Any) -> None:
    """把事件投递到硬件层（HID 状态级，对前台应用有效）。"""
    _load()
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def post_to_pid(event: Any, pid: int) -> None:
    """定向投递给某个进程：真人不在键盘前（显示器休眠、屏保、快速切换后的后台会话）时
    CGEventPost 会被丢掉，PostToPid 仍能送到目标 App。真锁屏（loginwindow 持有会话）
    两条路都进不去，所以能不能注入仍由 backend 的 session_locked() 说了算。"""
    _load()
    Quartz.CGEventPostToPid(int(pid), event)


def press_key(keycode: int, flags: int = 0, hold: float = 0.03) -> None:
    _load()
    s = src()
    down = Quartz.CGEventCreateKeyboardEvent(s, keycode, True)
    up = Quartz.CGEventCreateKeyboardEvent(s, keycode, False)
    if flags:
        Quartz.CGEventSetFlags(down, flags)
        Quartz.CGEventSetFlags(up, flags)
    post(down)
    time.sleep(hold)
    post(up)


def hotkey(keycode: int, hold: float = 0.03) -> None:
    """按下并释放 ⌘+keycode（粘贴 ⌘V=9、全选 ⌘A=0、复制 ⌘C=8）。"""
    _load()
    press_key(keycode, Quartz.kCGEventFlagMaskCommand, hold)
    time.sleep(0.02)


def press_enter() -> None:
    press_key(K_RETURN)


def press_cmd_enter() -> None:
    """⌘+回车：组合键是本机实测**唯一可靠**的合成按键通路（裸按键会被输入源吞掉）。"""
    press_key(K_RETURN, Quartz.kCGEventFlagMaskCommand)


def type_text(text: str, raw: bool = False) -> bool:
    """把文字送进当前焦点。默认走剪贴板。

    实测结论（本机 TextEdit，⌘S 后读盘核对）：
      - 不带修饰键的合成按键会被当前输入源重新映射或直接丢弃 —— keycode 6 打出来是
        '而不是 z，裸回车 key code 36 完全不落地；
      - 挂 kCGEventSourceStatePrivate 事件源后按键才进得去，但字符仍被输入源改写；
      - 组合键（⌘V / ⌘S）始终可靠。
    所以文本默认走剪贴板粘贴，和 Windows 版「中文用键盘模拟不可靠、剪贴板最稳」是同一条坑。
    raw=True 强制走 UnicodeString 直发（应用不接受粘贴、或输入源是 ABC 时才值得试）。
    """
    if not text or not _load():
        return False
    if raw:
        for chunk in _chunks(text, _MAX_CHUNK):
            if not _send_chunk(chunk):
                return False
            time.sleep(0.012)
        return True
    return paste_text(text, restore_clipboard=True)


def _chunks(text: str, limit: int) -> List[str]:
    """按码点切段（绝不把代理对劈开），每段最多 limit 个 UTF-16 码元。"""
    out: List[str] = []
    cur: List[str] = []
    used = 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if cur and used + width > limit:
            out.append("".join(cur))
            cur, used = [], 0
        cur.append(ch)
        used += width
    if cur:
        out.append("".join(cur))
    return out


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _send_chunk(chunk: str) -> bool:
    pair = _unicode_events(chunk)
    if pair is None:
        return False
    post(pair[0])
    time.sleep(0.008)
    post(pair[1])
    return True


def _unicode_events(chunk: str) -> Optional[Tuple[Any, Any]]:
    """造一对挂着 UnicodeString 的 keyDown/keyUp，**不投递**，并把事件里的码元数读回来核对。

    pyobjc 上这条路的坑（本机实测，写错的表现为「静默不打字」）：
      - 只有自由函数 CGEventKeyboardSetUnicodeString(ev, 长度, str) 可用：事件对象上
        没有同名方法，CGEventCreateKeyboardEventUnicodeString 这个符号也不存在。
      - 长度必须是 **UTF-16 码元数**，不是 len(str)：传码点数遇到 emoji 会抛
        UnicodeDecodeError（代理对只给了一半）。
      - 传 bytes 或 list[int] 都报 "Expecting unicode string"，只有 str 能过。
    """
    n = _utf16_len(chunk)
    pair: List[Any] = []
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(src(), 0, down)
        if ev is None:
            return None
        Quartz.CGEventKeyboardSetUnicodeString(ev, n, chunk)
        got = _event_units(ev)
        if 0 <= got != n:
            return None
        pair.append(ev)
    return pair[0], pair[1]


def _event_units(event: Any) -> int:
    try:
        actual, _buf = Quartz.CGEventKeyboardGetUnicodeString(event, 64, None, None)
        return int(actual)
    except Exception:
        return -1  # 这个 getter 桥不上就不校验，别因此判整条链路失败


def click_at(x: int, y: int, settle: float = 0.15) -> None:
    """全局坐标单击：先游标挪到位，再 move -> down -> up。

    只 post 鼠标事件不会移动真实游标，部分应用（尤其 Chromium 系）按真实游标位置做
    命中测试，所以必须先 CGWarpMouseCursorPosition。
    """
    if not _load():
        return
    x, y = int(x), int(y)
    s = src()
    Quartz.CGWarpMouseCursorPosition((x, y))
    time.sleep(0.03)
    move = Quartz.CGEventCreateMouseEvent(s, Quartz.kCGEventMouseMoved, (x, y), 0)
    post(move)
    time.sleep(0.02)
    down = Quartz.CGEventCreateMouseEvent(s, Quartz.kCGEventLeftMouseDown, (x, y),
                                          Quartz.kCGMouseButtonLeft)
    up = Quartz.CGEventCreateMouseEvent(s, Quartz.kCGEventLeftMouseUp, (x, y),
                                        Quartz.kCGMouseButtonLeft)
    post(down)
    time.sleep(0.05)
    post(up)
    if settle:
        time.sleep(settle)


# ---------------------------------------------------------------- 剪贴板
def get_clipboard_text() -> str:
    if not _load():
        return ""
    pb = AppKit.NSPasteboard.generalPasteboard()
    s = pb.stringForType_(AppKit.NSPasteboardTypeString)
    return str(s) if s else ""


def set_clipboard_text(text: str) -> bool:
    if not _load():
        return False
    s = Foundation.NSString.stringWithString_(str(text))
    return _write_objects([s])


def paste_text(text: str, restore_clipboard: bool = True) -> bool:
    """写剪贴板 -> ⌘V -> （可选）还原原内容。等价于 winio.paste_text。"""
    if not _load():
        return False
    saved = _backup_pasteboard() if restore_clipboard else ()
    if not set_clipboard_text(text):
        return False
    written_at = int(_pb().changeCount())
    time.sleep(0.04)
    hotkey(K_V)
    if restore_clipboard:
        time.sleep(0.12)
        _restore_pasteboard(saved, written_at)
    return True


def select_all_and_clear() -> None:
    """⌘A 再 Delete：Windows 版靠 EditableText.Clear，macOS 的 AXValue 不一定可写，
    所以清空一律走键盘。"""
    _load()
    hotkey(K_A)
    time.sleep(0.04)
    press_key(K_DELETE)


def copy_selection() -> str:
    """⌘C 后回读剪贴板。发图之后用它可以确认输入框到底收没收到。"""
    if not _load():
        return ""
    saved = _backup_pasteboard()
    written_at = int(_pb().changeCount())
    hotkey(K_C)
    time.sleep(0.10)
    text = get_clipboard_text()
    _restore_pasteboard(saved, written_at)
    return text


def paste_image(path: str, restore_clipboard: bool = True) -> bool:
    """把图片写进剪贴板再 ⌘V：图像类型 + 文件 URL 一起给，
    因为各家 App 认的不一样（ChatGPT/豆包吃 TIFF/PNG 图像，Finder 类应用只认文件 URL）。
    返回 False 只表示「粘贴没报错」这一层失败；有没有真进去要由 backend 回读校验。
    """
    img = load_image(path or "")
    if img is None:
        return False
    abs_path = os.path.abspath(path)
    saved = _backup_pasteboard() if restore_clipboard else ()
    url = Foundation.NSURL.fileURLWithPath_(abs_path)
    if not _write_objects([img, url]):
        return False
    written_at = int(_pb().changeCount())
    time.sleep(0.04)
    hotkey(K_V)
    if restore_clipboard:
        time.sleep(0.12)
        _restore_pasteboard(saved, written_at)
    return True


def load_image(path: str) -> Any:
    """载入图片供剪贴板使用。

    注意：本 pyobjc 桥没有暴露 NSImage 的类工厂 imageWithContentsOfFile:
    （dir() 里没有， hasattr 直接 False），只能走 alloc/init 那条路。
    """
    if not _load() or not path:
        return None
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return None
    try:
        img = AppKit.NSImage.alloc().initWithContentsOfFile_(abs_path)
    except Exception:
        return None
    return img if img is not None else None


def _write_objects(items: Sequence[Any]) -> bool:
    """写剪贴板。**必须先 clearContents()**：本机实测不先抢下所有权
    （别的 App 还占着 pasteboard 时）writeObjects_ 直接返回 False，
    而声明式写法 declareTypes_owner_ 又会把富类型丢掉，所以取这个组合。"""
    pb = _pb()
    arr = Foundation.NSArray.alloc().initWithArray_(list(items))
    if not pb.clearContents():
        return False
    return bool(pb.writeObjects_(arr))


def _pb() -> Any:
    return AppKit.NSPasteboard.generalPasteboard()


def _backup_pasteboard() -> Tuple[Tuple[Any, Any], ...]:
    """按类型备份当前剪贴板（writeObjects 会整块替换，纯文本备份会把图片丢掉）。

    用 pasteboard 级的 dataForType_ 而不是 pasteboardItems()[0] 的：一次 writeObjects 可以
    落下好几个 item，只读第一个会拿到**上一条**内容（实测还原后翻出几分钟前的旧文本）。
    """
    pb = _pb()
    out: List[Tuple[Any, Any]] = []
    for t in (AppKit.NSPasteboardTypeString, AppKit.NSPasteboardTypeTIFF,
              AppKit.NSPasteboardTypeFileURL):
        try:
            obj = pb.dataForType_(t)
        except Exception:
            obj = None
        if obj is not None:
            out.append((t, obj))
    return tuple(out)


def _restore_pasteboard(saved: Sequence[Tuple[Any, Any]], after_change_count: int = -1) -> bool:
    """还原备份。

    after_change_count 是「我们写入剪贴板那一刻」的 changeCount：现在值不一样就说明
    粘贴目标或别的进程又写过剪贴板，这时不硬恢复，免得把用户刚复制的东西盖掉。
    """
    if not saved:
        return False
    pb = _pb()
    if after_change_count >= 0 and int(pb.changeCount()) != int(after_change_count):
        return False
    for t, obj in saved:
        try:
            pb.declareTypes_owner_([t], None)
            pb.setData_forType_(obj, t)
        except Exception:
            pass
    return True


def user_active(within_s: float) -> bool:
    """最近 within_s 秒内真人的手是否在键鼠上动过（与 Windows 版同语义）。

    worker 靠它让路：人在敲键盘，程序就闭嘴，否则会把用户的输入搅乱。
    """
    if not _load():
        return False
    best: Optional[float] = None
    for name in _ACTIVITY_EVENTS:
        et = getattr(Quartz, name, None)
        if et is None:
            continue
        try:
            idle = Quartz.CGEventSourceSecondsSinceLastEventType(
                Quartz.kCGEventSourceStateHIDSystemState, et)
        except Exception:
            continue
        idle = float(idle)
        if idle >= 1e12:  # CGFLOAT_MAX：本会话还没发生过该类事件
            continue
        if best is None or idle < best:
            best = idle
    if best is None:
        return False
    return best < float(within_s)


# ---------------------------------------------------------------- 自检
def _frontmost_bundle() -> str:
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return str(app.bundleIdentifier() or "") if app else ""


def _mouse_loc() -> Tuple[int, int]:
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return int(p.x), int(p.y)


def _idle_of(event_name: str) -> float:
    """距上一次该类事件进入 HID 事件流过了多少秒。"""
    et = getattr(Quartz, event_name, None)
    if et is None:
        return -1.0
    return float(Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateHIDSystemState, et))


def _app(bundle_id: str) -> Any:
    for a in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if str(a.bundleIdentifier() or "") == bundle_id:
            return a
    return None


def _selfcheck(live_bundle: str = "") -> int:
    """自检口径：证明事件**进了系统事件流**、字符串**真挂上了事件**，且全程不打扰用户。

    默认不往任何 App 投可见字符。试过拿 TextEdit 当靶子做端到端回读（能读回
    「继续生成 abc😀」，证明 type_text/press_enter/hotkey 都有效），但反复跑会给用户攒出
    一堆带「要不要保存」面板的残留窗口，所以改成：惰性验证 + 显式 --live <bundleId> 实投。
    「应用到底收没收到」这件事归 probe.py 对着真实聊天窗口测。
    """
    if not _load():
        print("pyobjc 未安装，跳过")
        return 2
    import os

    fails: List[str] = []

    print("[剪贴板]")
    print("  当前内容 %r（前 20 字）" % get_clipboard_text()[:20])
    print("  写中文:", set_clipboard_text("继续生成"))
    got = get_clipboard_text()
    print("  读回:  %r %s" % (got, "OK" if got == "继续生成" else "FAIL"))
    if got != "继续生成":
        fails.append("set/get_clipboard_text")
    print("  paste_text:", paste_text("第二段", restore_clipboard=True),
          "-> 还原后 %r（应为 '继续生成'）" % get_clipboard_text())
    if get_clipboard_text() != "继续生成":
        fails.append("paste_text 未还原剪贴板")

    print("\n[键盘：默认只做惰性验证，绝不把字符打进用户当前的输入框]")
    idle0 = _idle_of("kCGEventKeyDown")
    press_key(K_SHIFT)
    press_key(K_SHIFT)
    idle1 = _idle_of("kCGEventKeyDown")
    streamed = 0.0 <= idle1 < 1.0
    print("  投两下 Shift（不出字、不触发快捷键）：KeyDown idle %.1fs -> %.1fs  %s"
          % (idle0, idle1, "OK 事件进了 HID 流" if streamed else "FAIL 事件被丢掉（八成没给辅助功能权限）"))
    if not streamed:
        fails.append("键盘投递")
    # 内容核对：把要发的字符串真装进事件，再从事件里读回码元数（不投递，所以不打扰任何窗口）
    for probe in ("继续生成 abc\U0001F600", "额度已用完，请明天再来", "a" * 60):
        parts = _chunks(probe, _MAX_CHUNK)
        ok = all(_unicode_events(c) is not None for c in parts)
        print("  %-14s 分 %d 段、共 %2d 码元 %s"
              % (probe[:12], len(parts), sum(_utf16_len(c) for c in parts), "OK" if ok else "FAIL"))
        if not ok:
            fails.append("UnicodeString 没挂上")
    if len(_chunks("a" * 45, 20)) != 3:
        fails.append("_chunks 分段数不对")
    lone = "\U0001F600" * 15  # 代理对不能被劈开
    if any(_utf16_len(c) % 2 or len(c) > 20 for c in _chunks(lone, 20)):
        fails.append("_chunks 劈开了代理对")
    else:
        print("  emoji 串分段不劈代理对 OK（每段字符数 %s）" % [len(c) for c in _chunks(lone, 20)])

    print("\n[鼠标]")
    before = _mouse_loc()
    idle_m0 = _idle_of("kCGEventMouseMoved")
    click_at(before[0] + 40, before[1] + 25, settle=0.2)
    after = _mouse_loc()
    idle_m1 = _idle_of("kCGEventMouseMoved")
    moved = after == (before[0] + 40, before[1] + 25) and 0.0 <= idle_m1 <= max(
        1.0, idle_m0 if idle_m0 >= 0 else 1.0)
    print("  click_at: 指针 %s -> %s，MouseMoved idle %.1fs -> %.1fs  %s"
          % (before, after, idle_m0, idle_m1, "OK" if moved else "FAIL"))
    if not moved:
        fails.append("click_at")

    print("\n[图片写剪贴板]")
    png = os.path.join("reports", "shot-SilverKitten.png")
    if os.path.isfile(png):
        img = load_image(png)
        url = Foundation.NSURL.fileURLWithPath_(os.path.abspath(png))
        print("  load_image:", img is not None, " writeObjects(图像+fileURL):",
              _write_objects([img, url]))
        types = [str(t) for t in _pb().types()]
        has_img = any("tiff" in t or "png" in t or "image" in t for t in types)
        has_url = any("file-url" in t for t in types)
        print("  类型 %s %s" % (types[:4], "OK" if has_img and has_url else "FAIL 图像/文件URL 缺一"))
        if not (has_img and has_url):
            fails.append("paste_image 类型")
        print("  paste_image(不存在的文件):", paste_image("/nonexistent/x.png"), "（应为 False）")
    else:
        print("  跳过：找不到 %s" % png)

    print("\n[真人活跃度]")
    for w in (0.5, 2.0, 5.0):
        print("  user_active(within=%ss) = %s" % (w, user_active(w)))
    print("  刚才自检投过 Shift，所以这几项多半是 True；真人动键鼠也会翻 True")

    if live_bundle:
        print("\n[实投到 %s]：真的会打字并回车，确认这个目标经得起折腾再跑" % live_bundle)
        ra = _app(live_bundle)
        if ra is None:
            print("  FAIL 目标应用没在运行")
            fails.append("live 目标没运行")
        else:
            ra.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
            time.sleep(0.8)
            print("  前台现在是: %s" % (_frontmost_bundle() or "?"))
            select_all_and_clear()
            typed = type_text("继续生成 abc\U0001F600")
            press_enter()
            print("  type_text=%s：请目视确认目标里出现了「继续生成 abc😀」并且被回车提交" % typed)
            if not typed:
                fails.append("live type_text")

    print("SELF-CHECK", "OK" if not fails else "FAIL %s" % fails)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    live = ""
    if args and args[0] == "--live":
        live = args[1] if len(args) > 1 else "com.apple.TextEdit"
    raise SystemExit(_selfcheck(live))
