"""跨平台数据类型。平台层（win / mac）与上层（sensor / actuator / worker）共用的唯一契约。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

Rect = Tuple[int, int, int, int]  # left, top, right, bottom（全局坐标）


@dataclass
class WindowInfo:
    """一个顶层窗口。hwnd 在 macOS 上是 CGWindowID，在 Windows 上是句柄。"""

    hwnd: int
    title: str
    pid: int
    process: str  # macOS: 应用显示名（如 ChatGPT）；Windows: 进程名（不含 .exe）
    rect: Rect
    visible: bool = True
    minimized: bool = False
    class_name: str = ""  # macOS 上填 bundle id

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    def __str__(self) -> str:
        return f"[{self.hwnd}] {self.process} :: {self.title!r} {self.width}x{self.height}"


@dataclass
class FocusReport:
    ok: bool
    strategy: str
    foreground: int
    target: int

    def __str__(self) -> str:
        return (
            f"{'OK' if self.ok else 'FAIL'} via {self.strategy} "
            f"(foreground={self.foreground} target={self.target})"
        )


@dataclass
class ElementRef:
    """一个可操作的 UI 元素。

    source: 'ax'（辅助功能控件）| 'uia'（Windows 控件树）| 'ocr'（屏幕文字）
    ref:    平台原生句柄，上层只负责原样传回 Backend 的 ax_* 方法。
    """

    name: str
    control_type: str
    rect: Rect
    source: str
    enabled: bool = True
    ref: Any = None
    depth: int = 0
    score: float = 1.0

    @property
    def center(self) -> Tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2

    @property
    def text(self) -> str:
        return self.name

    @property
    def area(self) -> int:
        l, t, r, b = self.rect
        return max(0, r - l) * max(0, b - t)

    def __str__(self) -> str:
        return f"<{self.source}:{self.control_type} {self.name!r} {self.rect}>"


@dataclass
class ReadResult:
    """一次控件树 / OCR 读取的原始产出。"""

    text: str = ""
    input_boxes: List[ElementRef] = field(default_factory=list)
    buttons: List[ElementRef] = field(default_factory=list)
    error: str = ""
    elements: int = 0
    ms: float = 0.0
    # 遍历超预算被截断时为真。半截文本必须带上这个标记：它不能拿去刷新静默指纹，
    # 也不能当"读不到"处理 —— 实测流式输出时满树要 2.1~2.4s，超预算是常态。
    truncated: bool = False

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class OcrLine:
    text: str
    rect: Rect
    confidence: float = 1.0

    @property
    def center(self) -> Tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2


@dataclass
class OcrResult:
    lines: List[OcrLine] = field(default_factory=list)
    width: int = 0
    height: int = 0
    ms: float = 0.0
    error: str = ""

    @property
    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines)

    def find(self, needle: str) -> List[OcrLine]:
        out = []
        low = needle.lower()
        for ln in self.lines:
            if low in ln.text.lower():
                out.append(ln)
        return out


# ---------------------------------------------------------------- 注入能力分级

TIER_AX_DIRECT = "ax_direct"  # AX 赋值 + AXPress 发送，不依赖前台，锁屏可用
TIER_AX_TYPE = "ax_type"  # AXRaise 置前 + 键盘事件，需要解锁
TIER_PASTE = "paste"  # 剪贴板 + ⌘V + 回车，需要解锁且在前台
TIER_ORDER = (TIER_AX_DIRECT, TIER_AX_TYPE, TIER_PASTE)


@dataclass
class Capability:
    """某个应用被 probe 实测出来的注入能力矩阵。"""

    key: str
    tiers: List[str] = field(default_factory=list)  # 按可用性从高到低
    read_mode: str = "none"  # ax | ocr | none
    input_hint: str = ""  # 实测有效的输入框标识，如 'ax:AXTextArea'
    send_hint: str = ""  # 实测有效的发送按钮标识，如 'ax:发送'
    locked_ok: bool = False  # 锁屏态是否仍能读写
    note: str = ""
    tested_at: str = ""

    def best_tier(self, locked: bool) -> Optional[str]:
        for t in TIER_ORDER:
            if t not in self.tiers:
                continue
            if locked and t != TIER_AX_DIRECT:
                continue
            return t
        return None
