import SwiftUI

/// The selector keeps display filters separate from the persisted supervision scope.
struct SupervisionSettingsView: View {
    @ObservedObject var store: MonitorStore
    @State private var searchText = ""
    @State private var showCompleted = false
    @State private var expandedAppIDs: Set<String> = []
    @State private var collapsedSearchAppIDs: Set<String> = []
    @State private var showUnavailableMarks = false
    @State private var snapshot = SupervisionSettingsSnapshot.empty

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            summary
            searchField
            Toggle("显示已完成会话，供手动启动", isOn: $showCompleted)
                .font(.system(size: 10)).radarHoverHighlight()
            filterSummary

            if snapshot.groups.isEmpty && snapshot.unavailableApps.isEmpty {
                emptyState
            }
            ForEach(snapshot.groups) { group in
                appGroup(group)
            }
            ForEach(snapshot.unavailableApps) { app in
                Toggle(isOn: Binding(get: { store.isAppSupervised(app.id) },
                                     set: { store.setAppSupervision($0, appID: app.id) })) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("监督 \(app.name) 的全部对话").font(.system(size: 10, weight: .semibold))
                        Text("当前无可核验的可读会话；软件监督选择保留。")
                            .font(.system(size: 9)).foregroundStyle(.secondary)
                    }
                }
                .toggleStyle(.checkbox).radarHoverHighlight()
                .padding(8).frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 8).fill(.white.opacity(0.055)))
            }
            if !snapshot.unavailableMarks.isEmpty {
                unavailableMarks
            }
        }
        .onAppear(perform: refreshSnapshot)
        // Grouping, deduplication and search run only when their inputs change.
        // Expanding a group or receiving unrelated monitor state does not rebuild them.
        .onChange(of: store.sessions) { _ in refreshSnapshot() }
        .onChange(of: store.apps) { _ in refreshSnapshot() }
        .onChange(of: store.removedSessions) { _ in refreshSnapshot() }
        .onChange(of: store.supervisedSessionIDs) { _ in refreshSnapshot() }
        .onChange(of: store.supervisedAppIDs) { _ in refreshSnapshot() }
        .onChange(of: showCompleted) { _ in refreshSnapshot() }
        .onChange(of: searchText) { _ in
            collapsedSearchAppIDs.removeAll()
            refreshSnapshot()
        }
    }

    private var summary: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("持续监督").font(.system(size: 12, weight: .semibold))
                Spacer()
                Text("选择立即保存").font(.system(size: 9)).foregroundStyle(.secondary)
            }
            Text("\(store.supervisedAppIDs.count) 个软件 · \(store.supervisedSessionIDs.count) 条单选 · 当前覆盖 \(snapshot.effectiveCount) 条")
                .font(.system(size: 10)).foregroundStyle(.secondary)
            Text(store.supervisedAppIDs.isEmpty && store.supervisedSessionIDs.isEmpty
                 ? "选择软件可覆盖当前及今后发现的可核验对话；展开软件也可逐条选择。"
                 : "关闭软件监督后，单独选择仍保留。协作模式保留这些选择，完成时只提醒验收。")
                .font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }

    private var searchField: some View {
        HStack(spacing: 7) {
            Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
            TextField("搜索会话、软件或工作区", text: $searchText)
                .textFieldStyle(.plain)
                .accessibilityLabel("搜索监督会话")
            if !searchText.isEmpty {
                Button { searchText = "" } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                        .padding(3)
                }
                .buttonStyle(RadarButtonStyle())
                .help("清除搜索").accessibilityLabel("清除搜索")
            }
        }
        .font(.system(size: 11)).padding(8)
        .background(RoundedRectangle(cornerRadius: 7).fill(.white.opacity(0.07)))
        .overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(.white.opacity(0.12)))
    }

    private var filterSummary: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(snapshot.countDescription).font(.system(size: 9)).foregroundStyle(.secondary)
            if snapshot.hiddenMarkedCompletedCount > 0 {
                Text("已隐藏的完成会话中，\(snapshot.hiddenMarkedCompletedCount) 条保留单独标记；开启上方开关可调整。")
                    .font(.system(size: 9)).foregroundStyle(.secondary)
            }
            if snapshot.searchFilteredMarkedCount > 0 {
                Text("搜索过滤了 \(snapshot.searchFilteredMarkedCount) 条单独标记的会话，监督选择仍然有效。")
                    .font(.system(size: 9)).foregroundStyle(.secondary)
            }
        }
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(snapshot.isSearching ? "没有匹配的会话或软件" : "当前没有可选择的可读会话")
                .font(.system(size: 11, weight: .medium))
            Text(snapshot.isSearching ? "可搜索会话标题、软件名称或完整工作区路径。" : "雷达读取到会话后会在这里显示。")
                .font(.system(size: 10)).foregroundStyle(.secondary)
            if snapshot.isSearching {
                Button("清除搜索") { searchText = "" }
                    .buttonStyle(RadarButtonStyle(inset: 7)).font(.system(size: 10))
            }
        }
        .padding(10).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(.white.opacity(0.04)))
    }

    private var unavailableMarks: some View {
        DisclosureGroup(isExpanded: $showUnavailableMarks) {
            VStack(alignment: .leading, spacing: 8) {
                Text("这些会话当前不可读取或身份不唯一，保留标记后可在读取恢复时重新核验。搜索与完成会话开关不会影响此列表。")
                    .font(.system(size: 9)).foregroundStyle(.secondary)
                ForEach(snapshot.unavailableMarks) { mark in
                    unavailableMarkRow(mark)
                }
            }.padding(.top, 5)
        } label: {
            Text("\(snapshot.unavailableMarks.count) 条单选会话暂不可核验")
                .font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }

    private func unavailableMarkRow(_ mark: SupervisionSettingsSnapshot.UnavailableMark) -> some View {
        HStack(alignment: .top, spacing: 8) {
            VStack(alignment: .leading, spacing: 3) {
                Text(mark.title).font(.system(size: 10)).lineLimit(2)
                Text(mark.detail).font(.system(size: 9)).foregroundStyle(.secondary)
            }.frame(maxWidth: .infinity, alignment: .leading)
                .help(mark.title + "\n" + mark.id)
            Button("取消标记") { store.removeSupervision(sessionID: mark.id) }
                .font(.system(size: 10)).buttonStyle(RadarButtonStyle(inset: 5))
                .help("取消此会话的单独监督标记")
                .accessibilityLabel("取消监督标记：" + mark.title)
        }
    }

    private func appGroup(_ group: SupervisionSettingsSnapshot.Group) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .center, spacing: 5) {
                Button { toggleExpansion(group.id) } label: {
                    HStack(spacing: 5) {
                        Image(systemName: isExpanded(group.id) ? "chevron.down" : "chevron.right")
                            .font(.system(size: 9, weight: .semibold)).frame(width: 12)
                        Text(group.name).font(.system(size: 11, weight: .semibold))
                        Text("\(group.rows.count) 条").font(.system(size: 9)).foregroundStyle(.secondary)
                        Spacer(minLength: 0)
                    }.padding(.vertical, 4).contentShape(Rectangle())
                }
                .buttonStyle(RadarButtonStyle())
                .accessibilityLabel((isExpanded(group.id) ? "收起 " : "展开 ") + group.name + " 会话")
                Toggle("监督全部", isOn: Binding(get: { store.isAppSupervised(group.id) },
                                               set: { store.setAppSupervision($0, appID: group.id) }))
                    .font(.system(size: 10)).toggleStyle(.checkbox).radarHoverHighlight()
                    .fixedSize()
                    .accessibilityLabel(group.name + "：监督全部对话")
                    .help("覆盖此软件当前及今后发现的可核验对话；已完成的旧轮次仍需手动启动。")
            }
            if group.hiddenCompletedCount > 0 {
                Text("\(group.hiddenCompletedCount) 条已完成会话已隐藏")
                    .font(.system(size: 9)).foregroundStyle(.secondary)
            }
            if isExpanded(group.id) {
                if store.isAppSupervised(group.id) {
                    Text("已覆盖本软件当前及新发现的可核验对话。旧的完成轮次仍需手动启动。")
                        .font(.system(size: 9)).foregroundStyle(.secondary)
                    if let reason = group.inputRestriction {
                        Text(reason).font(.system(size: 9)).foregroundStyle(.secondary)
                    }
                }
                if group.rows.isEmpty {
                    Text("开启「显示已完成会话」可查看并逐条选择。")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                } else {
                    LazyVStack(alignment: .leading, spacing: 6) {
                        ForEach(group.rows) { row in
                            sessionRow(row, appID: group.id)
                        }
                    }
                }
            }
        }
        .padding(8).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(.white.opacity(0.055)))
    }

    private func sessionRow(_ row: SessionRecord, appID: String) -> some View {
        Toggle(isOn: Binding(get: { store.isSupervised(row) },
                             set: { store.setSupervision($0, for: row) })) {
            VStack(alignment: .leading, spacing: 3) {
                Text(row.title).font(.system(size: 10)).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 6) {
                    Text(row.statusLabel)
                    if !row.project.isEmpty {
                        Text(row.displayProject).lineLimit(1).truncationMode(.middle)
                    }
                    if store.supervisedSessionIDs.contains(row.id) {
                        Text("单独标记")
                    }
                }.font(.system(size: 9)).foregroundStyle(.secondary)
                if let reason = ContinuationPolicy.followupRestriction(row) {
                    Text(reason).font(.system(size: 9)).foregroundStyle(.secondary)
                }
            }
            .help(row.title + (row.project.isEmpty ? "" : "\n" + row.project) + "\n" + store.supervisionDetail(for: row))
        }
        .toggleStyle(.checkbox).radarHoverHighlight()
        .disabled(store.isAppSupervised(appID))
        .accessibilityLabel(row.app_name + "：" + row.title + "，持续监督")
    }

    private func isExpanded(_ appID: String) -> Bool {
        snapshot.isSearching ? !collapsedSearchAppIDs.contains(appID) : expandedAppIDs.contains(appID)
    }

    private func toggleExpansion(_ appID: String) {
        if snapshot.isSearching {
            if !collapsedSearchAppIDs.insert(appID).inserted { collapsedSearchAppIDs.remove(appID) }
        } else if !expandedAppIDs.insert(appID).inserted {
            expandedAppIDs.remove(appID)
        }
    }

    private func refreshSnapshot() {
        snapshot = SupervisionSettingsSnapshot(store: store, query: searchText, includeCompleted: showCompleted)
    }
}

