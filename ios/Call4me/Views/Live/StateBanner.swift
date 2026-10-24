import SwiftUI

/// Colored strip with the call phase, and a hold timer when on hold.
struct StateBanner: View {
    let phase: CallPhase
    let company: String?
    let holdSince: Date?

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: phase.symbol)
                .font(.title)
            VStack(alignment: .leading, spacing: 2) {
                Text(phase.title)
                    .font(.title2.bold())
                if let company {
                    Text(company).font(.callout)
                }
            }
            Spacer()
            if let holdSince {
                Text(holdSince, style: .timer)
                    .font(.title2.monospacedDigit().bold())
                    .accessibilityLabel("Time on hold")
            }
        }
        .padding()
        .frame(maxWidth: .infinity)
        .background(PhaseColor.color(phase).opacity(0.9))
        .foregroundStyle(.white)
        .accessibilityElement(children: .combine)
    }
}
