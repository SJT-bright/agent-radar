# 任务雷达（Agent Radar）

原生 macOS AI 任务悬浮窗。当前公开源码对应 **1.8.19（build 73）**。它从本机可读取的会话状态和窗口信息中显示当前对话、运行时间、完成与中断状态，不需要 AI API Key。

## 下载

[下载最新版 macOS 应用](https://github.com/SJT-bright/agent-radar/releases/latest)。将 ZIP 中的“任务雷达.app”放入 Applications，启动后按浮窗指引完成辅助功能授权，再点击“查询”复查。应用包面向 Apple Silicon、macOS 13+，使用本地代码签名，未做 Apple 公证。

1.8.19 修复展开浮窗退出后重启位置下移的问题，统一保存顶部锚点。当前版本包含 Codex 长对话分块回溯与“待确认”显示、Gemini 原生应用发现、三点菜单悬停展开与离开收起，以及慢采集来源隔离和界面重复刷新优化。[更新记录](CHANGELOG.md) · [发布验证](docs/release-validation.md)

## 功能

- 集中查看 Codex、Claude Code、ZCode、WorkBuddy、AutoClaw、Qoder、Grok、Cline 等应用的可识别会话；Gemini、Antigravity 等应用按实际可读窗口发现，支持程度取决于它们暴露的控件。点击卡片可跳转到相应应用或工作区。
- 显示真实轮次计时、完成或中断原因。身份可确认而轮次未知的 Codex 会话显示“待确认”；缺少证据的状态不算正在运行。
- 置顶浮窗支持折叠、按应用汇总、移除和恢复会话、透明度与磨砂。以工作文件夹为主名称、对话名为副名称；可信活动会话跨软件使用同一目录时提示冲突。
- 三点菜单支持悬停、点击和键盘操作。提醒弹窗与任务音效可独立开启，提醒可拖动，并显示实际恢复倒计时。
- 协作模式提醒用户验收。明确开启自动发送并选择监督范围后，可对可核验的新轮次按规则续接；发送前核对原会话、轮次和输入框，写入后回读，同一轮防止重复发送。不同应用的续接支持及真实验收程度不同。
- 提供辅助功能自检和夜间防空闲休眠开关。监控与状态处理在本机完成。
- 设置按运行、监督、提示词和限流分区，支持搜索折叠、草稿保护、⌘V 粘贴及 ⌘S 保存。提示词超过 2000 字符时保留全部草稿，删减到上限后才能保存。
- 支持的前台 AI 应用出现唯一、可点击的新“插队”按钮时可自动点击一次；可关闭，暂停监控时停止。

## 识别原理

本地数据库和日志提供稳定会话身份与轮次事件；macOS 辅助功能提供窗口、标题、按钮和输入框。二者合并后检查新鲜度，自动操作前再次核验目标。本项目目前主要读取结构化信息，没有通用截图视觉模型来理解全部屏幕内容。详见 [会话与界面识别](docs/recognition.md)。

## 构建

需要 Apple Silicon Mac、macOS 13+、Xcode Command Line Tools 和 Python 3。首次构建先安装 PyObjC：

```sh
cd vendor/watchdog
/usr/bin/python3 -m venv .venv-mac
.venv-mac/bin/python -m pip install -r requirements-mac.txt
cd ../..
AGENT_RADAR_ALLOW_ADHOC=1 ./scripts/build.sh
./scripts/test.sh
```

开发用 ad-hoc 构建产物位于 `build/任务雷达.app`。正式构建要求已有有效的 `AgentRadar Local` 签名身份；辅助功能授权需针对实际运行的应用复查。测试通过、构建成功和签名有效分别证明自己的检查范围，不能代替目标应用的真实自动发送验收。

源码不包含本机聊天数据库、账户凭据、内部桌面回执或真实会话探针。`Sources/` 是 Swift 应用，`collector/` 是本地会话适配器，`supervisor/` 是续接桥，`tests/` 是隔离检查；测试中的个人路径与真实会话 ID 已替换为通用数据。

## 许可

源码与任务雷达 App 图标按 [MIT License](LICENSE) 开源。`Assets/icons/claude-code.png` 是第三方产品标识，商标及图像权利归原权利人，不在本项目 MIT 授权范围内；来源见 [图标说明](Assets/icons/SOURCES.md)。
