# 任务雷达 Windows

Windows 原生 WPF 版本，版本 1.9.0。与 macOS 共用本机会话适配器、轮次判断和恢复协议；悬浮窗、胶囊、提醒、设置、监督和顺序队列保持相同使用方式。

[下载 Windows 安装包与便携包](https://github.com/SJT-bright/agent-radar/releases/tag/v1.9.0) · [项目开源代码](https://github.com/SJT-bright/agent-radar) · [macOS 版本](../README.md)

## 安装

Windows 10/11 x64。安装版默认放在当前用户的本地应用目录，不要求管理员权限；便携版解压后打开 `AgentRadar.exe`。包内含 .NET 运行时和 Python 采集组件，无需另装 Python。用户设置与恢复记录保存在 `%APPDATA%\AgentRadar`，升级和卸载不删除它们。

第一次打开默认关闭自动发送。目标 AI 应用应与任务雷达使用相同权限级；在设置中查询窗口访问，再逐条选择监督范围。

## OpenClaw 持续关照

在会话右键菜单或「设置 → 监督」中开启指定 OpenClaw 会话的持续关照，并明确开启自动发送。关注后续的新鲜轮次：意外中断后通过本地网关恢复原会话，完成后提醒验收。主动停止、等待确认、身份不明、认证或上下文限制只提醒。后台子任务须逐条选择，定时任务不自动启动。

恢复严格按入队顺序进行；响应确认最多 30 秒，超时后继续观察，不重复发送同一轮。自动恢复每条会话每小时最多 3 次，次数写入本机持久记录，重启不会清零。OpenClaw 恢复在后台完成，无需抢占键盘或切换窗口；持续关照不修复模型服务本身的连接错误。

原生 OpenClaw 默认读取用户目录下 `.openclaw`，本地网关默认 `ws://127.0.0.1:18789`。原生命令通过 `node.exe` 与明确的 `openclaw.mjs` 执行，不把提示词交给 shell。

WSL 安装可在常规设置指定用户目录，例如 `\\wsl.localhost\Ubuntu\home\your-user`、发行版 `Ubuntu` 和发行版内的 OpenClaw 可执行路径。只接受本机指定 WSL 共享和 loopback 网关，不接受远程共享、远程网关、查询参数中的凭据。OpenClaw 官方 Windows 指引见 [Windows 平台说明](https://docs.openclaw.ai/platforms/windows)。

## 功能与验收边界

| 项目 | Windows 实现 |
| --- | --- |
| 悬浮窗、胶囊 | 238 点展开、60×32 点收起；顶部锚点、拖动、悬停与屏幕边界 |
| 会话列表 | 按软件分组、目录主标题、会话副标题、计时、待确认、置顶、移除与恢复 |
| 提醒 | 每条会话保留最新提醒、顺序查看、全部知道了、跳转、恢复一次、音效 |
| 设置 | 常规、监督、提示词、诊断；全文草稿保留、超限不可保存、关闭前处理草稿 |
| 恢复 | 严格 FIFO、发送前身份与输入回读、同轮防重复、30 秒原会话响应确认 |
| OpenClaw | 原会话 RPC、逐条持续关照、持久频率保护、原生与指定 WSL |
| 窗口访问 | Windows UI Automation，原始正文和输入内容不进入诊断文件 |
| 夜间常亮 | 可释放的系统执行状态请求；不会修改系统锁屏或模拟真人输入 |
| 启动 | 当前用户登录启动项、单实例与托盘入口 |

Windows 与 macOS 使用各自字体与系统合成效果，毛玻璃效果随 Windows 版本变化。桌面 AI 应用的新版本可能改变本地数据格式或可访问性树；无法证明会话身份或唯一发送按钮时保持待确认。Windows 上每款外部 AI 应用的真实发送，需要在安装了对应软件的设备继续验收；CI 控件检查不代表外部消息已经送达。

## 构建与测试

```powershell
python -m pip install pyinstaller==6.16.0
dotnet run --project Windows/AgentRadar.Core.Tests -c Release
python -B -m unittest discover -s tests -p 'test_windows*.py' -v
./Windows/build.ps1
```

构建需要 .NET 10 SDK、Python 3.12 和 Inno Setup。WPF 是 Windows 原生桌面框架，见 [Microsoft 官方说明](https://learn.microsoft.com/en-us/dotnet/desktop/wpf/overview/)。GitHub Actions 在 Windows runner 中构建安装版与便携版，再安装到含中文与空格的目录，运行安装后的冻结采集器及实际 WPF 控件测试，并检查卸载保留用户数据。隔离验收不调用真实 AI 应用或发送消息。

源码采用仓库根目录 MIT 许可。安装包依赖的 .NET、Python、PyInstaller 和 Inno Setup 各自保留原许可。
