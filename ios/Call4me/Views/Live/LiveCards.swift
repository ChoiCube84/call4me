import SwiftUI

/// Plain-language "what's happening" from the agent.
struct SummaryCard: View {
    let text: String

    var body: some View {
        Label {
            Text(text).font(.title3)
        } icon: {
            Image(systemName: "text.bubble.fill")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(Color.accentColor.opacity(0.12))
        .accessibilityLabel("Summary: \(text)")
    }
}

/// "Still on hold. Keep waiting?" with two big answers.
struct HoldCheckCard: View {
    let minutes: Int
    let onAnswer: (Bool) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("On hold \(minutes) min. Keep waiting?")
                .font(.title3.bold())
            HStack {
                BigButton(title: "Keep waiting", systemImage: "hourglass") { onAnswer(true) }
                BigButton(title: "Stop", systemImage: "xmark", tint: .red) { onAnswer(false) }
            }
        }
        .padding()
        .background(Color.orange.opacity(0.2))
    }
}

/// The agent asks the user to type directly (e.g. for a full card number).
struct TakeoverSuggestedCard: View {
    let reason: String
    let onTakeOver: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Please take over", systemImage: "hand.raised.fill")
                .font(.title3.bold())
            Text(reason).font(.body)
            BigButton(title: "Take over now", systemImage: "keyboard", tint: .purple, action: onTakeOver)
        }
        .padding()
        .background(Color.orange.opacity(0.2))
    }
}

/// Shown when the user closed the question sheet without answering.
struct WaitingAskCard: View {
    let question: String
    let onAnswer: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("The AI needs you", systemImage: "exclamationmark.bubble.fill")
                .font(.title3.bold())
            Text(question).font(.body)
            BigButton(title: "Answer", systemImage: "square.and.pencil", tint: .orange, action: onAnswer)
        }
        .padding()
        .background(Color.orange.opacity(0.2))
    }
}
