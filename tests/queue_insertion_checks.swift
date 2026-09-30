import Foundation

@main struct QueueInsertionChecks {
    static func main() {
        precondition(!QueueInsertionController.shouldInsert(previouslyPresent: nil, count: 1),
                     "existing queue on first observation must remain untouched")
        precondition(QueueInsertionController.shouldInsert(previouslyPresent: false, count: 1),
                     "one new queue control must be inserted")
        precondition(!QueueInsertionController.shouldInsert(previouslyPresent: true, count: 1),
                     "the same queue control must not be pressed twice")
        precondition(!QueueInsertionController.shouldInsert(previouslyPresent: false, count: 2),
                     "ambiguous queued messages must remain untouched")
        let first = QueueInsertionController.contextKey(window: "window", urls: ["qoder://app#/chat/first"],
                                                        headings: ["Task"], composers: [1])
        let second = QueueInsertionController.contextKey(window: "window", urls: ["qoder://app#/chat/second"],
                                                         headings: ["Task"], composers: [1])
        precondition(first != second, "switching conversation must create a new baseline")
        precondition(first == QueueInsertionController.contextKey(window: "window", urls: ["qoder://app#/chat/first"],
                                                             headings: ["Task"], composers: [2]),
                     "composer remount during send must retain conversation baseline")
        precondition(QueueInsertionController.contextKey(window: "window", urls: ["file:///app"],
                                                      headings: ["命令面板", "Task"], composers: [1]) !=
                     QueueInsertionController.contextKey(window: "window", urls: ["file:///app"],
                                                      headings: ["命令面板", "Task"], composers: [2]),
                     "generic app URL must not merge separate conversations")
        precondition(QueueInsertionController.contextKey(window: "window", urls: [],
                                                         headings: [], composers: []) == nil,
                     "unidentified conversation must not be clicked")
        print("Queue insertion checks passed")
    }
}
