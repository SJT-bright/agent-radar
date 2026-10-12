import Foundation

enum OpenClawCareRegistry {
    struct Selection { var ids = Set<String>(); var care = Set<String>() }
    static let defaultURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/AgentRadar/selected-openclaw-sessions.json")
    static func valid(_ id: String) -> Bool {
        let parts = id.split(separator: ":")
        return parts.count == 3 && parts[0] == "openclaw" &&
            parts[1].range(of: "^[A-Za-z0-9_-]{1,80}$", options: .regularExpression) != nil && UUID(uuidString: String(parts[2])) != nil
    }
    static func read(_ url: URL) throws -> Selection {
        guard FileManager.default.fileExists(atPath: url.path) else { return Selection() }
        guard (try url.resourceValues(forKeys: [.isSymbolicLinkKey, .fileSizeKey])).isSymbolicLink != true,
              (try url.resourceValues(forKeys: [.fileSizeKey])).fileSize ?? 65537 <= 65536 else {
            throw CocoaError(.fileReadCorruptFile)
        }
        guard let raw = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any],
              raw["version"] as? Int == 1, let ids = raw["ids"] as? [String], ids.count <= 40,
              ids.allSatisfy(valid) else { throw CocoaError(.fileReadCorruptFile) }
        let care = raw["care_ids"] as? [String] ?? []
        guard care.count <= 40, care.allSatisfy(valid) else { throw CocoaError(.fileReadCorruptFile) }
        return Selection(ids: Set(ids), care: Set(care).intersection(ids))
    }
    static func update(_ url: URL, id: String, enabled: Bool) throws -> Selection {
        guard valid(id) else { throw CocoaError(.validationMissingMandatoryProperty) }
        var current = try read(url)
        current.ids.insert(id)
        if enabled { current.care.insert(id) } else { current.care.remove(id) }
        guard current.ids.count <= 40 else { throw CocoaError(.fileWriteOutOfSpace) }
        let raw: [String: Any] = ["version": 1, "ids": current.ids.sorted(), "care_ids": current.care.sorted()]
        let data = try JSONSerialization.data(withJSONObject: raw, options: [.sortedKeys])
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let temporary = url.deletingLastPathComponent().appendingPathComponent(".care-" + UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: temporary) }
        try data.write(to: temporary)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: temporary.path)
        guard rename(temporary.path, url.path) == 0 else { throw CocoaError(.fileWriteUnknown) }
        return current
    }
}
