import Foundation

/// Reads the ids the backend puts in push `userInfo`. Accepts ids at the top
/// level or nested under "data".
struct PushPayload {
    let category: String
    let sessionId: String?
    let requestId: String?
    let body: String

    init(category: String, userInfo: [AnyHashable: Any], body: String) {
        self.category = category
        self.body = body
        let nested = userInfo["data"] as? [String: Any] ?? [:]
        func value(_ key: String) -> String? {
            (userInfo[key] as? String) ?? (nested[key] as? String)
        }
        sessionId = value("session_id")
        requestId = value("request_id")
    }

    var askRequest: AskRequest? {
        guard category == PushCategory.askUser, let requestId, let sessionId else { return nil }
        return AskRequest(requestId: requestId, sessionId: sessionId, question: body, sensitivity: nil)
    }

    /// True for alerts that need the user now (stronger haptic).
    var isUrgent: Bool {
        [PushCategory.askUser, PushCategory.holdContinue, PushCategory.takeover, PushCategory.callback]
            .contains(category)
    }
}
