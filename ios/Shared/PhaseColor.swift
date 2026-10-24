import SwiftUI

/// One color per phase, used by the app banner and the Live Activity.
enum PhaseColor {
    static func color(_ phase: CallPhase) -> Color {
        switch phase {
        case .calling, .menu: return .blue
        case .onHold: return .gray
        case .personAnswered: return .green
        case .needsInput: return .orange
        case .typing: return .purple
        case .done: return .green
        }
    }
}
