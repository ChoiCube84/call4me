import Foundation

/// The user-facing step of a call. Many backend `CallState` values collapse
/// into one phase so the UI stays short and plain.
enum CallPhase: String, Codable, Hashable {
    case calling
    case menu
    case onHold
    case personAnswered
    case needsInput
    case typing
    case done

    /// Maps the backend `CallState` string (backend/contracts.py) to a phase.
    init(backendState: String) {
        switch backendState.uppercased() {
        case "DIALING": self = .calling
        case "MENU": self = .menu
        case "HOLD", "RECORDED_MESSAGE", "CALLBACK_OFFERED": self = .onHold
        case "HUMAN_SUSPECTED", "HUMAN_CONFIRMED", "IN_CONVERSATION": self = .personAnswered
        case "WAITING_ON_USER": self = .needsInput
        case "TAKEOVER": self = .typing
        case "ENDED", "FAILED", "VOICEMAIL": self = .done
        default: self = .calling
        }
    }

    var title: String {
        switch self {
        case .calling: return "Calling"
        case .menu: return "Navigating menu"
        case .onHold: return "On hold"
        case .personAnswered: return "Person answered"
        case .needsInput: return "Needs your input"
        case .typing: return "You're typing"
        case .done: return "Done"
        }
    }

    var symbol: String {
        switch self {
        case .calling: return "phone.arrow.up.right"
        case .menu: return "list.number"
        case .onHold: return "hourglass"
        case .personAnswered: return "person.wave.2"
        case .needsInput: return "exclamationmark.bubble.fill"
        case .typing: return "keyboard"
        case .done: return "checkmark.circle.fill"
        }
    }

    /// Phases where the user should look at the phone now.
    var needsAttention: Bool {
        self == .needsInput || self == .personAnswered
    }

    var isFinished: Bool { self == .done }
}
