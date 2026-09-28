import Foundation
@main struct PromptBoundary {
    static func main() throws {
        let values = [String(repeating: "优", count: 2001), String(repeating: "👩🏽‍💻", count: 2001),
                      String(repeating: "x", count: 1999) + "👩🏽‍💻", "新计划\n核对\t结果\u{0}"]
        let data = try JSONEncoder().encode(values.map(PromptRules.sanitize))
        print(String(decoding: data, as: UTF8.self))
    }
}
