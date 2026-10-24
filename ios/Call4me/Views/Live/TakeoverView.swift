import SwiftUI

/// Type-to-talk: the user types, the phone speaks it to the company.
/// The AI is silent until the user taps Hand back.
struct TakeoverView: View {
    @ObservedObject var model: LiveCallModel
    @State private var draft = ""
    @State private var sending = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label("You're typing", systemImage: "keyboard")
                    .font(.title2.bold())
                Spacer()
                Button {
                    Task { await model.handBack() }
                } label: {
                    Label("Hand back", systemImage: "arrow.uturn.backward")
                        .font(.title3.bold())
                        .frame(minHeight: 44)
                }
                .buttonStyle(.borderedProminent)
                .tint(.green)
                .accessibilityHint("The AI talks again.")
            }
            .padding()
            .background(PhaseColor.color(.typing).opacity(0.2))

            Text("The AI is quiet. What you type is spoken to the company.")
                .font(.callout)
                .padding(.horizontal)
                .padding(.top, 8)
                .frame(maxWidth: .infinity, alignment: .leading)

            CaptionList(captions: model.captions)

            if let error = model.errorText {
                Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.red)
            }
            composer
        }
        .onAppear { focused = true }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 10) {
            TextField("Type to speak", text: $draft, axis: .vertical)
                .font(.title3)
                .lineLimit(1...5)
                .focused($focused)
                .submitLabel(.send)
                .onSubmit { Task { await send() } }
                .padding(12)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
            Button {
                Task { await send() }
            } label: {
                Image(systemName: "paperplane.fill")
                    .font(.title2)
                    .frame(width: 52, height: 52)
            }
            .buttonStyle(.borderedProminent)
            .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || sending)
            .accessibilityLabel("Speak")
        }
        .padding()
        .background(.bar)
    }

    private func send() async {
        guard !sending else { return }
        sending = true
        let text = draft
        draft = ""
        if await model.say(text) {
            Haptics.tap()
        } else {
            draft = text  // let the user retry
        }
        sending = false
    }
}
