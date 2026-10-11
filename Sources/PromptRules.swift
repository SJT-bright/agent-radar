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
    var rateLimitWaitSeconds = 10
    var rateLimitText: String = "刚才被限流了，现在请继续"
    var followUps: [String: String] = [:]
    var completionMode: String = "collaboration"
    var ragePrompt: String = Self.defaultRagePrompt
    var appRagePrompts: [String: String] = [:]
    var appResumeTexts: [String: String] = [:]
    var resumePrompt: String = ""
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
        case interruptText, rateLimitWakeEnabled, rateLimitWaitSeconds, rateLimitWaitMinutes, rateLimitText, followUps
        case completionMode, ragePrompt, appRagePrompts, appResumeTexts, resumePrompt, rageAlternateEvery, rageAlternatePrompt
    }

    init() {}

    // 新增字段缺失时逐项补默认值，保留旧版已保存的提示词和会话规则。
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        interruptText = try values.decodeIfPresent(String.self, forKey: .interruptText) ?? interruptText
        rateLimitWakeEnabled = try values.decodeIfPresent(Bool.self, forKey: .rateLimitWakeEnabled) ?? rateLimitWakeEnabled
        if let seconds = try values.decodeIfPresent(Int.self, forKey: .rateLimitWaitSeconds) {
            rateLimitWaitSeconds = seconds
        } else if let minutes = try values.decodeIfPresent(Int.self, forKey: .rateLimitWaitMinutes) {
            // Old minute waits migrate to the newly requested ten-second ceiling.
            rateLimitWaitSeconds = minutes > 0 ? 10 : 1
        }
        rateLimitText = try values.decodeIfPresent(String.self, forKey: .rateLimitText) ?? rateLimitText
        followUps = try values.decodeIfPresent([String: String].self, forKey: .followUps) ?? followUps
        completionMode = try values.decodeIfPresent(String.self, forKey: .completionMode) ?? completionMode
        ragePrompt = try values.decodeIfPresent(String.self, forKey: .ragePrompt) ?? ragePrompt
        appRagePrompts = try values.decodeIfPresent([String: String].self, forKey: .appRagePrompts) ?? appRagePrompts
        appResumeTexts = try values.decodeIfPresent([String: String].self, forKey: .appResumeTexts) ?? appResumeTexts
        resumePrompt = try values.decodeIfPresent(String.self, forKey: .resumePrompt) ?? resumePrompt
        rageAlternateEvery = try values.decodeIfPresent(Int.self, forKey: .rageAlternateEvery) ?? rageAlternateEvery
        rageAlternatePrompt = try values.decodeIfPresent(String.self, forKey: .rageAlternatePrompt) ?? rageAlternatePrompt
        self = normalized()
    }

    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(interruptText, forKey: .interruptText)
        try values.encode(rateLimitWakeEnabled, forKey: .rateLimitWakeEnabled)
        try values.encode(rateLimitWaitSeconds, forKey: .rateLimitWaitSeconds)
        try values.encode(rateLimitText, forKey: .rateLimitText)
        try values.encode(followUps, forKey: .followUps)
        try values.encode(completionMode, forKey: .completionMode)
        try values.encode(ragePrompt, forKey: .ragePrompt)
        try values.encode(appRagePrompts, forKey: .appRagePrompts)
        try values.encode(appResumeTexts, forKey: .appResumeTexts)
        try values.encode(resumePrompt, forKey: .resumePrompt)
        try values.encode(rageAlternateEvery, forKey: .rageAlternateEvery)
        try values.encode(rageAlternatePrompt, forKey: .rageAlternatePrompt)
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
        result.rateLimitWaitSeconds = min(10, max(1, rateLimitWaitSeconds))
        result.followUps = followUps.mapValues(Self.sanitize)
        result.appRagePrompts = appRagePrompts.mapValues(Self.sanitize).filter { !$0.value.isEmpty }
        result.appResumeTexts = appResumeTexts.mapValues(Self.sanitize).filter { !$0.value.isEmpty }
        result.resumePrompt = Self.sanitize(resumePrompt)
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

    /// 按软件的中断恢复文字：专属 > 全局自定义 > 固定默认（策略层常量）。
    /// 字典值与全局值允许留空表示「用下一级」；解析在 ContinuationPolicy 完成。
    static let appDisplayNames = ["workbuddy": "WorkBuddy", "workbuddy-ai": "WorkBuddy AI",
                                  "zcode": "ZCode", "autoclaw": "AutoClaw", "grok": "Grok", "codex": "Codex",
                                  "qoder": "Qoder", "qoder-cn": "Qoder CN"]
}

