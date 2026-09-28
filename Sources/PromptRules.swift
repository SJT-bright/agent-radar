import Foundation
#if !PROMPT_RULES_MODEL_ONLY
import AppKit
import SwiftUI
#endif

/// 个性化自动继续规则：按原因（意外中断 / 限流唤起）与按会话（完成后跟催）。
/// 只保存在本机 UserDefaults；提示词由用户亲手配置，对话正文不会进入这里。
struct PromptRules: Codable, Equatable {
    var interruptText: String = "刚才中断了，请继续"
    var rateLimitWakeEnabled = false
    var rateLimitWaitMinutes = 5
    var rateLimitText: String = "刚才被限流了，现在请继续"
    var followUps: [String: String] = [:]
    var completionMode: String = "collaboration"
    var ragePrompt: String = Self.defaultRagePrompt
    var appRagePrompts: [String: String] = [:]
    var rageAlternateEvery = 3
    var rageAlternatePrompt: String = Self.defaultRageAlternatePrompt

    static let defaultRagePrompt = "请继续当前项目，先完成尚未完成的工作，再检查已有改动，找出下一项有价值的优化并实施；完成后实际测试，说明改动、验证结果和剩余问题。"
    static let defaultGrokRagePrompt = "请继续当前项目。先完成尚未完成的工作，再检查已有改动，找出下一项有价值的优化并实施；完成后实际测试，说明改动、验证结果和剩余问题。"
    static let defaultRageAlternatePrompt = "请从真实使用者的角度走查当前项目，指出具体体验问题，提出可验证的改进建议并实施优化；完成后实际测试，说明改动、验证结果和剩余问题。"
    private static let previousDefaultRagePrompts = [
        "从使用者角度检查当前项目，提出可验证的优化方案；针对方案仔细调研，反复审查其必要性、可行性与风险；按最终方案实施改进并验证结果，说明证据和剩余问题。",
        "回顾上一轮自己提出的优化建议与实施结果，再从使用者角度检查当前项目并提出可验证的新方案；针对方案仔细调研，反复审查其必要性、可行性与风险；按最终方案实施改进并验证结果，说明证据和剩余问题。"
    ]
    private static let previousDefaultGrokRagePrompt = "请继续当前项目。先完成上一轮尚未完成的工作，再从真实使用者的角度走查关键体验，指出具体问题，提出可验证的改进建议并实施优化；完成后实际测试，说明改动、验证结果和剩余问题。下一轮继续寻找值得改进的地方，不要只停留在建议。"

    private enum CodingKeys: String, CodingKey {
        case interruptText, rateLimitWakeEnabled, rateLimitWaitMinutes, rateLimitText, followUps
        case completionMode, ragePrompt, appRagePrompts, rageAlternateEvery, rageAlternatePrompt
    }

    init() {}

