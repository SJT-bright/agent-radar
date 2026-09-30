# 任务雷达（Agent Radar）

原生 macOS AI 任务悬浮窗。当前公开源码对应 **1.8.1（build 55）**。它从本机可读取的会话状态和窗口信息中显示当前对话、运行时间、完成与中断状态，不需要 AI API Key。

## 功能

- 集中查看 Codex、Claude Code、ZCode、WorkBuddy、AutoClaw、Qoder、Grok、Cline 等应用的可识别会话；点击卡片可跳转到相应应用或工作区。
- 显示本轮计时、完成或中断原因。缺少可靠证据时保留待确认状态，不把应用正在运行等同于会话正在运行。
- 可折叠的置顶浮窗，支持按应用汇总、移除和恢复会话、透明度与磨砂设置。完成待验收提醒有紧凑布局与右上角跳转图标，可按已读取的会话身份打开原对话；左上角旋转图标负责单次续接。
- 协作模式提醒用户验收。显式开启自动发送并选择监督范围后，可对可核验的新轮次按规则续接；发送前核对会话身份、轮次和输入框，同一轮防止重复发送。不同应用的自动续接支持范围和实际验收程度不同。
- 提供辅助功能自检及夜间防空闲休眠开关。监控和状态处理在本机完成。
- 设置页按运行、监督、提示词和限流分区，支持监督范围搜索与折叠、草稿保护和快捷保存。提示词可用 ⌘V 粘贴；超过 2000 字符时保留全部草稿、标红字数，删减到上限内再保存。
- 前台支持的 AI 应用出现唯一且可点击的新「插队」按钮时可自动点击一次；可从菜单关闭，暂停监控时停止。内置专属 App 图标。

## 构建

需要 Apple Silicon Mac、macOS 13+、Xcode Command Line Tools 和 Python 3。首次构建前安装 PyObjC 依赖：

```sh
cd vendor/watchdog
/usr/bin/python3 -m venv .venv-mac
.venv-mac/bin/python -m pip install -r requirements-mac.txt
cd ../..
./scripts/test.sh
AGENT_RADAR_ALLOW_ADHOC=1 ./scripts/build.sh
```

开发用 ad-hoc 构建产物在 `build/任务雷达.app`。正式使用建议保留稳定的代码签名身份；辅助功能授权需针对实际运行的应用完成并复查。构建成功和自动化测试通过不代表所有目标应用的真实自动发送都已验证。

本项目不包含本机聊天数据库、账户凭据、运行回执或安装包。`Sources/` 为 Swift 应用，`collector/` 为只读会话适配器，`supervisor/` 为续接桥，`tests/` 为自动化检查。

## 许可

源码与任务雷达 App 图标按 [MIT License](LICENSE) 开源。`Assets/icons/claude-code.png` 是第三方产品标识，商标及图像权利归原权利人，不在本项目 MIT 授权范围内；来源见 [图标说明](Assets/icons/SOURCES.md)。