#if !PROMPT_RULES_MODEL_ONLY
/// A draft belongs to the window, so switching pages and reopening the same
/// settings entry cannot throw away edits. Live switches stay in MonitorStore.
private final class PromptEditorState: ObservableObject {
    @Published var draft: PromptSettingsDraft
    @Published var followUpText: String
    @Published var savedMessage = ""
    let store: MonitorStore
    let session: SessionRecord?

    init(store: MonitorStore, session: SessionRecord?) {
        self.store = store
        self.session = session
        draft = PromptSettingsDraft(rules: store.promptRules, reminderPopupEnabled: store.reminderPopupEnabled,
                                    reminderSoundEnabled: store.reminderSoundEnabled)
        followUpText = session.flatMap { store.promptRules.followUps[$0.id] } ?? ""
    }

    var hasChanges: Bool {
        if let session = session {
            return followUpText != (store.promptRules.followUps[session.id] ?? "")
        }
        return draft.hasChanges(comparedTo: store.promptRules, reminderPopupEnabled: store.reminderPopupEnabled,
                                reminderSoundEnabled: store.reminderSoundEnabled)
    }

    var validationMessages: [String] {
        if session != nil {
            return followUpText.unicodeScalars.count > PromptRules.maxLength
                ? ["会话提示词超过 \(PromptRules.maxLength) 字符；完整内容已保留，请删减后保存。"] : []
        }
        return draft.validationMessages
    }

    var hasOverlengthPrompt: Bool {
        session != nil ? followUpText.unicodeScalars.count > PromptRules.maxLength : draft.hasOverlengthPrompt
    }

    @discardableResult func save() -> Bool {
        guard !hasOverlengthPrompt else {
            savedMessage = "提示词超出 2000 字符，请删减后保存。"
            return false
        }
        let adjusted = !validationMessages.isEmpty
        if let session = session {
            store.setFollowUp(followUpText, for: session)
        } else {
            store.applySettingsDraft(draft)
        }
        reload()
        savedMessage = adjusted ? "已保存并处理上方提示的问题，可查看保存后的文字。" : "已保存，后续任务使用最新设置。"
        return true
    }

    func reload() {
        draft = PromptSettingsDraft(rules: store.promptRules, reminderPopupEnabled: store.reminderPopupEnabled,
                                    reminderSoundEnabled: store.reminderSoundEnabled)
        followUpText = session.flatMap { store.promptRules.followUps[$0.id] } ?? ""
        savedMessage = ""
    }
}

final class PromptSettingsController: NSObject, NSWindowDelegate {
    static let shared = PromptSettingsController()
    private var window: NSWindow?
    private var editor: PromptEditorState?

    func close() { window?.performClose(nil) }
    func showGlobal(store: MonitorStore) { present(store: store, session: nil) }
    func showFollowUp(store: MonitorStore, session: SessionRecord) { present(store: store, session: session) }

    func windowShouldClose(_ sender: NSWindow) -> Bool { approveClosingDraft() }
    func windowWillClose(_ notification: Notification) {
        window = nil
        editor = nil
    }

    private func approveClosingDraft() -> Bool {
        guard let editor = editor, editor.hasChanges else { return true }
        let alert = NSAlert()
        alert.messageText = "保存未保存的修改？"
        alert.informativeText = "提醒、提示词与限流设置尚未保存。运行开关和监督选择已即时生效。"
        if !editor.validationMessages.isEmpty {
            alert.informativeText += "\n\n" + editor.validationMessages.joined(separator: "\n")
        }
        alert.addButton(withTitle: "保存")
        alert.addButton(withTitle: "继续编辑")
        alert.addButton(withTitle: "不保存")
        alert.window.level = .floating
        switch alert.runModal() {
        case .alertFirstButtonReturn: return editor.save()
        case .alertThirdButtonReturn: return true
        default: return false
        }
    }

