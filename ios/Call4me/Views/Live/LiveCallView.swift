import SwiftUI

/// Big captions of both sides, a plain summary, the state, and Take over.
struct LiveCallView: View {
    @ObservedObject var model: LiveCallModel
    @EnvironmentObject private var router: AppRouter
    @State private var confirmStop = false

    var body: some View {
        VStack(spacing: 0) {
            StateBanner(phase: model.phase, company: model.session?.company,
                        holdSince: model.phase == .onHold ? model.holdStartedAt : nil)
            if !model.connected {
                Label("Reconnecting…", systemImage: "wifi.exclamationmark")
                    .font(.callout.bold())
                    .frame(maxWidth: .infinity)
                    .padding(6)
                    .background(.yellow)
                    .foregroundStyle(.black)
            }
            if let summary = model.latestSummary {
                SummaryCard(text: summary)
            }
            if let ask = router.waitingAsk, router.pendingAsk == nil {
                WaitingAskCard(question: ask.question) { router.reopenWaitingAsk() }
            }
            if let minutes = model.holdCheckMinutes {
                HoldCheckCard(minutes: minutes) { keep in
                    Task { await model.answerHold(keepWaiting: keep) }
                }
            }
            if let reason = model.takeoverReason {
                TakeoverSuggestedCard(reason: reason) {
                    Task { await model.takeOver() }
                }
            }
            CaptionList(captions: model.captions)
            controls
        }
        .confirmationDialog("Stop this call?", isPresented: $confirmStop, titleVisibility: .visible) {
            Button("Stop call", role: .destructive) { Task { await model.cancelCall() } }
            Button("Keep going", role: .cancel) {}
        }
    }

    private var controls: some View {
        VStack(spacing: 10) {
            if let error = model.errorText {
                Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.red)
            }
            BigButton(title: "Take over", systemImage: "keyboard", tint: .purple) {
                Task { await model.takeOver() }
            }
            .accessibilityHint("You type. The phone speaks your words.")
            Button("Stop call", role: .destructive) { confirmStop = true }
                .font(.body.bold())
                .frame(minHeight: 44)
        }
        .padding()
        .background(.bar)
    }
}
