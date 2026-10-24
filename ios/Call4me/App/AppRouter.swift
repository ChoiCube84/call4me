import Foundation

/// A question from the agent the user must answer.
/// Holds only the question; the answer is never kept here.
struct AskRequest: Identifiable, Equatable {
    var id: String { requestId }
    let requestId: String
    let sessionId: String
    let question: String
    let sensitivity: String?

    var isHighSensitivity: Bool { sensitivity == "high" }
}

/// App-wide navigation driven by pushes, deep links, and the start screen.
@MainActor
final class AppRouter: ObservableObject {
    static let shared = AppRouter()

    /// The call shown in the live view.
    @Published var activeCallId: String?
    /// Question to show in the ask sheet.
    @Published var pendingAsk: AskRequest?
    /// Question not yet answered (kept if the user taps "Later").
    @Published private(set) var waitingAsk: AskRequest?
    /// Open the type-to-talk view as soon as the live view appears.
    @Published var wantsTakeover = false

    /// Request ids already answered, so a push and the live channel
    /// don't show the same question twice. Ids only, never answers.
    private var answered: Set<String> = []

    func open(callId: String) {
        activeCallId = callId
    }

    func show(_ ask: AskRequest) {
        guard !answered.contains(ask.requestId), pendingAsk?.requestId != ask.requestId else { return }
        activeCallId = activeCallId ?? ask.sessionId
        pendingAsk = ask
        waitingAsk = ask
    }

    func reopenWaitingAsk() {
        pendingAsk = waitingAsk
    }

    /// The call moved on (answered elsewhere, timed out, or takeover).
    func clearWaitingAsk() {
        waitingAsk = nil
        pendingAsk = nil
    }

    func markAnswered(_ requestId: String) {
        answered.insert(requestId)
        if pendingAsk?.requestId == requestId { pendingAsk = nil }
        if waitingAsk?.requestId == requestId { waitingAsk = nil }
    }

    func handle(url: URL) {
        // call4me://call/<id>
        guard url.scheme == "call4me", url.host == "call" else { return }
        let id = url.lastPathComponent
        if !id.isEmpty, id != "/" { open(callId: id) }
    }
}
