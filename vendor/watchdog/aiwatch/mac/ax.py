"""macOS 辅助功能（AXUIElement）封装：应用句柄、子树遍历读文本、赋值 / 按下。

产出语义对齐 Windows 版 read_uia：
  text         可见文本，按 y 再 x 排序，一行一条
  input_boxes  AXTextArea / AXTextField + Electron 网页区里的「文本输入区」
  buttons      AXButton 等真实按钮，以及「有名字 + 能被 AXPress」的网页元素
  每个 ElementRef.ref 都是原生 AXUIElement，可原样回传给 set_value / press / focus
  ElementRef.score 是置信度：真实按钮/输入框 1.0，可点的网页容器 0.5，
  可能是只读文档区的输入框 0.7；find_send_button 返回的是「发送键得分」，
  低于 MIN_SEND_SCORE 一律不发。
  拿不到坐标的元素 rect = NO_GEOMETRY 且 enabled=False（= 不可点击，只能走 AXPress/AXValue），
  永远不会是 (0,0,0,0) 那种会被误当成屏幕左上角的假坐标。
  遍历被预算/节点上限截断时 ReadResult.truncated = True，半截文本不能拿去刷新静默指纹。

坐标：AXPosition / AXSize 与 CGWindowList 的 kCGWindowBounds 同为「全局点、左上角原点」，
实测 ChatGPT 窗口两边数值完全一致（275,103,1255,760），所以这里不做任何换算。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import ApplicationServices as AX

from ..types import ElementRef, ReadResult

TIMEOUT = 2.0  # 单次 AX 消息超时：某个 app 挂掉时宁可读不到，也不能拖死 worker
FLAG_TIMEOUT = 0.6  # 建树开关用短超时（对无响应的应用不能白等）
# 一次子树遍历的墙钟预算，**从拿到 root 之后**才起算（取 root 本身要 0.3~1s，
# 从 t0 起算等于把建树的钱也算进遍历里）。实测 ChatGPT / AutoClaw 满树 1.3~2.4s，
# 流式输出时更久：预算 2.0s 会把静止窗口读成半截，指纹每轮都变 → 永远判不出卡顿。
READ_BUDGET_S = 5.0
SETTLE_S = 0.12  # 赋值后等 React 重渲染再回读
VISIT_FACTOR = 8  # 访问节点上限 = max_elements × 此倍数（见 read_window_tree）
MIN_VISITS = 8000  # 侧边栏噪声节点远比正文多，下限给足，否则又被截断

# 窗口挑选的几何门槛：CG rect 与 AX 窗口 rect 实测数值一致，所以重叠不足就是「不是同一个窗口」，
# 宁可不读也不能拿别的窗口读 —— 半截/错窗口的读数会让点击与粘贴落在错地方（台账 #5）。
MIN_WINDOW_OVERLAP = 0.80
# 元素中心允许落在窗口 rect 外多远一点（Chromium 常把控件坐标写成压边 1~2pt）
CLIP_TOL = 4
# 发送键最低置信度：低于这个分宁可不发。误点侧边栏一项就会把会话切走（台账 #17）。
MIN_SEND_SCORE = 40.0
# 元素拿不到坐标时用的占位 rect：**不是** (0,0,0,0)（那会被当成屏幕左上角真的去点）。
# 面积为 0、中心落在所有显示器之外，同时把 enabled 置 False 明确标记「不可点击」（台账 #26）。
NO_GEOMETRY = (-999999, -999999, -999999, -999999)


def has_geometry(box) -> bool:
    """这个 rect 能不能拿去做点击 / 粘贴落点：面积为 0 或就是无坐标占位都算不能。"""
    if not box:
        return False
    return _area(tuple(box)) > 0 and tuple(box) != NO_GEOMETRY


def clickable(ref) -> bool:
    """ElementRef 是否可作为坐标落点（无坐标元素只能走 AXPress / AXValue，不能点击）。"""
    return bool(getattr(ref, "enabled", True)) and has_geometry(getattr(ref, "rect", None))


AX_TREE_FLAGS = ("AXManualAccessibility", "AXEnhancedUserInterface")

INPUT_ROLES = {"AXTextArea", "AXTextField", "AXSearchField"}
# Chromium 网页输入框的 role 有时是 AXGroup，只能靠（本地化的）role description 认
INPUT_DESC_KEYS = ("文本编辑区", "文本输入区", "文字输入区", "编辑区", "输入区", "text entry", "text area", "text field")
STRICT_BUTTON_ROLES = {"AXButton", "AXMenuButton", "AXPopUpButton"}
PSEUDO_BUTTON_ROLES = {"AXGroup", "AXImage", "AXUnknown"}
# 这几个角色挂在窗口子树里，展开它等于把整棵应用树 / 菜单栏重复卷进来
SKIP_ROLES = {"AXApplication", "AXMenuBar", "AXMenuExtra"}
MAX_DEPTH = 64
SEND_TITLES = ("发送", "send", "提交", "提问", "发出", "enter")

_ERROR_NAMES = {
    0: "ok",
    -25200: "failure",
    -25201: "illegal-argument",
    -25202: "invalid-element",
    -25203: "invalid-element-observer",
    -25204: "cannot-complete(超时)",
    -25205: "attribute-unsupported",
    -25206: "action-unsupported",
    -25207: "notification-unsupported",
    -25208: "not-implemented",
    -25209: "notification-already-registered",
    -25210: "notification-not-registered",
    -25211: "api-disabled(该应用没开无障碍)",
    -25212: "no-value",
    -25213: "parameterized-attribute-unsupported",
    -25214: "not-enough-precision",
}


def err_name(code: int) -> str:
    return _ERROR_NAMES.get(int(code), "err-%s" % int(code))


_INVISIBLE = dict.fromkeys(
    list(range(0x200B, 0x2010)) + list(range(0x2060, 0x2070)) + [0xFEFF, 0x00AD]
)


def _clean(s: Any) -> str:
    if not isinstance(s, str):
        return ""
    # Chromium \u4f1a\u5728\u6807\u7b7e\u9875\u6807\u9898\u4e4b\u7c7b\u7684\u5185\u5bb9\u91cc\u585e\u6ee1\u96f6\u5bbd\u65b9\u5411\u63a7\u5236\u7b26\uff0c\u4e0d\u6e05\u6389\u4f1a\u628a text \u6c61\u67d3\u6210\u4e71\u7801
    return s.translate(_INVISIBLE).strip()


def _canonical(s: str) -> str:
    """去空白 + 形近字归一。与 rules 侧同一思路，但不 import sensor（它绑死了 Windows）。"""
    out = "".join((s or "").split()).lower()
    for a, b in (("i", "l"), ("1", "l"), ("|", "l"), ("!", "l")):
        out = out.replace(a, b)
    return out


def _area(rect: Sequence[int]) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _confidence(kind: str, r: str, desc: str) -> float:
    """给上层当置信度：伪按钮 0.5；像聊天输入框 1.0；可能是只读文档区 0.7。"""
    if kind == "pseudo":
        return 0.5
    if kind == "input":
        if r in ("AXTextField", "AXSearchField") or "输入" in desc:
            return 1.0
        return 0.7
    return 1.0


def _intersect(a: Sequence[int], b: Sequence[int]) -> Optional[Tuple[int, int, int, int]]:
    l = max(a[0], b[0])
    t = max(a[1], b[1])
    r = min(a[2], b[2])
    bot = min(a[3], b[3])
    return (l, t, r, bot) if r > l and bot > t else None


def _center_in(box: Sequence[int], clip: Sequence[int], tol: int = CLIP_TOL) -> bool:
    """元素中心落在 clip 里就算属于这个窗口（压着边界、探出去一点点的控件仍然点得到）。"""
    if not box or box == NO_GEOMETRY:
        return True
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    return (
        clip[0] - tol <= cx <= clip[2] + tol and clip[1] - tol <= cy <= clip[3] + tol
    )


# ---------------------------------------------------------------- 权限 / 句柄


def trusted() -> bool:
    """辅助功能是否已授权。**只问不弹框**（弹框版会打扰用户，禁止在本项目使用）。"""
    try:
        return bool(AX.AXIsProcessTrusted())
    except Exception:
        return False


def _set_timeout(element: Any, seconds: float = TIMEOUT) -> None:
    try:
        AX.AXUIElementSetMessagingTimeout(element, float(seconds))
    except Exception:
        pass


def unwrap(target: Any) -> Optional[Any]:
    """ElementRef → 原生 AXUIElement；裸 AXUIElement 原样返回。"""
    if target is None:
        return None
    ref = getattr(target, "ref", None)
    if ref is not None:
        return ref
    if "AXUIElement" in type(target).__name__:
        return target
    return None


def enable_ax_tree(target: Any, timeout: float = FLAG_TIMEOUT) -> Dict[str, str]:
    """强制 Chromium / Electron 构建无障碍树，返回每个开关的错误码名。

    不设置时子树是空的（对齐 Windows 版「坑 6」）。Chromium 在无障碍客户端断开后
    会把树拆掉，所以这个开关必须**每轮读取前重按一次**，不能只在启动时设一次。
    """
    raw = unwrap(target)
    out: Dict[str, str] = {}
    if raw is None:
        return {"element": "无效句柄"}
    _set_timeout(raw, timeout)
    for flag in AX_TREE_FLAGS:
        try:
            out[flag] = err_name(int(AX.AXUIElementSetAttributeValue(raw, flag, True)))
        except Exception as exc:
            out[flag] = type(exc).__name__
    return out


def app_element(pid: int, timeout: float = TIMEOUT, build_tree: bool = True) -> Optional[Any]:
    """取进程的 AX 应用句柄，默认顺手把无障碍树叫起来。"""
    if not trusted():
        return None
    try:
        app = AX.AXUIElementCreateApplication(int(pid))
    except Exception:
        return None
    _set_timeout(app, timeout)
    if build_tree:
        enable_ax_tree(app, FLAG_TIMEOUT)
        _set_timeout(app, timeout)
    return app


def tune(element: Any, timeout: float = TIMEOUT) -> Optional[Any]:
    """给外部传回来的裸元素补超时（可能是别的轮次创建的句柄）。"""
    raw = unwrap(element)
    if raw is not None:
        _set_timeout(raw, timeout)
    return raw


# ---------------------------------------------------------------- 属性读写


def get(element: Any, name: str, timeout: float = TIMEOUT) -> Tuple[int, Any]:
    """一次属性读取，返回 (错误码, 值)。属性不存在时 pyobjc 会抛异常，统一收敛。"""
    raw = unwrap(element)
    if raw is None:
        return -25201, None
    _set_timeout(raw, timeout)
    try:
        err, value = AX.AXUIElementCopyAttributeValue(raw, name, None)
    except Exception:
        return -25200, None
    return int(err), value


def get_attr(element: Any, name: str, default: Any = None, timeout: float = TIMEOUT) -> Any:
    err, value = get(element, name, timeout)
    return value if err == 0 else default


def set_attr(element: Any, name: str, value: Any, timeout: float = TIMEOUT) -> int:
    raw = unwrap(element)
    if raw is None:
        return -25201
    _set_timeout(raw, timeout)
    try:
        return int(AX.AXUIElementSetAttributeValue(raw, name, value))
    except Exception:
        return -25200


def attribute_names(element: Any) -> List[str]:
    raw = unwrap(element)
    if raw is None:
        return []
    _set_timeout(raw)
    try:
        err, names = AX.AXUIElementCopyAttributeNames(raw, None)
    except Exception:
        return []
    return [str(n) for n in names] if err == 0 else []


def action_names(element: Any) -> List[str]:
    raw = unwrap(element)
    if raw is None:
        return []
    _set_timeout(raw)
    try:
        err, names = AX.AXUIElementCopyActionNames(raw, None)
    except Exception:
        return []
    return [str(n) for n in names] if err == 0 else []


def children(element: Any) -> List[Any]:
    return list(get_attr(element, AX.kAXChildrenAttribute, []) or [])


def role(element: Any) -> str:
    return _clean(get_attr(element, AX.kAXRoleAttribute, ""))


def role_description(element: Any) -> str:
    return _clean(get_attr(element, AX.kAXRoleDescriptionAttribute, ""))


def title(element: Any) -> str:
    return _clean(get_attr(element, AX.kAXTitleAttribute, ""))


def text_value(element: Any) -> str:
    value = get_attr(element, AX.kAXValueAttribute)
    return value if isinstance(value, str) else ""


def placeholder(element: Any) -> str:
    for name in ("AXPlaceholderValue", "AXPlaceholderString"):
        value = get_attr(element, name)
        if isinstance(value, str) and value.strip():
            return _clean(value)
    return ""


def enabled(element: Any) -> bool:
    value = get_attr(element, AX.kAXEnabledAttribute, True)
    return True if value is None else bool(value)


def element_name(element: Any) -> str:
    """按 AXValue → AXTitle → AXDescription → AXHelp 短路取一个可读名字。

    Chromium 正文放 AXValue、aria-label 放 AXTitle/AXDescription，
    AppKit 控件（如搜索框）又把占位文字只放在 AXDescription，四者都得试。
    """
    for name in (AX.kAXValueAttribute, AX.kAXTitleAttribute, AX.kAXDescriptionAttribute, AX.kAXHelpAttribute):
        text = _clean(get_attr(element, name))
        if text:
            return text
    return ""


def rect_of(element: Any, timeout: float = TIMEOUT) -> Optional[Tuple[int, int, int, int]]:
    pos = get_attr(element, AX.kAXPositionAttribute, timeout=timeout)
    size = get_attr(element, AX.kAXSizeAttribute, timeout=timeout)
    if pos is None or size is None:
        return None
    try:
        ok_p, point = AX.AXValueGetValue(pos, AX.kAXValueCGPointType, None)
        ok_s, dim = AX.AXValueGetValue(size, AX.kAXValueCGSizeType, None)
    except Exception:
        return None
    if not ok_p or not ok_s:
        return None
    l, t = int(round(point.x)), int(round(point.y))
    return (l, t, l + int(round(dim.width)), t + int(round(dim.height)))


def element_at_point(pid: int, x: float, y: float, app: Any = None) -> Optional[Any]:
    """按屏幕坐标（全局点）取元素，供「按文字点击」和排障用。"""
    target = app if app is not None else app_element(pid)
    if target is None:
        return None
    _set_timeout(target)
    try:
        err, element = AX.AXUIElementCopyElementAtPosition(target, float(x), float(y), None)
    except Exception:
        return None
    return element if err == 0 else None


# ---------------------------------------------------------------- 窗口


def windows(pid: int, app: Any = None, timeout: float = TIMEOUT) -> List[Any]:
    target = app if app is not None else app_element(pid, timeout=timeout)
    if target is None:
        return []
    out = list(get_attr(target, AX.kAXWindowsAttribute, [], timeout=timeout) or [])
    out.extend(get_attr(target, "AXFloatingWindows", [], timeout=timeout) or [])
    return out


def focused_window(pid: int, app: Any = None) -> Optional[Any]:
    target = app if app is not None else app_element(pid)
    if target is None:
        return None
    win = get_attr(target, AX.kAXFocusedWindowAttribute)
    if win is None:
        win = get_attr(target, AX.kAXMainWindowAttribute)
    return unwrap(win)


def frontmost(pid: int, flag: Optional[bool] = None, app: Any = None) -> Optional[bool]:
    """读 / 写 AXFrontmost。flag=None 表示只读。"""
    target = app if app is not None else app_element(pid)
    if target is None:
        return None
    if flag is None:
        value = get_attr(target, AX.kAXFrontmostAttribute)
        return None if value is None else bool(value)
    if set_attr(target, AX.kAXFrontmostAttribute, bool(flag)) != 0:
        return False
    return bool(get_attr(target, AX.kAXFrontmostAttribute, False))


def window_summaries(pid: int, timeout: float = TIMEOUT, build_tree: bool = True) -> List[Dict[str, Any]]:
    """一个进程的 AX 窗口清单 {element, rect, title, minimized}，一次 AX 往返拿全。

    CGWindowList 的 kCGWindowName 对无标题窗口会给空串或残留旧值，
    标题与最小化状态必须以 AX 侧为准（见 winops.list_windows）。
    """
    out: List[Dict[str, Any]] = []
    app = app_element(pid, timeout=timeout, build_tree=build_tree)
    if app is None:
        return out
    for win in windows(pid, app=app, timeout=timeout):
        out.append(
            {
                "element": win,
                "rect": rect_of(win, timeout=timeout),
                "title": _clean(get_attr(win, AX.kAXTitleAttribute, "", timeout=timeout)),
                "minimized": bool(get_attr(win, AX.kAXMinimizedAttribute, False, timeout=timeout)),
            }
        )
    return out


def _overlap_ratio(rect: Sequence[int], other: Optional[Sequence[int]]) -> float:
    """交集 / 两者较大面积。1.0 = 完全重合，0.0 = 不相交或拿不到坐标。"""
    if not other:
        return 0.0
    inside = _intersect(tuple(rect), tuple(other))
    if not inside:
        return 0.0
    return _area(inside) / float(max(1, max(_area(rect), _area(other))))


def window_for(
    pid: int,
    rect: Optional[Sequence[int]] = None,
    title_hint: str = "",
    timeout: float = TIMEOUT,
    build_tree: bool = True,
) -> Optional[Any]:
    """按「标题是否相等 + 全局 rect 重叠率」挑最匹配的 AX 窗口；**没有够像的返回 None**。

    AX 侧没有 CGWindowID，只能靠坐标对齐；实测两边坐标一致，重叠率即唯一键。
    旧实现即使重叠为 0 也返回 wins[0]，于是传一个完全屏外的 rect 能读到真窗口，
    拿一张空标题幽灵窗的 rect 也能读到真窗口 —— 而 rect 随后被 clip 成交集，
    元素中心落进幽灵窗里，点击 / 粘贴就打在错的地方（台账 #5）。现在宁可不读。
    """
    wins = window_summaries(pid, timeout=timeout, build_tree=build_tree)
    if not wins:
        return None
    hint = _canonical(title_hint)
    if rect is None:
        # 没有 rect 可比时只认标题；标题也没有就取面积最大的**有名字**的窗口，
        # 空标题的常驻幽灵窗（面板 / 提示窗）不参选
        named = [w for w in wins if _canonical(w["title"]) == hint] if hint else []
        if not named:
            named = [w for w in wins if w["title"]] or wins
        return max(named, key=lambda w: _area(w["rect"] or (0, 0, 0, 0)))["element"]

    want = tuple(int(v) for v in rect)
    if _area(want) <= 0:
        return None
    best, best_key = None, None
    for info in wins:
        ratio = _overlap_ratio(want, info["rect"])
        if ratio < MIN_WINDOW_OVERLAP:
            continue  # 零重叠 / 只对上一半的窗口一律不当它是同一个
        key = (
            1 if hint and _canonical(info["title"]) == hint else 0,
            ratio,
            _area(info["rect"] or (0, 0, 0, 0)),
        )
        if best_key is None or key > best_key:
            best, best_key = info["element"], key
    return best


def raise_window(element: Any) -> bool:
    return perform(element, AX.kAXRaiseAction)


def set_minimized(element: Any, flag: bool) -> bool:
    raw = unwrap(element)
    if raw is None:
        return False
    if set_attr(raw, AX.kAXMinimizedAttribute, bool(flag)) != 0:
        return False
    return bool(get_attr(raw, AX.kAXMinimizedAttribute, flag)) == bool(flag)


# ---------------------------------------------------------------- 动作 / 赋值


def perform(element: Any, action: str = AX.kAXPressAction) -> bool:
    raw = unwrap(element)
    if raw is None:
        return False
    _set_timeout(raw)
    try:
        return int(AX.AXUIElementPerformAction(raw, action)) == 0
    except Exception:
        return False


def press(element: Any) -> bool:
    """执行默认动作。True 只代表系统接受了动作：React 受控组件同样返回成功却什么都不做，
    是否真的发出去了要靠上层回读校验。"""
    return perform(element, AX.kAXPressAction)


def focus(element: Any) -> bool:
    raw = unwrap(element)
    if raw is None:
        return False
    if set_attr(raw, AX.kAXFocusedAttribute, True) != 0:
        return False
    return bool(get_attr(raw, AX.kAXFocusedAttribute, False))


def get_value(element: Any) -> str:
    raw = unwrap(element)
    return text_value(raw) if raw is not None else ""


def _same(a: str, b: str) -> bool:
    return (a or "").replace(" ", "").rstrip("\n") == (b or "").replace(" ", "").rstrip("\n")


def write_value(element: Any, text: str, verify: bool = True) -> Tuple[bool, str]:
    """写 AXValue 并回读校验，返回 (是否生效, 说明)。

    必须校验：Chromium / Electron 的 React 受控输入对 AXValue 赋值会**返回成功但静默忽略**
    （实测 ChatGPT 输入框 set → err=0，回读仍是占位文字），不校验就是谎报成功。
    """
    raw = unwrap(element)
    if raw is None:
        return False, "元素句柄无效"
    err = set_attr(raw, AX.kAXValueAttribute, text)
    if err != 0:
        return False, "AXValue 写入被拒：%s" % err_name(err)
    if not verify:
        return True, "ok(未校验)"
    time.sleep(SETTLE_S)
    if _same(get_value(raw), text):
        return True, "ok"
    sel_err = set_attr(raw, "AXSelectedText", text)
    if sel_err == 0:
        time.sleep(SETTLE_S)
        if _same(get_value(raw), text):
            return True, "ok(AXSelectedText)"
    return False, "写入被静默忽略（回读=%r），该应用只能走 ax_type / paste 通道" % (get_value(raw)[:32],)


def set_value(element: Any, text: str, verify: bool = True) -> bool:
    return write_value(element, text, verify)[0]


# ---------------------------------------------------------------- 子树遍历


def read_window_tree(window: Any, max_elements: int = 900) -> ReadResult:
    """深度遍历一个窗口的 AX 子树，产出 ReadResult。任何失败都收敛到 result.error，不抛异常。

    window 传 WindowInfo（推荐）或 pid。
    """
    result = ReadResult()
    t0 = time.time()
    if not trusted():
        result.error = "缺辅助功能权限：系统设置 → 隐私与安全性 → 辅助功能"
        result.ms = (time.time() - t0) * 1000
        return result

    if isinstance(window, int):
        pid, rect, hint = window, None, ""
    else:
        pid = getattr(window, "pid", None)
        rect = getattr(window, "rect", None)
        hint = getattr(window, "title", "") or ""
    if pid is None:
        result.error = "read_window_tree 需要 WindowInfo 或 pid"
        return result

    app = app_element(int(pid))
    if app is None:
        result.error = "取不到 pid=%s 的 AX 应用句柄" % pid
        return result
    root = window_for(int(pid), rect, hint)
    if root is None:
        result.error = (
            "pid=%s 的 AX 窗口与给定 rect %s 重叠不足（<%.0f%%），拒绝读取：防读到别的窗口 / 幽灵窗"
            % (pid, tuple(rect) if rect else None, MIN_WINDOW_OVERLAP * 100)
            if rect
            else "pid=%s 没有可读的 AX 窗口（应用没开窗口 / 只有菜单栏常驻）" % pid
        )
        result.ms = (time.time() - t0) * 1000
        return result
    r_root = role(root)

    # max_elements 限制产出条数；访问节点上限放宽 VISIT_FACTOR 倍，
    # 否则 Chromium 侧边栏上千个噪声节点会把正文和输入框挤出去。
    visit_limit = max(int(max_elements) * VISIT_FACTOR, MIN_VISITS)
    # 预算从**拿到 root 之后**起算：取 root（含把 Electron 的无障碍树叫起来）本身就要几百毫秒，
    # 从 t0 起算等于把这笔钱也算进遍历，静止窗口就会被读成半截（台账 #9）。
    t_walk = time.time()
    deadline = t_walk + READ_BUDGET_S
    clip = tuple(rect) if rect and _area(rect) > 0 else None

    texts: List[Tuple[int, int, str]] = []
    inputs: List[ElementRef] = []
    buttons: List[ElementRef] = []
    dropped = 0
    visited = 0
    truncated = ""
    # Chromium 把 AXApplication / AXMenuBar 当窗口的子节点挂回来，不记就会原地打转
    seen: set = set()
    stack: List[Tuple[Any, int]] = [(root, 0)]
    while stack:
        raw, depth = stack.pop()
        try:
            if raw in seen:
                continue
            seen.add(raw)
        except TypeError:
            pass
        visited += 1
        if visited > visit_limit:
            truncated = "达到节点访问上限 %d" % visit_limit
            break
        if time.time() > deadline:
            truncated = "遍历超过预算 %.1fs 提前收工" % READ_BUDGET_S
            break

        r = role(raw)
        # 锁屏时窗口自身的 role 也会报成 AXApplication，只有深度 > 0 才按噪声跳过
        if depth > 0 and r in SKIP_ROLES:
            continue
        desc = role_description(raw)
        is_input = r in INPUT_ROLES or any(k in desc for k in INPUT_DESC_KEYS)
        name = element_name(raw)
        box = rect_of(raw)

        kind = "text"
        if is_input:
            kind = "input"
        elif r in STRICT_BUTTON_ROLES:
            kind = "button"
        elif r in PSEUDO_BUTTON_ROLES and name and AX.kAXPressAction in action_names(raw):
            kind = "pseudo"

        if depth < MAX_DEPTH:
            for child in reversed(children(raw)):
                stack.append((child, depth + 1))

        if kind == "text":
            if not name:
                continue
            for line in (_clean(x) for x in name.splitlines()):
                if line:
                    texts.append(((box[1] if box else 0), (box[0] if box else 0), line))
            continue

        if box is not None and _area(box) <= 0:
            # 有坐标但面积为零 = 被滚动区裁掉 / 隐藏的控件，点不到，别给上层当坐标用。
            continue
        if box is None:
            # 完全没有坐标：元素必须留着（锁屏时系统对所有元素回 -25205，丢了输入框和
            # 发送钮 ax_direct 就没法用），但**不能伪造 (0,0,0,0)** —— 那会被上层当成屏幕
            # 左上角真的去点。给一个面积 0、中心在所有显示器之外的占位 rect，并把 enabled
            # 置 False 明确标记「不可点击」（台账 #26）。
            box = NO_GEOMETRY
        elif clip and not _center_in(box, clip):
            # 中心落在窗口外的元素不属于这个窗口。旧实现把它裁成交集，于是假 rect 的中心
            # 落进了窗口里 —— 点击/粘贴就打在别的窗口上（台账 #5）。现在直接丢弃。
            dropped += 1
            continue
        no_geo = box == NO_GEOMETRY
        ref = ElementRef(
            name=name or (placeholder(raw) if kind == "input" else ""),
            control_type=r or ("AXTextArea" if kind == "input" else "AXButton"),
            rect=box,
            source="ax",
            enabled=False if no_geo else enabled(raw),
            ref=raw,
            depth=depth,
            score=_confidence(kind, r, desc),
        )
        if kind == "input":
            if len(inputs) < max_elements:
                inputs.append(ref)
        elif len(buttons) < max_elements:
            buttons.append(ref)

    texts.sort(key=lambda x: (x[0], x[1]))
    result.text = "\n".join(x[2] for x in texts)
    result.input_boxes = inputs
    result.buttons = buttons
    result.elements = len(texts) + len(inputs) + len(buttons)
    result.ms = (time.time() - t0) * 1000
    if truncated:
        # 半截读数绝不能被上层当成「内容变了」或「读不到」：必须带标记（台账 #9）
        result.truncated = True
    notes = []
    if dropped:
        notes.append("丢弃 %d 个中心落在窗口 rect 外的元素" % dropped)
    if truncated:
        notes.append(truncated)
    if not inputs and not buttons and (not result.text.strip() or visited <= 5):
        # 只读到标题、连一个控件都没有 = 子树被折起来了：
        # 锁屏时系统会把每个应用的窗口子树折成 AXApplication + AXMenuBar
        why = (
            "锁屏态：系统只暴露 AXApplication/AXMenuBar，窗口内容不在 AX 树里（解锁后才有）"
            if r_root == "AXApplication"
            else "应用没有建无障碍树"
        )
        result.error = "AX 树几乎没有内容（访问 %d 节点）：%s" % (visited, truncated or why)
    elif notes:
        result.error = "；".join(notes)
    return result


# ---------------------------------------------------------------- 发送按钮


def _row_band(box: Sequence[int]) -> Tuple[float, float]:
    """输入框所在行的纵向容差带：**上下各半行高**。

    聊天界面的发送钮就贴在输入框那一行的右端；留一整行高（旧实现）会把侧边栏里
    第 N 个会话标题一起收进「同一行」，误点一下就切了会话（台账 #17）。
    """
    h = max(1, box[3] - box[1])
    half = max(6.0, h / 2.0)
    return box[1] - half, box[3] + half


def _send_row_ok(rect: Sequence[int], box_rect: Sequence[int]) -> bool:
    """几何判据：中心落在输入框那一行、且在输入框右半段或紧贴其右侧。"""
    if not has_geometry(rect) or not has_geometry(box_rect):
        return False
    top, bottom = _row_band(box_rect)
    tol = max(6.0, (box_rect[3] - box_rect[1]) / 2.0)
    cx, cy = (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0
    if not (top <= cy <= bottom):
        return False
    return box_rect[0] - tol <= cx <= box_rect[2] + 3.0 * tol


def _rank_buttons(
    buttons: Sequence[ElementRef],
    box_rect: Optional[Sequence[int]],
    keywords: Sequence[str],
) -> List[Tuple[float, ElementRef]]:
    scored: List[Tuple[float, ElementRef]] = []
    if box_rect and not has_geometry(box_rect):
        box_rect = None  # 锁屏时 AX 给不出坐标，位置启发式会拿占位 rect 算出垃圾
    band = _row_band(box_rect) if box_rect else None
    for btn in buttons:
        name = _canonical(btn.name)
        if not name:
            continue
        score = 0.0
        for i, kw in enumerate(keywords):
            if not kw:
                continue
            if name == kw:
                score = 100.0 - i
                break
            if name.startswith(kw) and len(name) <= len(kw) + 6:
                score = 80.0 - i
                break
            if kw in name and len(name) <= len(kw) + 10 and btn.score >= 1.0:
                score = 60.0 - i
                break
        if score > 0 and box_rect and has_geometry(btn.rect):
            # 侧边栏里的会话标题也可能含「发送」二字（实测 'qq自动发送助手' 就以 60 分登顶过）。
            # 旧判据只看横向、还留 40pt 容差，差 3pt 躲过去了；现在按「输入框半行高」判同排，
            # 不同排的非精确命中一律否掉，精确命中的重罚（台账 #17）。
            if not _send_row_ok(btn.rect, box_rect):
                score -= 80.0 if score < 100.0 else 25.0
        if score == 0.0 and band and box_rect and has_geometry(btn.rect):
            cx, cy = btn.center
            box_cx = (box_rect[0] + box_rect[2]) / 2.0
            w = max(1, btn.rect[2] - btn.rect[0])
            h = max(1, btn.rect[3] - btn.rect[1])
            same_row = band[0] <= cy <= band[1] and cx >= box_cx
            roundish = w * h >= 200 and max(w, h) <= min(w, h) * 2.5
            if same_row and roundish and len(btn.name) <= 24:
                score = (
                    20.0
                    - abs(cy - (box_rect[3] + 8)) / 50.0
                    - (cx - box_cx) / float(max(1, box_rect[2] - box_rect[0]))
                )
        if score > 0:
            scored.append((score * (0.5 if btn.score < 1.0 else 1.0), btn))
    scored.sort(key=lambda x: (-x[0], -_area(x[1].rect)))
    return scored


def _normalise_source(source: Any) -> Tuple[List[ElementRef], Optional[Sequence[int]]]:
    if isinstance(source, ReadResult):
        buttons = list(source.buttons)
        boxes = [b for b in source.input_boxes if has_geometry(b.rect)] or list(source.input_boxes)
        box = max(boxes, key=lambda b: b.area) if boxes else None
        return buttons, (box.rect if box else None)
    buttons = [b for b in (source or []) if isinstance(b, ElementRef)]
    return buttons, None


def find_send_button(
    source: Any,
    box: Any = None,
    titles: Iterable[str] = SEND_TITLES,
) -> Optional[ElementRef]:
    """挑发送按钮：先按标题命中（发送 / Send / 提交 / Enter），再用「输入框同行右侧」补位。

    入参可以是 ReadResult、按钮列表；box 可传 ElementRef 或裸 AXUIElement。
    返回值带 score，第一个即最优。
    """
    buttons, box_rect = _normalise_source(source)
    if box is not None:
        box_rect = box.rect if isinstance(box, ElementRef) else (rect_of(box) or box_rect)
        if box_rect is not None and not has_geometry(box_rect):
            box_rect = None
    if not buttons:
        return None
    ranked = _rank_buttons(buttons, box_rect, [_canonical(t) for t in titles])
    if not ranked:
        return None
    score, btn = ranked[0]
    if score < MIN_SEND_SCORE:
        # 置信度不够就不下发：这里点错一下的代价是切会话 / 点到别的东西，
        # 比这一轮不点严重得多（台账 #17）。完整排名留给 send_button_candidates 看。
        return None
    return ElementRef(
        name=btn.name,
        control_type=btn.control_type,
        rect=btn.rect,
        source=btn.source,
        enabled=btn.enabled,
        ref=btn.ref,
        depth=btn.depth,
        score=round(score, 3),
    )


def send_button_candidates(
    source: Any,
    box: Any = None,
    limit: int = 8,
    titles: Iterable[str] = SEND_TITLES,
) -> List[ElementRef]:
    """有序候选表（含 score），给探针和人工排障看；主链路只用 find_send_button。"""
    buttons, box_rect = _normalise_source(source)
    if box is not None:
        box_rect = box.rect if isinstance(box, ElementRef) else (rect_of(box) or box_rect)
    ranked = _rank_buttons(buttons, box_rect, [_canonical(t) for t in titles])
    out: List[ElementRef] = []
    for score, btn in ranked[: max(0, int(limit))]:
        out.append(
            ElementRef(
                name=btn.name,
                control_type=btn.control_type,
                rect=btn.rect,
                source=btn.source,
                enabled=btn.enabled,
                ref=btn.ref,
                depth=btn.depth,
                score=round(score, 3),
            )
        )
    return out


def click_element(element: Any) -> bool:
    """按元素中心走系统坐标取一次元素并按下（AXPress 不可用时的兜底）。"""
    raw = unwrap(element)
    if raw is None:
        return False
    box = rect_of(raw)
    if not box:
        return False
    pid = _pid_of(raw)
    if pid is None:
        return False
    hit = element_at_point(pid, (box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    return perform(hit, AX.kAXPressAction) if hit is not None else False


def _pid_of(element: Any) -> Optional[int]:
    raw = unwrap(element)
    if raw is None:
        return None
    try:
        res = AX.AXUIElementGetPid(raw, None)
    except Exception:
        return None
    # pyobjc 把 C 的出参 AXPID* 包成 (错误码, pid)：单参调用会抛，抛了就等于
    # 归属判定永远返回 None，「命中的元素到底属不属于这个应用」这条检查会形同虚设。
    if isinstance(res, (tuple, list)):
        if len(res) < 2 or res[0] or res[1] is None:
            return None
        return int(res[1])
    return int(res) if res else None


# ---------------------------------------------------------------- 自检


def _self_check(argv: Sequence[str]) -> int:
    import os

    import AppKit

    print("== aiwatch.mac.ax 自检 ==")
    if not trusted():
        print("缺权限：辅助功能（AXIsProcessTrusted=False）")
        print("  请到 系统设置 → 隐私与安全性 → 辅助功能 勾选运行本自检的程序")
        print("  本自检不调用任何带 prompt 的 API，不会弹系统对话框")
        print("read_window_tree 在这种状态下的行为:", read_window_tree(0).error)
        return 2
    print("辅助功能已授权: True  自身 pid=%d" % os.getpid())

    probe_text = ""
    if "--write" in argv:
        i = argv.index("--write")
        probe_text = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("--") else "AI-WATCHDOG-PROBE"
    only = ""
    if "--only" in argv:
        only = argv[argv.index("--only") + 1] if len(argv) > argv.index("--only") + 1 else ""

    ws = AppKit.NSWorkspace.sharedWorkspace()
    seen = set()
    apps = []
    for a in ws.runningApplications():
        if a.activationPolicy() == 0 and a.processIdentifier() != os.getpid():
            pid = int(a.processIdentifier())
            if pid not in seen:
                seen.add(pid)
                apps.append((pid, str(a.localizedName() or ""), str(a.bundleIdentifier() or "")))

    hints = ("electron", "openai", "codex", "zhipu", "moonshot", "doubao", "coze", "hermes", "claw", "claude", "kimi")
    picked = []
    for pid, name, bundle in apps:
        summaries = window_summaries(pid)
        if not summaries:
            continue
        tag = (bundle + " " + name).lower()
        picked.append((0 if any(h in tag for h in hints) else 1, -len(summaries), pid, name, bundle, summaries))
    picked.sort()
    print("有 AX 窗口的常规应用 %d 个，先看 AI 应用的第一个窗口：" % len(picked))

    listed = 0
    for _, _, pid, name, bundle, summaries in picked:
        if only and not re.search(only, "%s %s %d" % (name, bundle, pid), re.I):
            continue
        if not only and listed >= 10:
            break
        listed += 1
        flags = enable_ax_tree(app_element(pid, build_tree=False))
        win = summaries[0]
        res = read_window_tree(_PlainWindow(pid, win["rect"] or (0, 0, 0, 0), win["title"]))
        print(
            "\n[%d] %s (%s)\n  窗口 %r %s minimized=%s  建树开关=%s"
            % (pid, name, bundle, win["title"], win["rect"], win["minimized"], flags)
        )
        print(
            "  读取 %d 字 / %d 元素 / %.0fms；输入框 %d、按钮 %d%s"
            % (res.chars, res.elements, res.ms, len(res.input_boxes), len(res.buttons), "  error=%s" % res.error if res.error else "")
        )
        if res.text:
            head = [ln for ln in res.text.splitlines() if ln][:3]
            print("  文本首 3 行:", head)
        for box in res.input_boxes[:2]:
            print("   输入框 %-12s %s enabled=%s 内容=%r" % (box.control_type, box.rect, box.enabled, box.name[:24]))
        for cand in send_button_candidates(res, limit=3):
            print("   发送候选 %-12s %s %r score=%.1f" % (cand.control_type, cand.rect, cand.name[:20], cand.score))
        mid = res.buttons[0] if res.buttons else None
        if mid is not None:
            print("   按钮首个:", mid)
        hit = None
        if win["rect"]:
            cx, cy = (win["rect"][0] + win["rect"][2]) // 2, (win["rect"][1] + win["rect"][3]) // 2
            hit = element_at_point(pid, cx, cy)
        print("   窗口中心取点:", "None" if hit is None else "%s %r" % (role(hit), element_name(hit)[:24]))
        if probe_text and res.input_boxes:
            box = max(res.input_boxes, key=lambda b: b.area)
            before = get_value(box)
            # 空输入框读回来的是占位文字，照原样写回会把占位符变成真内容
            restore_to = "" if before == placeholder(box) else before
            ok, detail = write_value(box, probe_text)
            after = get_value(box)
            if ok:  # 没写进去就不用还原，免得把占位文字当真内容塞回输入框
                write_value(box, restore_to)
            print("   写入校验: %s（%s）回读=%r 现在=%r" % (ok, detail, after[:24], get_value(box)[:24]))
    return 0


class _PlainWindow:
    """自检用的最小窗口对象：只带 read_window_tree 需要的三个字段。"""

    def __init__(self, pid: int, rect: Any, title: str = "") -> None:
        self.pid = pid
        self.rect = rect
        self.title = title


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_self_check(sys.argv[1:]))