    private func present(store: MonitorStore, session: SessionRecord?) {
        PromptEditMenu.install()
        if let window = window, editor?.store === store, editor?.session?.id == session?.id {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        guard approveClosingDraft() else { return }
        if let window = window { window.close() }
        let editor = PromptEditorState(store: store, session: session)
        self.editor = editor
        let hosting = NSHostingView(rootView: PromptEditorView(store: store, editor: editor))
        hosting.sizingOptions = []
        let size = NSSize(width: 560, height: session == nil ? 660 : 400)
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = session.map { "保留的会话提示词 · \($0.app_name)" } ?? "任务雷达设置"
        window.level = .floating
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentMinSize = NSSize(width: 440, height: 400)
        window.contentView = hosting
        window.setContentSize(size)
        window.delegate = self
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        self.window = window
    }
}

/// Accessory apps have no default Edit menu. TextEditor needs the standard
/// responder actions for Command-V/C/X/A to reach its NSTextView.
private enum PromptEditMenu {
    static func install() {
        let main = NSApp.mainMenu ?? NSMenu()
        if main.items.contains(where: { $0.identifier?.rawValue == "AgentRadarEditMenu" }) { return }
        let item = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        item.identifier = NSUserInterfaceItemIdentifier("AgentRadarEditMenu")
        let edit = NSMenu(title: "编辑")
        edit.addItem(NSMenuItem(title: "撤销", action: Selector(("undo:")), keyEquivalent: "z"))
        edit.addItem(NSMenuItem(title: "重做", action: Selector(("redo:")), keyEquivalent: "Z"))
        edit.addItem(.separator())
        edit.addItem(NSMenuItem(title: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        edit.addItem(NSMenuItem(title: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        edit.addItem(NSMenuItem(title: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        edit.addItem(NSMenuItem(title: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        item.submenu = edit
        main.addItem(item)
        NSApp.mainMenu = main
    }
}

private struct EditorBox: View {
    @Binding var text: String
    var label: String
    var height: CGFloat = 100

    private var characterCount: Int { text.unicodeScalars.count }
    private var isOverLimit: Bool { characterCount > PromptRules.maxLength }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            TextEditor(text: $text)
                .scrollContentBackground(.hidden)
                .font(.system(size: 12))
                .padding(6).frame(height: height)
                .background(RoundedRectangle(cornerRadius: 8).fill(.white.opacity(0.06)))
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(.white.opacity(0.16), lineWidth: 0.6))
                .accessibilityLabel(label)
            Text("\(characterCount)/\(PromptRules.maxLength) 字符" +
                 (isOverLimit ? " · 超出 \(characterCount - PromptRules.maxLength) 字符，请删减后保存" : ""))
                .font(.system(size: 10))
                .foregroundStyle(isOverLimit ? Color.red : Color.secondary)
        }
    }
}

private struct CapsuleButton: View {
    let title: String
    var destructive = false
    var disabled = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title).font(.system(size: 12, weight: .medium))
                .padding(.horizontal, 12).frame(height: 28)
                .background(Capsule().fill((destructive ? Color.red : .white).opacity(0.14)))
                .contentShape(Capsule())
        }
        .buttonStyle(RadarButtonStyle(cornerRadius: 14))
        .disabled(disabled)
    }
}

private enum SettingsPage: String, CaseIterable, Identifiable {
    case operation = "运行", supervision = "监督", prompts = "提示词", rateLimit = "限流"
    var id: String { rawValue }
}

private struct PromptEditorView: View {
    @ObservedObject var store: MonitorStore
    @ObservedObject var editor: PromptEditorState
    @State private var page: SettingsPage = .operation

    var body: some View {
        VStack(spacing: 0) {
            if editor.session == nil {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text("任务雷达设置").font(.system(size: 17, weight: .semibold))
                        Spacer()
                        Text(store.paused ? "监控已暂停" : (store.autoContinueEnabled ? "自动发送已开启" : "仅提醒"))
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(store.paused ? Color.orange : Color.secondary)
                    }
                    Picker("设置分类", selection: $page) {
                        ForEach(SettingsPage.allCases) { Text($0.rawValue).tag($0) }
                    }.pickerStyle(.segmented).labelsHidden()
                }.padding(16)
                Divider()
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let session = editor.session {
                        legacyEditor(session)
                    } else {
                        switch page {
                        case .operation: operationPage
                        case .supervision:
                            SupervisionSettingsView(store: store)
                            if isRage { historicalStart }
                        case .prompts: promptsPage
                        case .rateLimit: rateLimitPage
                        }
                    }
                }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            saveBar
        }
    }

