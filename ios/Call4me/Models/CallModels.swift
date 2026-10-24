import Foundation

// Mirrors backend/contracts.py and backend/main.py. Keys are snake_case on
// the wire; JSONCoding converts them.

struct CallLimits: Codable, Hashable {
    var maxHoldMinutes: Int = 60
    var maxSpendUsd: Double = 0
    var allowPlanChanges: Bool = false
    var allowCancellation: Bool = false
    var notes: String = ""
}

struct StartCallRequest: Encodable {
    var userId: String
    var company: String
    var phoneNumber: String
    var goal: String
    var limits: CallLimits
    var disclosureText: String
}

struct CallSession: Decodable, Identifiable, Hashable {
    var id: String
    var userId: String
    var company: String
    var phoneNumber: String
    var goal: String
    var limits: CallLimits
    var disclosureText: String
    var state: String
    var startedAt: Date
    var endedAt: Date?
    var outcome: String?
    var referenceNumber: String?
    var minutesSaved: Double?
}

struct TimelineEvent: Decodable, Hashable {
    var at: Date
    var kind: String
    var fromState: String?
    var toState: String?
    var confidence: Double?
}

struct CallDetail: Decodable {
    var session: CallSession
    var timeline: [TimelineEvent]
}

struct Company: Decodable, Hashable, Identifiable {
    var name: String
    var phoneNumber: String
    var note: String?

    var id: String { name }
}

struct Accepted: Decodable {
    var ok: Bool
    var detail: String?
}
