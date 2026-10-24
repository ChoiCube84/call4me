import SwiftUI

/// Owns the model for one call and switches between live, typing, and summary.
struct LiveCallScreen: View {
    @EnvironmentObject private var router: AppRouter
    @StateObject private var model: LiveCallModel

    init(callId: String) {
        _model = StateObject(wrappedValue: LiveCallModel(callId: callId))
    }

    var body: some View {
        Group {
            if let result = model.result {
                SummaryView(result: result, company: model.session?.company) {
                    router.activeCallId = nil
                }
            } else if model.isTakingOver {
                TakeoverView(model: model)
            } else {
                LiveCallView(model: model)
            }
        }
        .animation(.default, value: model.isTakingOver)
        .overlay(alignment: .top) { AlertBanner(alert: $model.alert) }
        .sheet(item: $router.pendingAsk) { ask in
            AskUserSheet(ask: ask, model: model)
        }
        .onAppear {
            model.start()
            consumeTakeoverRequest()
        }
        .onChange(of: router.wantsTakeover) { _, _ in consumeTakeoverRequest() }
        .onDisappear { model.stop() }
    }

    /// "Take over" tapped on a notification.
    private func consumeTakeoverRequest() {
        guard router.wantsTakeover else { return }
        router.wantsTakeover = false
        Task { await model.takeOver() }
    }
}
