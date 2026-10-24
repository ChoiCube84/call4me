import SwiftUI
import WidgetKit

/// Lock screen / banner layout for the Live Activity.
struct LockScreenCallView: View {
    let company: String
    let state: CallActivityAttributes.ContentState

    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: state.phase.symbol)
                .font(.largeTitle)
                .foregroundStyle(PhaseColor.color(state.phase))
            VStack(alignment: .leading, spacing: 4) {
                Text(state.phase.title)
                    .font(.title3.bold())
                Text(state.detail ?? company)
                    .font(.body)
                    .lineLimit(2)
            }
            Spacer()
            PhaseTimer(state: state)
                .font(.title3.monospacedDigit())
        }
        .padding()
    }
}

/// Hold timer while on hold, call timer otherwise, nothing when done.
struct PhaseTimer: View {
    let state: CallActivityAttributes.ContentState

    var body: some View {
        if state.phase.isFinished {
            Image(systemName: "checkmark")
        } else if state.phase == .onHold, let hold = state.holdStartDate {
            Text(hold, style: .timer)
        } else {
            Text(state.startDate, style: .timer)
        }
    }
}
