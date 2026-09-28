# 集成来源与边界

`scripts/bundle_watchdog.py` 直接复制用户提供的 `ai监管/ai-watchdog/aiwatch/types.py` 及 `mac/ax.py`、`mac/winops.py`、`mac/inject.py` 到应用资源，复用原实现；这些原文件不修改。

`bridge.py` 是任务雷达的严格调用层：只支持固定或用户配置的续写消息与明确会话，不启动原 watchdog daemon，不使用原默认按静默/完成续写的规则，也不更改系统锁屏设置。Python 依赖随应用复制自原有 venv，保留相应 dist-info 和许可证。程序不使用原工程配置、报告或凭据。

动作前通过本地适配器再次核验原会话与轮次。控件目标只用 AX，匹配失败就停，不做盲目坐标输入。真实发送后由 Swift 监控新状态判断恢复；结果未知不重试。

`tests/test_continuation.py` 用隔离后端验证动作顺序、保护条件、防重与持久化；Swift 策略测试验证生命周期触发。它们不代表真实 AI 对话已成功发送。

1.4.12：总开关默认关闭，升级后需显式开启；关闭仍观察新中断并提醒。启用的恢复允许覆盖已核验输入框中的草稿，WorkBuddy 两版跳过会导致编辑器状态失步的 AXValue，点击经核验的唯一 AX 输入框内部建立真实选择区，全选粘贴并回读；剪贴板等编辑器确认后恢复完整内容。窗口激活不调用原 watchdog 的最小化回退。`last-recovery.json` 仅记录最近结果码、阶段、应用 ID、轮次摘要及时间。
