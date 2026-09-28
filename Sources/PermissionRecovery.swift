import Foundation

struct PermissionProbeResult: Equatable {
    enum State: String { case idle, checking, ready, denied, unconfirmed, unavailable }
    var state: State
    var label: String
    var detail: String
    static let idle = Self(state: .idle, label: "未查询", detail: "点击查询读取当前结果")
    static let checking = Self(state: .checking, label: "查询中", detail: "正在进行只读检查…")
    static func window(granted: Bool, apps: Int, windows: Int) -> Self {
        if !granted { return Self(state: .denied, label: "未生效", detail: "当前进程没有获得可用的辅助功能授权。系统开关已开时，可能是旧版本身份或授权缓存不匹配；请按下方步骤重新添加正式版。") }
        if windows > 0 { return Self(state: .ready, label: "实际读取成功", detail: "本次实际读取到 \(apps) 个应用的 \(windows) 个窗口。") }
        return Self(state: .unconfirmed, label: "尚未确认", detail: "系统报告已授权，但本次没有读到可验证窗口。请打开 Finder 等其他应用窗口后重新查询；不能仅凭系统开关认定生效。")
    }
    static func helper(_ code: String) -> Self {
        switch code {
        case "ready": return Self(state: .ready, label: "组件已响应", detail: "恢复组件可启动，且系统报告该组件已授权。此查询不输入、不发送，尚未验证具体会话的输入框。")
        case "permission_required": return Self(state: .denied, label: "组件待授权", detail: "恢复组件已响应，但没有可用的辅助功能授权。请按下方步骤重新添加正式版并重启。")
        case "another_recovery": return Self(state: .unconfirmed, label: "组件忙，待查询", detail: "恢复组件正在处理其他请求；本次没有取得新的权限结果。")
        case "bridge_unavailable": return Self(state: .unavailable, label: "组件未启动", detail: "无法启动恢复组件；这不是已经确认的授权问题。请重启正式版后查询。")
        case "bridge_timeout": return Self(state: .unavailable, label: "组件响应超时", detail: "恢复组件没有按时返回结果，不能据此判断已授权或未授权。请重启正式版后查询。")
        default: return Self(state: .unavailable, label: "组件连接异常", detail: "恢复组件返回 \(code)。这不是已经确认的授权问题；请重启正式版后查询。")
        }
    }
    static func combinedLabel(window: Self, helper: Self) -> String {
        if window.state == .checking || helper.state == .checking { return "查询中" }
        if window.state == .idle || helper.state == .idle { return "未查询" }
        if window.state != .ready { return window.state == .denied ? "未授权" : "未确认" }
        if helper.state == .ready { return "已生效" }
        return helper.state == .denied ? "输入未授权" : "组件异常"
    }
}

struct PermissionRestartPlan {
    static let formalPath = "/Applications/任务雷达.app"
    static let bundleID = "local.agentradar.desktop"
    static let recheckArgument = "--permission-recheck"
    static func arguments(waitingFor pids: [Int32]) -> [String]? {
        guard !pids.isEmpty, pids.allSatisfy({ $0 > 1 }) else { return nil }
        // All PIDs are numeric arguments, never interpolated into shell code.
        // Wait for every known instance to exit; never start a duplicate on timeout.
        let script = """
        receipt="$HOME/Library/Application Support/AgentRadar/permission-restart.log"
        mkdir -p "$HOME/Library/Application Support/AgentRadar"
        /bin/date -u '+%Y-%m-%dT%H:%M:%SZ restart requested' >> "$receipt"
        tries=0
        while :; do
          alive=0
          for pid in "$@"; do
            if kill -0 "$pid" 2>/dev/null; then alive=1; fi
          done
          [ "$alive" -eq 0 ] && break
          tries=$((tries + 1))
          if [ "$tries" -ge 100 ]; then
            printf '%s\n' 'timeout waiting for previous instance; no new instance started' >> "$receipt"
            exit 1
          fi
          sleep 0.1
        done
        /usr/bin/open -a '/Applications/任务雷达.app' --args --permission-recheck
        result=$?
        printf 'open formal app exit=%s\n' "$result" >> "$receipt"
        exit "$result"
        """
        return ["-c", script, "agentradar-permission-restart"] + Set(pids).sorted().map(String.init)
    }
    static func shouldRestartInstance(path: String?, pid: Int32, currentPID: Int32) -> Bool {
        pid == currentPID || path.map { URL(fileURLWithPath: $0).standardizedFileURL.resolvingSymlinksInPath().path == formalPath } == true
    }
}

#if !PERMISSION_RECOVERY_MODEL_ONLY
import AppKit
import SwiftUI