/// Cached presentation state. In particular, unreadability is derived from the
/// complete source snapshot, never from a collapsed, completed-hidden or searched list.
private struct SupervisionSettingsSnapshot {
    struct Group: Identifiable {
        let id: String
        let name: String
        let rows: [SessionRecord]
        let hiddenCompletedCount: Int
        let inputRestriction: String?
    }
    struct UnavailableApp: Identifiable {
        let id: String
        let name: String
    }
    struct UnavailableMark: Identifiable {
        let id: String
        let title: String
        let detail: String
    }

    var groups: [Group] = []
    var unavailableApps: [UnavailableApp] = []
    var unavailableMarks: [UnavailableMark] = []
    var effectiveCount = 0
    var totalCount = 0
    var visibleCount = 0
    var hiddenCompletedCount = 0
    var hiddenMarkedCompletedCount = 0
    var searchFilteredCount = 0
    var searchFilteredMarkedCount = 0
    var isSearching = false
    static let empty = SupervisionSettingsSnapshot()

    var countDescription: String {
        var parts = ["显示 \(visibleCount) / \(totalCount) 条会话"]
        if hiddenCompletedCount > 0 { parts.append("隐藏 \(hiddenCompletedCount) 条已完成") }
        if searchFilteredCount > 0 { parts.append("搜索过滤 \(searchFilteredCount) 条") }
        return parts.joined(separator: " · ")
    }