    // 新增字段缺失时逐项补默认值，保留旧版已保存的提示词和会话规则。
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        interruptText = try values.decodeIfPresent(String.self, forKey: .interruptText) ?? interruptText
        rateLimitWakeEnabled = try values.decodeIfPresent(Bool.self, forKey: .rateLimitWakeEnabled) ?? rateLimitWakeEnabled
        rateLimitWaitMinutes = try values.decodeIfPresent(Int.self, forKey: .rateLimitWaitMinutes) ?? rateLimitWaitMinutes
        rateLimitText = try values.decodeIfPresent(String.self, forKey: .rateLimitText) ?? rateLimitText
        followUps = try values.decodeIfPresent([String: String].self, forKey: .followUps) ?? followUps
        completionMode = try values.decodeIfPresent(String.self, forKey: .completionMode) ?? completionMode
        ragePrompt = try values.decodeIfPresent(String.self, forKey: .ragePrompt) ?? ragePrompt
        appRagePrompts = try values.decodeIfPresent([String: String].self, forKey: .appRagePrompts) ?? appRagePrompts
        rageAlternateEvery = try values.decodeIfPresent(Int.self, forKey: .rageAlternateEvery) ?? rageAlternateEvery
        rageAlternatePrompt = try values.decodeIfPresent(String.self, forKey: .rageAlternatePrompt) ?? rageAlternatePrompt
        self = normalized()
    }

    func normalized() -> PromptRules {
        var result = self
        result.completionMode = completionMode == "rage" ? "rage" : "collaboration"
        result.interruptText = Self.sanitize(interruptText)
        result.rateLimitText = Self.sanitize(rateLimitText)
        result.ragePrompt = Self.sanitize(ragePrompt)
        if Self.previousDefaultRagePrompts.contains(result.ragePrompt) {
            result.ragePrompt = Self.defaultRagePrompt
        }
        if result.ragePrompt.isEmpty { result.ragePrompt = Self.defaultRagePrompt }
        result.rageAlternateEvery = min(100, max(2, rageAlternateEvery))
        result.rageAlternatePrompt = Self.sanitize(rageAlternatePrompt)
        if result.rageAlternatePrompt.isEmpty { result.rageAlternatePrompt = Self.defaultRageAlternatePrompt }
        result.rateLimitWaitMinutes = min(60, max(1, rateLimitWaitMinutes))
        result.followUps = followUps.mapValues(Self.sanitize)
        result.appRagePrompts = appRagePrompts.mapValues(Self.sanitize).filter { !$0.value.isEmpty }
        if result.appRagePrompts["grok"] == Self.previousDefaultGrokRagePrompt {
            result.appRagePrompts["grok"] = Self.defaultGrokRagePrompt
        }
        return result
    }

    static let maxLength = 2000
    static let defaultsKey = "promptRules.v1"

    /// 与 supervisor/bridge.py 的校验对齐：只保留换行/制表，
    /// 去掉其他控制字符与首尾空白，超长内容保存前截断并提示。
    static func sanitize(_ text: String) -> String {
        let scalars = text.unicodeScalars.filter { scalar in
            if scalar == "\n" || scalar == "\t" { return true }
            return scalar.value >= 32 && scalar.value != 127
        }
        var result = String(String.UnicodeScalarView(scalars))
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if result.unicodeScalars.count > maxLength {
            // Python validates Unicode code points. Keep each grapheme intact
            // while respecting the same scalar limit across the bridge.
            var kept = ""
            var scalarCount = 0
            for character in result {
                let width = String(character).unicodeScalars.count
                guard scalarCount + width <= maxLength else { break }
                kept.append(character)
                scalarCount += width
            }
            result = kept
        }
        return result
    }

    static func load(from defaults: UserDefaults = .standard) -> PromptRules {
        guard let data = defaults.data(forKey: defaultsKey),
              let rules = try? JSONDecoder().decode(PromptRules.self, from: data) else { return PromptRules() }
        return rules
    }

    func save(to defaults: UserDefaults = .standard) {
        if let data = try? JSONEncoder().encode(normalized()) {
            defaults.set(data, forKey: Self.defaultsKey)
        }
    }

    func followUp(for sessionID: String) -> String? {
        guard let text = followUps[sessionID]?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty else { return nil }
        return text
    }

    func ragePrompt(forApp appID: String) -> String {
        appRagePrompts[appID] ?? (appID == "grok" ? Self.defaultGrokRagePrompt : ragePrompt)
    }
}

#if !PROMPT_RULES_MODEL_ONLY
/// 全局设置与单会话完成提示词共用的小编辑窗。保存立即生效并落盘。
final class PromptSettingsController {
    static let shared = PromptSettingsController()
    private var window: NSWindow?

    func close() { window?.close(); window = nil }

    func showGlobal(store: MonitorStore) { present(store: store, session: nil) }
    func showFollowUp(store: MonitorStore, session: SessionRecord) { present(store: store, session: session) }

