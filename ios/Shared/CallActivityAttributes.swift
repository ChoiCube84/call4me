import ActivityKit
import Foundation

/// Live Activity data. Shared by the app and the widget extension.
///
/// The backend updates this via APNs (`apns-push-type: liveactivity`) with a
/// `content-state` matching `ContentState` below. Times are Unix seconds
/// (Double) so the server can send them without Apple's reference-date format:
///
///     "content-state": {
///       "state": "HOLD",
///       "startedAt": 1761235200.0,
///       "holdStartedAt": 1761235320.0,
///       "detail": "On hold 2 min"
///     }
struct CallActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// Backend `CallState` string, e.g. "HOLD".
        var state: String
        var startedAt: Double
        var holdStartedAt: Double?
        var detail: String?

        var phase: CallPhase { CallPhase(backendState: state) }
        var startDate: Date { Date(timeIntervalSince1970: startedAt) }
        var holdStartDate: Date? { holdStartedAt.map { Date(timeIntervalSince1970: $0) } }
    }

    var sessionId: String
    var company: String
}