    private init() {}

    init(store: MonitorStore, query: String, includeCompleted: Bool) {
        let query = query.trimmingCharacters(in: .whitespacesAndNewlines)
        isSearching = !query.isEmpty
        let sourceGroups = store.supervisionGroups(includeCompleted: true)
        let sourceAppIDs = Set(sourceGroups.map(\.id))
        let unique = Dictionary(grouping: store.sessions, by: \.id)
        let readableIDs = Set(store.sessions.compactMap { row -> String? in
            unique[row.id]?.count == 1 && row.isReadable && !store.isRemoved(row) ? row.id : nil
        })
        unavailableMarks = store.supervisedSessionIDs.subtracting(readableIDs).sorted().map { id in
            let rows = unique[id] ?? []
            let row = rows.first
            let shortID = id.count > 20 ? String(id.prefix(10)) + "…" + String(id.suffix(6)) : id
            let title = row?.title.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let reason = rows.count > 1 ? "会话身份不唯一" : (row == nil ? "当前快照中没有此会话" : "当前无法读取会话")
            return UnavailableMark(id: id, title: title.isEmpty ? "会话 " + shortID : title,
                                   detail: (row.map { $0.app_name + " · " } ?? "") + reason)
        }
        effectiveCount = store.effectiveSupervisedIDs.intersection(readableIDs).count

        func matches(_ row: SessionRecord) -> Bool {
            query.isEmpty || [row.title, row.app_name, row.app_id, row.project].contains {
                $0.localizedStandardContains(query)
            }
        }

        for source in sourceGroups {
            totalCount += source.sessions.count
            let hiddenCompleted = includeCompleted ? [] : source.sessions.filter { $0.status == "completed" }
            hiddenCompletedCount += hiddenCompleted.count
            hiddenMarkedCompletedCount += hiddenCompleted.filter { store.supervisedSessionIDs.contains($0.id) }.count
            let eligible = includeCompleted ? source.sessions : source.sessions.filter { $0.status != "completed" }
            let visible = eligible.filter(matches)
            let searchFiltered = eligible.filter { !matches($0) }
            searchFilteredCount += searchFiltered.count
            searchFilteredMarkedCount += searchFiltered.filter { store.supervisedSessionIDs.contains($0.id) }.count
            visibleCount += visible.count
            // Keep a provider's switch reachable when its only matches are hidden
            // completed rounds. This does not mark or start those rounds.
            guard query.isEmpty || source.sessions.contains(where: matches) else { continue }
            groups.append(Group(id: source.id, name: source.primary.app_name, rows: visible,
                                hiddenCompletedCount: hiddenCompleted.filter(matches).count,
                                inputRestriction: ContinuationPolicy.inputRestriction(source.primary)))
        }

        unavailableApps = store.supervisedAppIDs.subtracting(sourceAppIDs).sorted().compactMap { appID in
            let name = store.apps.first(where: { $0.id == appID })?.name ?? appID
            guard query.isEmpty || name.localizedStandardContains(query) || appID.localizedStandardContains(query) else { return nil }
            return UnavailableApp(id: appID, name: name)
        }
    }
}