    private func present(store: MonitorStore, session: SessionRecord?) {
        if let window = window { window.close(); self.window = nil }
        let hosting = NSHostingView(rootView: PromptEditorView(store: store, session: session))
        // 固有内容高度会把窗口撑到屏幕外，保存按钮随之离开可视区域。
        // 固定由窗口给出尺寸，正文在 ScrollView 内滚动。
        hosting.sizingOptions = []
        let title = session.map { "保留的会话提示词 · \($0.app_name)" } ?? "运行模式与提示词"
        let size = NSSize(width: 420, height: session == nil ? 640 : 380)
        hosting.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = title
        window.level = .floating
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentMinSize = NSSize(width: 360, height: 280)
        window.contentView = hosting
        window.setContentSize(size)
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.window = window
    }
}

/// 设置窗内的玻璃感输入框与悬停浮起按钮。
private struct EditorBox: View {
    init(_ text: Binding<String>, height: CGFloat) {
        _text = text
        self.height = height
    }
    @Binding var text: String
    var height: CGFloat
    @State private var hovered = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TextEditor(text: $text)
            .scrollContentBackground(.hidden)
            .font(.system(size: 12))
            .padding(6)
            .frame(height: height)
            .background(RoundedRectangle(cornerRadius: 8)
                .fill(.white.opacity(hovered ? 0.09 : 0.06)))
            .overlay(RoundedRectangle(cornerRadius: 8)
                .strokeBorder(.white.opacity(hovered ? 0.22 : 0.12), lineWidth: 0.6))
            .onHover { hovered = $0 }
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.15), value: hovered)
    }
}

private struct CapsuleButton: View {
    let title: String
    var destructive = false
    var disabled = false
    let action: () -> Void
    @State private var hovered = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button(action: action) {
            Text(title).font(.system(size: 12, weight: .medium))
                .foregroundStyle(.white.opacity(disabled ? 0.4 : 1))
                .padding(.horizontal, 14).frame(height: 26)
                .background(Capsule().fill((destructive ? Color.red : .white)
                    .opacity(hovered && !disabled ? 0.32 : 0.14)))
                .overlay(Capsule().strokeBorder(.white.opacity(hovered && !disabled ? 0.4 : 0.15), lineWidth: 0.6))
                .shadow(color: .white.opacity(hovered && !disabled ? 0.18 : 0), radius: 4)
                .scaleEffect(hovered && !disabled && !reduceMotion ? 1.05 : 1)
                .contentShape(Capsule())
        }.buttonStyle(RadarButtonStyle(cornerRadius: 14))
            .onHover { hovered = $0 }
            .animation(reduceMotion ? nil : .spring(response: 0.22, dampingFraction: 1), value: hovered)
            .disabled(disabled)
    }
}

private struct PromptEditorView: View {
    @ObservedObject var store: MonitorStore
    let session: SessionRecord?
    @State private var interruptText: String
    @State private var rateOn: Bool
    @State private var waitMinutes: Double
    @State private var rateText: String
    @State private var followUpText: String
    @State private var ragePrompt: String
    @State private var grokRagePrompt: String
    @State private var rageAlternateEvery: Int
    @State private var rageAlternatePrompt: String
    @State private var savedMessage = ""
    @State private var showCompletedForSupervision = false

    init(store: MonitorStore, session: SessionRecord?) {
        self.store = store
        self.session = session
        _ragePrompt = State(initialValue: store.promptRules.ragePrompt)
        _grokRagePrompt = State(initialValue: store.promptRules.ragePrompt(forApp: "grok"))
        _rageAlternateEvery = State(initialValue: store.promptRules.rageAlternateEvery)
        _rageAlternatePrompt = State(initialValue: store.promptRules.rageAlternatePrompt)
        _interruptText = State(initialValue: store.promptRules.interruptText)
        _rateOn = State(initialValue: store.promptRules.rateLimitWakeEnabled)
        _waitMinutes = State(initialValue: Double(store.promptRules.rateLimitWaitMinutes))
        _rateText = State(initialValue: store.promptRules.rateLimitText)
        _followUpText = State(initialValue: session.flatMap { store.promptRules.followUps[$0.id] } ?? "")
    }

