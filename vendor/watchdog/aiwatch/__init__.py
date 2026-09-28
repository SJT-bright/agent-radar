"""AI Watchdog —— 监控 AI 应用是否卡住/跑完/触发限额，并自动续写。

模块划分：
    config    配置加载与目标定义
    winio     Windows 窗口枚举 / 置前 / 键鼠注入
    ocr       Windows 系统 OCR（本地、支持中文）
    sensor    状态采集（UIA 控件树直读 + OCR 兜底）
    rules     规则引擎（静默超时 + 关键词 + 限额识别）
    actuator  执行层（置前 → 定位输入框 → 粘贴 → 回车）
    worker    单应用工作进程（一个目标一个子进程）
    manager   多进程调度 + 看板
    probe     探针：检测每个应用能否被注入
"""

__version__ = "1.0.0"
