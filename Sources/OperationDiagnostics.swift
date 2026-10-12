import Foundation
import CryptoKit

/// Fixed metadata only. The Python writer shares a lock and limits with the
/// send bridge; disk work and short helper processes stay off the main thread.
final class OperationReceiptWriter {
    private let queue = DispatchQueue(label: "radar.operation-receipts", qos: .utility)
    private let lock = NSLock()
    private var pending = 0
    private let resourcePath: String
    init(resourcePath: String) { self.resourcePath = resourcePath }
    static func hash(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
    func submit(event: ContinuationEvent, stage: String, code: String, attempted: Bool, time: Double) {
        guard time.isFinite, time > 0, event.detectedAt.isFinite else { return }
        let payload: [String: Any] = ["operation_id": event.operationID,
            "session_hash": Self.hash(event.session.id), "key_hash": Self.hash(event.key),
            "app_id": event.session.app_id, "stage": stage, "code": code,
            "attempted": attempted, "time": time,
            "elapsed_ms": Int(min(86_400, max(0, time - event.detectedAt)) * 1000)]
        guard let data = try? JSONSerialization.data(withJSONObject: payload), data.count <= 2048 else { return }
        lock.lock()
        guard pending < 128 else { lock.unlock(); return }
        pending += 1; lock.unlock()
        queue.async { [self] in
            defer { lock.lock(); pending -= 1; lock.unlock() }
            let process = Process(), input = Pipe()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = ["-B", resourcePath + "/supervisor/record_operation.py"]
            process.standardInput = input
            process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
            do {
                try process.run()
                input.fileHandleForWriting.write(data); try? input.fileHandleForWriting.close()
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 3) {
                    if process.isRunning {
                        process.terminate()
                        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 1) {
                            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
                        }
                    }
                }
                process.waitUntilExit()
            } catch { try? input.fileHandleForWriting.close() }
        }
    }
    static func recentSummary(directory: URL) -> String {
        let url = directory.appendingPathComponent("operation-receipts.jsonl")
        guard let size = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize,
              size <= 262_144, let data = try? Data(contentsOf: url), data.count <= 262_144,
              let text = String(data: data, encoding: .utf8) else { return "尚无操作回执" }
        var events: [String: [String]] = [:], order: [String] = []
        for line in text.split(separator: "\n").suffix(2048) {
            guard line.utf8.count <= 1024,
                  let row = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any],
                  let id = row["operation_id"] as? String, id.count == 32,
                  id.allSatisfy({ "0123456789abcdef".contains($0) }),
                  let app = row["app_id"] as? String,
                  let stage = row["stage"] as? String,
                  let code = row["code"] as? String,
                  app.range(of: "^[a-z][a-z0-9-]{0,30}$", options: .regularExpression) != nil,
                  stage.range(of: "^[a-z][a-z0-9-]{0,30}$", options: .regularExpression) != nil,
                  code.range(of: "^[a-z][a-z0-9_]{0,63}$", options: .regularExpression) != nil else { continue }
            if events[id] == nil { order.append(id); events[id] = [] }
            events[id]?.append(app + " · " + stage + " · " + code)
        }
        return order.suffix(8).map { id in
            "操作 \(id.prefix(8))：" + (events[id] ?? []).suffix(8).joined(separator: " → ")
        }.joined(separator: "\n")
    }
}
