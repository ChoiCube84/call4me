import SwiftUI

/// Visual alert that slides in at the top. The model fires the haptic.
/// Urgent alerts stay until tapped; others hide after a few seconds.
struct AlertBanner: View {
    @Binding var alert: InAppAlert?

    var body: some View {
        if let alert {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: icon(alert.kind)).font(.title)
                VStack(alignment: .leading, spacing: 4) {
                    Text(alert.title).font(.title3.bold())
                    if let message = alert.message { Text(message).font(.body) }
                }
                Spacer()
            }
            .padding()
            .background(color(alert.kind), in: RoundedRectangle(cornerRadius: 16))
            .foregroundStyle(.white)
            .padding(.horizontal)
            .shadow(radius: 8)
            .transition(.move(edge: .top).combined(with: .opacity))
            .onTapGesture { self.alert = nil }
            .accessibilityAddTraits(.isButton)
            .accessibilityHint("Tap to close")
            .task(id: alert.id) {
                guard alert.kind != .urgent else { return }
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                if self.alert?.id == alert.id { withAnimation { self.alert = nil } }
            }
        }
    }

    private func icon(_ kind: InAppAlert.Kind) -> String {
        switch kind {
        case .info: return "info.circle.fill"
        case .good: return "person.wave.2.fill"
        case .urgent: return "exclamationmark.circle.fill"
        }
    }

    private func color(_ kind: InAppAlert.Kind) -> Color {
        switch kind {
        case .info: return .blue
        case .good: return .green
        case .urgent: return .orange
        }
    }
}