final class PermissionRecoveryController: NSObject, NSWindowDelegate {
    static let shared = PermissionRecoveryController()
    private var window: NSWindow?
    private weak var store: MonitorStore?
    func windowWillClose(_ notification: Notification) { store?.endPermissionRepair() }
    func show(store: MonitorStore) {
        self.store = store
        if let window = window { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true); return }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 480, height: 650), styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "辅助功能自助修复"
        window.minSize = NSSize(width: 450, height: 520)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentView = NSHostingView(rootView: PermissionRecoveryView(store: store))
        window.center(); window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        self.window = window
    }
    var isVisible: Bool { window?.isVisible == true }
}

private struct PermissionRecoveryView: View {
    @ObservedObject var store: MonitorStore
    private var currentPath: String { Bundle.main.bundleURL.path }
    private var version: String {
        let info = Bundle.main.infoDictionary ?? [:]
        return "\(info["CFBundleShortVersionString"] as? String ?? "未知")（\(info["CFBundleVersion"] as? String ?? "未知")）"
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text("辅助功能自助修复").font(.system(size: 18, weight: .semibold))
                Text("修复期间暂停自动发送，关闭此窗口后恢复原设置。")
                    .font(.system(size: 11)).foregroundStyle(.orange)
                Text("开关已开启仍失败时，可能是旧版本代码身份或授权缓存不匹配。下面分别检查实际窗口读取和恢复组件，不需要关闭额外弹窗。")
                    .font(.system(size: 12)).foregroundStyle(.white.opacity(0.8))
                result("窗口读取", store.windowPermissionResult)
                result("输入恢复组件", store.helperPermissionResult)
                HStack {
                    Button(store.accessibilityCheck == .checking ? "查询中…" : "重新查询") { store.verifyAccessibility(reveal: false) }
                        .disabled(store.accessibilityCheck == .checking)
                    Spacer()
                    if let date = store.permissionCheckedAt { Text(date, style: .time).font(.system(size: 10)).foregroundStyle(.secondary) }
                }
                Divider()
                Text("当前运行版本：\(version)").font(.system(size: 12, weight: .medium))
                Text("当前路径：\(currentPath)").font(.system(size: 11)).textSelection(.enabled)
                Text("正式路径：\(PermissionRestartPlan.formalPath)").font(.system(size: 11)).textSelection(.enabled)
                if currentPath != PermissionRestartPlan.formalPath {
                    Text("当前运行的是其他位置的副本，请添加并重启正式路径中的应用。").font(.system(size: 11)).foregroundStyle(.orange)
                }
                Divider()
                Text("按顺序重新绑定当前版本").font(.system(size: 13, weight: .semibold))
                Text("1. 打开系统辅助功能设置。\n2. 选中旧「任务雷达」，点击减号移除。\n3. 点击加号，添加下方正式路径中的应用。\n4. 开启「任务雷达」开关，按系统要求完成认证。\n5. 回到这里，点击「重启正式版并查询」。")
                    .font(.system(size: 12)).lineSpacing(5)
                HStack {
                    Button("打开辅助功能设置") { store.requestPermission() }
                    Button("在 Finder 定位正式版") { store.revealFormalApplication() }
                }
                HStack {
                    Button("复制正式路径") { store.copyFormalApplicationPath() }
                    Spacer()
                    Button("重启正式版并查询") { store.restartFormalApplication() }
                        .disabled(store.permissionRestarting)
                }
                if let message = store.permissionRepairMessage {
                    Text(message).font(.system(size: 11)).foregroundStyle(.white.opacity(0.85)).textSelection(.enabled)
                }
                Text("完成系统设置后，重启并重新查询；具体会话仍需核验输入框。").font(.system(size: 10)).foregroundStyle(.secondary)
            }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
        }.buttonStyle(RadarButtonStyle(inset: 8))
            .foregroundStyle(.white).preferredColorScheme(.dark)
            .background(Color(nsColor: .windowBackgroundColor))
    }
    private func result(_ title: String, _ value: PermissionProbeResult) -> some View {
        let color: Color = value.state == .ready ? .green : (value.state == .checking || value.state == .idle ? .white.opacity(0.7) : .red)
        return VStack(alignment: .leading, spacing: 5) {
            HStack { Text(title).fontWeight(.semibold); Spacer(); Text(value.label).foregroundStyle(color) }
            Text(value.detail).font(.system(size: 11)).foregroundStyle(.white.opacity(0.8)).fixedSize(horizontal: false, vertical: true)
        }.font(.system(size: 12)).padding(12)
            .background(RoundedRectangle(cornerRadius: 10).fill(color.opacity(0.07)))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(color.opacity(0.3), lineWidth: 0.6))
    }
}
#endif