    private var operationPage: some View {
        VStack(alignment: .leading, spacing: 14) {
            sectionTitle("运行模式", detail: "模式、自动发送和常用功能立即生效；提醒设置点击底部保存后生效。")
            HStack(spacing: 8) {
                modeButton("协作模式", mode: "collaboration", color: .blue)
                modeButton("狂暴模式", mode: "rage", color: .red)
            }
            Text(isRage
                 ? "监督范围内的新鲜轮次明确结束或意外中断后，核验原会话并按次数轮换普通与特别提示词。每条会话独立计数，无总轮数上限。"
                 : "本轮明确完成后提醒「这个项目已完结，请验收」。意外中断仅在开启自动发送后尝试恢复。")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            Toggle("自动发送总开关", isOn: Binding(get: { store.autoContinueEnabled }, set: { store.setAutoContinue($0) }))
                .radarHoverHighlight()
            Text(store.autoContinueEnabled
                 ? "核验原会话后覆盖输入框草稿并发送；关闭会取消待发送任务。"
                 : "仅提醒。单次点击恢复不改变此开关。")
                .font(.system(size: 10)).foregroundStyle(.secondary)
            if isRage {
                Button("管理监督范围 · \(store.supervisedAppIDs.count) 个软件 / \(store.supervisedSessionIDs.count) 条单选") { page = .supervision }
                    .buttonStyle(RadarButtonStyle()).font(.system(size: 11))
                if store.supervisedAppIDs.isEmpty && store.supervisedSessionIDs.isEmpty {
                    Text("尚未选择监督范围；开启总开关也不会自动选择会话。")
                        .font(.system(size: 10)).foregroundStyle(.orange)
                }
            }
            Divider()
            sectionTitle("提醒", detail: "修改后点击底部保存；弹窗和音效可分别设置。")
            Toggle("显示提醒弹窗", isOn: $editor.draft.reminderPopupEnabled)
                .radarHoverHighlight()
                .accessibilityIdentifier("settings.reminderPopupEnabled")
            Toggle("任务提醒音效", isOn: $editor.draft.reminderSoundEnabled)
                .radarHoverHighlight()
                .accessibilityIdentifier("settings.reminderSoundEnabled")
            Text("开启音效后，提醒出现或桌面对话的新一轮任务完成时会发声。关闭弹窗后，任务完成仍会发声；暂停监控期间不触发提醒。")
                .font(.system(size: 10)).foregroundStyle(.secondary)
            HStack {
                Button("试听音效") { store.previewReminderSound() }
                    .buttonStyle(RadarButtonStyle()).font(.system(size: 11))
                    .help("播放当前任务提醒音效，不修改设置")
                    .accessibilityIdentifier("settings.previewReminderSound")
                Text("试听会直接播放，无需保存。")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            }
            Divider()
            sectionTitle("常用功能", detail: "按入队顺序逐条处理对话，暂停监控时停止自动操作。")
            Toggle("暂停监控", isOn: Binding(get: { store.paused }, set: { if $0 != store.paused { store.togglePause() } }))
                .radarHoverHighlight()
            Text("对话按顺序定位、核验和发送，重试保留原位，不自动点击插队。")
                .font(.system(size: 10)).foregroundStyle(.secondary)
            Toggle("夜间常亮（防休眠）", isOn: Binding(get: { store.keepAwakeEnabled }, set: { if $0 != store.keepAwakeEnabled { store.toggleKeepAwake() } }))
                .radarHoverHighlight()
            Text("常亮仅防止空闲休眠，不修改屏保与锁屏设置。")
                .font(.system(size: 10)).foregroundStyle(.secondary)
            Divider()
            HStack {
                Text("辅助功能 · " + store.accessibilityCheckLabel).font(.system(size: 11))
                Spacer()
                CapsuleButton(title: "查询", disabled: store.accessibilityCheck == .checking) { store.verifyAccessibility() }
                CapsuleButton(title: "自助修复…") { store.repairAccessibility() }
            }
            Text(store.accessibilityCheckDetail).font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }

    private var promptsPage: some View {
        VStack(alignment: .leading, spacing: 14) {
            sectionTitle("续接提示词", detail: "修改后点击底部保存。切换页面会保留草稿。")
            if !isRage {
                Text("当前为协作模式；狂暴模式提示词可预先编辑，切换模式后使用。")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            }
            promptField("意外中断提示词", text: $editor.draft.interruptText,
                        detail: "协作模式用于恢复可核验的意外中断。", defaultText: "刚才中断了，请继续", height: 65)
            Divider()
            promptField("普通续接提示词", text: $editor.draft.ragePrompt,
                        detail: "狂暴模式下，未设置专属文字的软件使用这段提示词。", defaultText: PromptRules.defaultRagePrompt)
            perAppPromptSection
            Divider()
            Stepper("每条会话每 \(editor.draft.rageAlternateEvery) 次续接，发送 1 次特别提示词",
                    value: $editor.draft.rageAlternateEvery, in: 2...100)
                .font(.system(size: 11)).radarHoverHighlight()
            Text("第 1 至 \(editor.draft.rageAlternateEvery - 1) 次使用普通提示词，第 \(editor.draft.rageAlternateEvery) 次使用特别提示词，之后重复。")
                .font(.system(size: 10)).foregroundStyle(.secondary)
            promptField("周期特别提示词", text: $editor.draft.rageAlternatePrompt,
                        detail: "每条会话分别计数；Grok 在特别轮次也使用此文字。", defaultText: PromptRules.defaultRageAlternatePrompt)
        }
    }

    private var rateLimitPage: some View {
        VStack(alignment: .leading, spacing: 14) {
            sectionTitle("限流后唤起", detail: "本页修改点击保存后生效；排定等待最多 10 秒。")
            Toggle("被限流时也自动唤起", isOn: $editor.draft.rateLimitWakeEnabled).radarHoverHighlight()
            VStack(alignment: .leading, spacing: 12) {
                Stepper("等待 \(editor.draft.rateLimitWaitSeconds) 秒后重新核验",
                        value: $editor.draft.rateLimitWaitSeconds, in: 1...10).radarHoverHighlight()
                Text(isRage ? "狂暴模式在等待结束后使用已配置的恢复提示词。" : "协作模式在等待结束后使用下方限流提示词。")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                if !isRage {
                    promptField("限流唤起提示词", text: $editor.draft.rateLimitText,
                                detail: "仅用于协作模式。", defaultText: "刚才被限流了，现在请继续", height: 80)
                }
            }.disabled(!editor.draft.rateLimitWakeEnabled)
            Text("自动唤起还需要自动发送总开关开启。主动停止、等待确认、未知状态不自动恢复；认证、权限和上下文问题需手动处理。同一轮最多发送一次。")
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }

    private var historicalStart: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            Text("手动启动历史完成会话").font(.system(size: 12, weight: .semibold))
            Text("仅点击此按钮才会为已选范围内的历史完成会话排队。会先保存当前草稿，再逐条核验；搜索筛选不改变已选范围。")
                .font(.system(size: 10)).foregroundStyle(.secondary)
            CapsuleButton(title: "启动已标记的完成会话（\(store.markedCompletedCount)）",
                          disabled: !store.autoContinueEnabled || store.paused || store.permissionRepairActive || store.markedCompletedCount == 0) {
                guard editor.save() else { return }
                store.startRageForCompleted()
                editor.savedMessage = "已请求核验完成会话；是否排队以恢复提醒为准。"
            }
            if !store.autoContinueEnabled {
                Text("先在「运行」页开启自动发送总开关。")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            } else if store.paused || store.permissionRepairActive {
                Text("监督暂时暂停，恢复后可启动。")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }
    }

    private func legacyEditor(_ session: SessionRecord) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(session.app_name + "：" + session.title).font(.system(size: 12, weight: .semibold))
            Text("这里保留旧版会话提示词。当前模式优先，保存不会自动发送。")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            EditorBox(text: $editor.followUpText, label: "保留的会话提示词", height: 120)
            if store.promptRules.followUp(for: session.id) != nil {
                CapsuleButton(title: "移除此设置", destructive: true) {
                    editor.followUpText = ""
                    editor.save()
                }
            }
        }
    }