    var body: some View {
        ScrollView {
          VStack(alignment: .leading, spacing: 12) {
            if let session = session {
                Text("\(session.app_name)：\(session.title)").font(.system(size: 11, weight: .medium))
                    .lineLimit(2).help(session.title)
                Text("当前运行模式优先：协作模式只提醒验收；狂暴模式按普通与周期特别提示词续接。这里保留旧版会话提示词，保存不代表会自动发送。")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                EditorBox($followUpText, height: 90)
                HStack {
                    Text("\(followUpText.unicodeScalars.count)/\(PromptRules.maxLength)").font(.system(size: 9)).foregroundStyle(.secondary)
                    Spacer()
                    if store.promptRules.followUp(for: session.id) != nil {
                        CapsuleButton(title: "移除此设置", destructive: true) {
                            store.setFollowUp(nil, for: session)
                            PromptSettingsController.shared.close()
                        }
                    }
                    CapsuleButton(title: "保存",
                                  disabled: followUpText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) {
                        store.setFollowUp(followUpText, for: session)
                        PromptSettingsController.shared.close()
                    }
                }
            } else {
                Text("运行模式").font(.system(size: 12, weight: .semibold))
                HStack(spacing: 6) {
                    modeButton("协作模式", mode: "collaboration", color: .blue)
                    modeButton("狂暴模式", mode: "rage", color: .red)
                }
                Text(isRage
                     ? "可按软件监督雷达识别到的当前及未来会话，也可单独选择会话；不同软件可同时开启，无总轮数上限。每条会话独立计数，按设定轮换普通与特别续接文字。"
                     : "每轮明确完成后提醒「这个项目已完结，请验收」。完成时不发送提示词；本轮结束仍需你验收项目。")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                Text(isRage
                     ? "仅在原会话明确结束或意外中断、有新鲜证据且可核验输入位置时自动续接；不再判断回答内容是否完成。"
                     : "无法核验本轮结束或会话身份时只提醒。")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                Toggle("自动发送总开关", isOn: Binding(
                    get: { store.autoContinueEnabled }, set: { store.setAutoContinue($0) })).radarHoverHighlight()
                Text(store.autoContinueEnabled
                     ? "已开启：核验原会话后覆盖输入框草稿并发送。关闭立即取消待发送任务。"
                     : "仅提醒：不切换软件、不输入、不发送。单次点击恢复不改变此开关。")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                Divider()
                supervisionSelector
                Divider()
                if isRage {
                    Text("普通续接提示词").font(.system(size: 11, weight: .semibold))
                    EditorBox($ragePrompt, height: 100)
                    Text("\(ragePrompt.unicodeScalars.count)/\(PromptRules.maxLength)").font(.system(size: 9)).foregroundStyle(.secondary)
                    CapsuleButton(title: "恢复默认优化文字") { ragePrompt = PromptRules.defaultRagePrompt }
                    Text("Grok 普通续接提示词").font(.system(size: 11, weight: .semibold))
                    EditorBox($grokRagePrompt, height: 100)
                    Text("\(grokRagePrompt.unicodeScalars.count)/\(PromptRules.maxLength) · 仅用于 Grok 的普通续接")
                        .font(.system(size: 9)).foregroundStyle(.secondary)
                    CapsuleButton(title: "恢复 Grok 默认文字") { grokRagePrompt = PromptRules.defaultGrokRagePrompt }
                    Divider()
                    Stepper("每条会话每 \(rageAlternateEvery) 次续接，发送 1 次特别提示词", value: $rageAlternateEvery, in: 2...100)
                        .font(.system(size: 11)).radarHoverHighlight()
                    Text("例如设为 3：第 1、2 次发送普通提示词，第 3 次发送下方特别提示词；每条会话分别计数。")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                    Text("周期特别提示词").font(.system(size: 11, weight: .semibold))
                    EditorBox($rageAlternatePrompt, height: 100)
                    Text("\(rageAlternatePrompt.unicodeScalars.count)/\(PromptRules.maxLength)")
                        .font(.system(size: 9)).foregroundStyle(.secondary)
                    CapsuleButton(title: "恢复默认特别文字") { rageAlternatePrompt = PromptRules.defaultRageAlternatePrompt }
                    Text("启动或切换模式不会自动选择会话，也不会补发历史任务。已完成会话需先标记，再点击下面按钮；仍会逐条核验是否可以发送。")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                    CapsuleButton(title: "启动已标记的完成会话", disabled: !store.autoContinueEnabled || store.markedCompletedCount == 0) {
                        saveRules()
                        store.startRageForCompleted()
                        savedMessage = "已请求核验已标记的完成会话；实际排队与跳过原因见恢复提醒。"
                    }
                    .help(store.autoContinueEnabled
                          ? (store.markedCompletedCount == 0
                             ? "先标记已完成会话，此按钮才会把这些旧轮次加入续接队列"
                             : "保存当前续接提示词，并只为已标记且可核验的已完成会话排队")
                          : "狂暴模式需要先开启自动发送总开关，此按钮才会执行")
                } else {
                    Text("意外中断提示词").font(.system(size: 11, weight: .semibold))
                    EditorBox($interruptText, height: 56)
                    CapsuleButton(title: "恢复默认文字") { interruptText = "刚才中断了，请继续" }
                }
                Toggle("被限流时也自动唤起（等待后重试）", isOn: $rateOn).radarHoverHighlight()
                if rateOn {
                    Stepper("等待 \(Int(waitMinutes)) 分钟后再发送", value: $waitMinutes, in: 1...60).radarHoverHighlight()
                    if isRage {
                        Text("限流等待结束后按各会话的续接次数发送普通或特别提示词。")
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                    } else {
                        EditorBox($rateText, height: 56)
                    }
                }
                Text("主动停止、等待确认、未知状态不自动恢复；认证、权限和上下文问题需手动处理。同一轮最多发送一次；狂暴模式不设小时次数上限。提示词最长 \(PromptRules.maxLength) 字，保存时清除控制字符并截断超长内容。模式与总开关立即生效，其余内容点击保存后生效。")
                    .font(.system(size: 9.5)).foregroundStyle(.secondary)
                if !savedMessage.isEmpty {
                    Text(savedMessage).font(.system(size: 10)).foregroundStyle(.secondary)
                        .accessibilityLabel(savedMessage)
                }
                HStack {
                    Spacer()
                    CapsuleButton(title: "保存并关闭") {
                        saveRules()
                        PromptSettingsController.shared.close()
                    }
                }

            }
          }.padding(14).frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var supervisionSelector: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("持续监督 · \(store.supervisedAppIDs.count) 个软件 / \(store.supervisedSessionIDs.count) 条单选 · 当前覆盖 \(store.effectiveSupervisedIDs.count) 条")
                .font(.system(size: 11, weight: .semibold))
            Text(store.supervisedAppIDs.isEmpty && store.supervisedSessionIDs.isEmpty
                 ? "选择软件即可覆盖它当前及今后发现的可核验对话，也可以逐条选择。切换模式不会自动选中软件。"
                 : "选择立即保存；协作模式保留选择但不持续优化。关闭软件监督会停止该软件的自动覆盖，单独选择仍保留。")
                .font(.system(size: 10)).foregroundStyle(.secondary)
            Toggle("显示已完成会话，供手动启动", isOn: $showCompletedForSupervision).radarHoverHighlight()
                .font(.system(size: 10))
            let groups = store.supervisionGroups(includeCompleted: showCompletedForSupervision)
            if groups.isEmpty {
                Text("当前没有可选择的正在运行会话。已完成会话可通过上方开关查看。")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            }
            ForEach(groups) { group in
                VStack(alignment: .leading, spacing: 5) {
                    Toggle(isOn: Binding(get: { store.isAppSupervised(group.id) },
                                         set: { store.setAppSupervision($0, appID: group.id) })) {
                        Text("监督 \(group.primary.app_name) 的全部对话")
                            .font(.system(size: 10, weight: .semibold))
                    }.toggleStyle(.checkbox).radarHoverHighlight()
                        .accessibilityLabel(group.primary.app_name + "：监督全部对话")
                    if store.isAppSupervised(group.id) {
                        Text("自动覆盖新对话；旧的已完成轮次仅在显式启动后处理。每条发送前仍须核验，无法定位输入框的应用只提醒。")
                            .font(.system(size: 9)).foregroundStyle(.secondary)
                        if let reason = ContinuationPolicy.inputRestriction(group.primary) {
                            Text(reason).font(.system(size: 9)).foregroundStyle(.secondary)
                        }
                    }
                    ForEach(group.sessions) { row in
                        Toggle(isOn: Binding(get: { store.isSupervised(row) },
                                             set: { store.setSupervision($0, for: row) })) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(row.title).font(.system(size: 10)).fixedSize(horizontal: false, vertical: true)
                                Text(row.statusLabel).font(.system(size: 9)).foregroundStyle(.secondary)
                                if let reason = ContinuationPolicy.followupRestriction(row) {
                                    Text(reason).font(.system(size: 9)).foregroundStyle(.secondary)
                                }
                            }.help(row.title + "\n" + store.supervisionDetail(for: row))
                        }.toggleStyle(.checkbox).radarHoverHighlight()
                            .disabled(store.isAppSupervised(group.id))
                            .accessibilityLabel(group.primary.app_name + "：" + row.title + "，持续监督")
                    }
                }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 8).fill(.white.opacity(0.055)))
            }
            let missingApps = store.supervisedAppIDs.subtracting(Set(groups.map(\.id))).sorted()
            ForEach(missingApps, id: \.self) { appID in
                Toggle(isOn: Binding(get: { store.isAppSupervised(appID) },
                                     set: { store.setAppSupervision($0, appID: appID) })) {
                    Text("监督 \(store.apps.first(where: { $0.id == appID })?.name ?? appID) 的全部对话 · 当前无可读会话")
                        .font(.system(size: 10))
                }.toggleStyle(.checkbox).radarHoverHighlight()
            }
            let shownIDs = Set(groups.flatMap { $0.sessions.map(\.id) })
            let unavailable = store.supervisedSessionIDs.subtracting(shownIDs).count
            if unavailable > 0 {
                Text("另有 \(unavailable) 条已标记会话当前不可读取；标记保留，读取恢复后再核验。")
                    .font(.system(size: 9)).foregroundStyle(.secondary)
            }
        }
    }

    private func modeButton(_ title: String, mode: String, color: Color) -> some View {
        let selected = store.promptRules.completionMode == mode
        return Button {
            guard !selected else { return }
            var rules = store.promptRules
            rules.completionMode = mode
            store.applyRules(rules.normalized())
            savedMessage = "模式已切换，旧模式的待发送任务已取消。"
        } label: {
            HStack(spacing: 5) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                Text(title)
            }.font(.system(size: 12, weight: .semibold))
                .frame(maxWidth: .infinity).padding(.vertical, 7)
                .background(RoundedRectangle(cornerRadius: 7).fill(color.opacity(selected ? 0.35 : 0.10)))
        }.buttonStyle(RadarButtonStyle())
            .accessibilityValue(selected ? "已选择" : "未选择")
    }

    private var isRage: Bool { store.promptRules.completionMode == "rage" }

    private func saveRules() {
        var rules = store.promptRules
        rules.interruptText = interruptText
        rules.rateLimitWakeEnabled = rateOn
        rules.rateLimitWaitMinutes = Int(waitMinutes)
        rules.rateLimitText = rateText
        rules.ragePrompt = ragePrompt
        rules.appRagePrompts["grok"] = grokRagePrompt
        rules.rageAlternateEvery = rageAlternateEvery
        rules.rageAlternatePrompt = rageAlternatePrompt
        store.applyRules(rules.normalized())
    }
}
#endif
