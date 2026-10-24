import SwiftUI

/// The AI needs something only the user knows. The answer lives only in this
/// view's memory, is sent once, then cleared. It is never saved or logged.
struct AskUserSheet: View {
    let ask: AskRequest
    @ObservedObject var model: LiveCallModel
    @Environment(\.dismiss) private var dismiss

    @State private var answer = ""
    @State private var sending = false
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    Label("The AI needs you", systemImage: "exclamationmark.bubble.fill")
                        .font(.headline)
                        .foregroundStyle(.orange)
                    Text(ask.question)
                        .font(.title.bold())
                        .fixedSize(horizontal: false, vertical: true)

                    if ask.isHighSensitivity {
                        highSensitivity
                    } else {
                        answerField
                    }
                }
                .padding()
            }
            .navigationTitle("Question")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Later") { dismiss() } }
            }
            .onAppear { focused = !ask.isHighSensitivity }
            .onDisappear { answer = "" }
        }
        .interactiveDismissDisabled(sending)
    }

    private var answerField: some View {
        VStack(alignment: .leading, spacing: 14) {
            // Private by design: no autocorrect learning, no suggestions.
            TextField("Type your answer", text: $answer, axis: .vertical)
                .font(.title2)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
                .focused($focused)
                .padding()
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
            Text("The AI says this once. We do not save it.")
                .font(.callout)
                .foregroundStyle(.secondary)
            BigButton(title: sending ? "Sending…" : "Send", systemImage: "paperplane.fill") {
                Task { await send() }
            }
            .disabled(answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || sending)
        }
    }

    /// High-sensitivity items (full card number, SSN) are not relayed by the AI.
    private var highSensitivity: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("This is very private. Please type it to the company yourself.")
                .font(.title3)
            BigButton(title: "Take over", systemImage: "keyboard", tint: .purple) {
                dismiss()
                Task { await model.takeOver() }
            }
        }
    }

    private func send() async {
        sending = true
        let text = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        let ok = await model.answer(ask, text: text)
        sending = false
        if ok {
            answer = ""
            Haptics.success()
            dismiss()
        }
    }
}
