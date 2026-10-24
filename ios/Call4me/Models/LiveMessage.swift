import Foundation

/// Raw message from `WS /calls/{id}/live` (backend LiveMessage).
/// Decode with `JSONCoding.liveDecoder`.
struct LiveMessage: Decodable {
    var type: String
    var sessionId: String
    var at: Date?
    var data: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case type, at, data
        case sessionId = "session_id"
    }
}

enum Speaker: String {
    case remote, agent, user

    var label: String {
        switch self {
        case .remote: return "Company"
        case .agent: return "AI assistant"
        case .user: return "You"
        }
    }
}

/// Typed view of a live message. Unknown types are ignored.
enum LiveEvent {
    case caption(speaker: Speaker, text: String, isFinal: Bool)
    case state(from: String?, to: String, reason: String?)
    case summary(String)
    case askUser(requestId: String, question: String, sensitivity: String?)
    case event(kind: String, reason: String?, data: [String: JSONValue])
    case ended(summary: String?, referenceNumber: String?, minutesSaved: Double?)

    init?(_ m: LiveMessage) {
        let d = m.data
        switch m.type {
        case "caption":
            guard let text = d.string("text") else { return nil }
            let speaker = Speaker(rawValue: d.string("speaker") ?? "remote") ?? .remote
            self = .caption(speaker: speaker, text: text, isFinal: d.bool("final") ?? true)
        case "state":
            guard let to = d.string("to") else { return nil }
            self = .state(from: d.string("from"), to: to, reason: d.string("reason"))
        case "summary":
            guard let text = d.string("text") else { return nil }
            self = .summary(text)
        case "ask_user":
            guard let id = d.string("request_id"), let q = d.string("question") else { return nil }
            self = .askUser(requestId: id, question: q, sensitivity: d.string("sensitivity"))
        case "event":
            self = .event(kind: d.string("kind") ?? "", reason: d.string("reason"), data: d)
        case "ended":
            self = .ended(
                summary: d.string("summary"),
                referenceNumber: d.string("reference_number"),
                minutesSaved: d.double("minutes_saved")
            )
        default:
            return nil
        }
    }
}