    private var saveBar: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let warning = editor.validationMessages.first {
                Text(warning).font(.system(size: 10))
                    .foregroundStyle(editor.hasOverlengthPrompt ? Color.red : Color.orange)
            }
            HStack(spacing: 8) {
                Text(editor.hasChanges ? "有未保存修改" : (editor.savedMessage.isEmpty ? "所有修改已保存" : editor.savedMessage))
                    .font(.system(size: 10)).foregroundStyle(editor.hasChanges ? Color.orange : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("settings.saveStatus")
                Spacer(minLength: 8)
                CapsuleButton(title: "还原修改", disabled: !editor.hasChanges) { editor.reload() }
                CapsuleButton(title: "保存", disabled: !editor.hasChanges || editor.hasOverlengthPrompt) { editor.save() }
                    .keyboardShortcut("s", modifiers: .command)
                CapsuleButton(title: "完成") { PromptSettingsController.shared.close() }
            }
        }.padding(14).background(.white.opacity(0.035))
    }

    private func promptField(_ title: String, text: Binding<String>, detail: String,
                             defaultText: String, height: CGFloat = 100) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title).font(.system(size: 12, weight: .semibold))
                Spacer()
                Button("恢复默认") { text.wrappedValue = defaultText }
                    .font(.system(size: 10)).buttonStyle(RadarButtonStyle())
                    .accessibilityLabel(title + "：恢复默认")
            }
            Text(detail).font(.system(size: 10)).foregroundStyle(.secondary)
            EditorBox(text: text, label: title, height: height)
        }
    }

    @State private var showAppPromptEditor = false
    @State private var selectedAppForPrompts = ""

    /// 按软件专属提示词：收起时只占一行（条目计数 + 添加入口），
    /// 展开后每个软件两个小字段（续接 / 中断恢复），专属优先于通用。
    private var perAppPromptSection: some View {
        let apps = Set(editor.draft.appRagePrompts.keys).union(editor.draft.appResumeTexts.keys)
            .union(["grok", "workbuddy-ai", "zcode", "autoclaw", "codex", "qoder", "qoder-cn"]).sorted()
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text("软件专属提示词").font(.system(size: 11, weight: .semibold))
                Text("专属优先于通用续接").font(.system(size: 10)).foregroundStyle(.secondary)
                Spacer(minLength: 0)
                Button(showAppPromptEditor ? "收起" : "自定义…") {
                    if selectedAppForPrompts.isEmpty { selectedAppForPrompts = apps.first ?? "grok" }
                    showAppPromptEditor.toggle()
                }
                .font(.system(size: 10)).buttonStyle(RadarButtonStyle())
                .accessibilityLabel("自定义按软件专属提示词")
            }
            if showAppPromptEditor {
                Picker("软件", selection: $selectedAppForPrompts) {
                    ForEach(apps, id: \.self) { appID in
                        Text(PromptRules.appDisplayNames[appID] ?? appID).tag(appID)
                    }
                }.pickerStyle(.menu).font(.system(size: 10))
                let overrides = editor.draft.appRagePrompts[selectedAppForPrompts]
                let resumeOverrides = editor.draft.appResumeTexts[selectedAppForPrompts]
                promptField("专属续接提示词",
                            text: Binding(get: { overrides ?? "" },
                                          set: { editor.draft.appRagePrompts[selectedAppForPrompts] = $0 }),
                            detail: "仅用于该软件的普通续接；留空使用通用提示词。",
                            defaultText: "", height: 70)
                promptField("专属中断恢复提示词",
                            text: Binding(get: { resumeOverrides ?? "" },
                                          set: { editor.draft.appResumeTexts[selectedAppForPrompts] = $0 }),
                            detail: "该软件意外中断重启时使用；留空使用通用恢复提示词。",
                            defaultText: "", height: 70)
            }
        }
    }

    private func sectionTitle(_ title: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title).font(.system(size: 13, weight: .semibold))
            Text(detail).font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }

    private func modeButton(_ title: String, mode: String, color: Color) -> some View {
        let selected = store.promptRules.completionMode == mode
        return Button {
            guard !selected else { return }
            var rules = store.promptRules
            rules.completionMode = mode
            store.applyRules(rules)
        } label: {
            HStack(spacing: 5) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                Text(title)
            }.font(.system(size: 12, weight: .semibold))
                .frame(maxWidth: .infinity).padding(.vertical, 9)
                .background(RoundedRectangle(cornerRadius: 7).fill(color.opacity(selected ? 0.35 : 0.10)))
        }.buttonStyle(RadarButtonStyle()).accessibilityValue(selected ? "已选择" : "未选择")
    }

    private var isRage: Bool { store.promptRules.completionMode == "rage" }
}
#endif
