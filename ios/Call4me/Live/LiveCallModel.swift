import Foundation

/// State for one live call: captions, summaries, phase, and user actions.
@MainActor
final class LiveCallModel: ObservableObject {
    let callId: String

    @Published private(set) var session: CallSession?
    @Published private(set) var backendState = "DIALING"
    @Published private(set) var captions: [Caption] = []
    @Published private(set) var latestSummary: String?
    @Published var alert: InAppAlert?
    @Published private(set) var takeoverReason: String?
    @Published private(set) var holdCheckMinutes: Int?
    @Published private(set) var holdStartedAt: Date?
    @Published private(set) var result: CallResult?
    @Published var isTakingOver = false
    @Published private(set) var connected = true
    @Published var errorText: String?

    var phase: CallPhase { CallPhase(backendState: backendState) }

    private let api = APIClient()
    private let router: AppRouter
    private var socket: LiveSocket?
    /// Lines the user typed, kept briefly so the server echo isn't shown twice.
    private var pendingEchoes: [String] = []

    init(callId: String, router: AppRouter = .shared) {
        self.callId = callId
        self.router = router
    }

    // MARK: Lifecycle

    func start() {
        guard socket == nil else { return }
        let socket = LiveSocket(callId: callId)
        socket.onEvent = { [weak self] in self?.handle($0) }
        socket.onConnectionChange = { [weak self] in self?.connected = $0 }
        socket.start()
        self.socket = socket
        Task { await loadSession() }
    }

    func stop() {
        socket?.stop()
        socket = nil
    }

    private func loadSession() async {
        guard let detail = try? await api.getCall(callId) else { return }
        session = detail.session
        apply(state: detail.session.state, reason: nil, quiet: true)
        LiveActivityManager.shared.start(session: detail.session)
        if CallPhase(backendState: detail.session.state).isFinished, result == nil {
            finish(summary: detail.session.outcome, reference: detail.session.referenceNumber,
                   minutes: detail.session.minutesSaved)
        }
    }

    // MARK: Live events

    func handle(_ event: LiveEvent) {
        switch event {
        case let .caption(speaker, text, isFinal):
            addCaption(speaker: speaker, text: text, isFinal: isFinal)
        case let .state(_, to, reason):
            apply(state: to, reason: reason, quiet: false)
        case let .summary(text):
            latestSummary = text
        case let .askUser(requestId, question, sensitivity):
            Haptics.attention()
            router.show(AskRequest(requestId: requestId, sessionId: callId,
                                   question: question, sensitivity: sensitivity))
        case let .event(kind, reason, data):
            handleEvent(kind: kind, reason: reason, data: data)
        case let .ended(summary, reference, minutes):
            finish(summary: summary, reference: reference, minutes: minutes)
        }
    }

    private func addCaption(speaker: Speaker, text: String, isFinal: Bool) {
        if speaker == .user, let i = pendingEchoes.firstIndex(of: text) {
            pendingEchoes.remove(at: i)
            return
        }
        if let last = captions.last, last.speaker == speaker, !last.isFinal {
            captions[captions.count - 1].text = text
            captions[captions.count - 1].isFinal = isFinal
        } else {
            captions.append(Caption(speaker: speaker, text: text, isFinal: isFinal))
        }
    }

    private func apply(state: String, reason: String?, quiet: Bool) {
        let old = phase
        backendState = state
        let new = phase
        if new == .onHold {
            if old != .onHold || holdStartedAt == nil { holdStartedAt = Date() }
        } else {
            holdStartedAt = nil
        }
        LiveActivityManager.shared.update(backendState: state, detail: reason)
        // Keep an optimistic takeover until the server confirms or leaves TAKEOVER.
        if new == .typing {
            isTakingOver = true
        } else if old == .typing || new == .done {
            isTakingOver = false
        }
        if old == .needsInput, new != .needsInput, new != .done { router.clearWaitingAsk() }
        guard !quiet, old != new else { return }
        switch new {
        case .personAnswered:
            show(.good, "A person answered", "The AI is talking to them now.")
        case .needsInput:
            show(.urgent, "Needs your input", nil)
        case .onHold where old != .onHold:
            show(.info, "On hold", "We will tell you when a person answers.")
        default:
            break
        }
    }

    private func handleEvent(kind: String, reason: String?, data: [String: JSONValue]) {
        switch kind {
        case "takeover_suggested":
            takeoverReason = reason ?? "The AI needs you to type."
            show(.urgent, "Please take over", reason)
        case "guard_denied", "guard.denied":
            show(.info, "AI said no for you", reason ?? "It was outside your limits.")
        case "max_hold":
            holdCheckMinutes = Int(data.double("hold_minutes") ?? 0)
            show(.urgent, "Still on hold", "Keep waiting?")
        case "callback_offered":
            show(.info, "They offered a callback", reason)
        default:
            break
        }
    }

    private func finish(summary: String?, reference: String?, minutes: Double?) {
        let failed = backendState == "FAILED" || backendState == "VOICEMAIL"
        result = CallResult(
            outcome: summary ?? (failed ? "The call did not go through." : "The call is finished."),
            referenceNumber: reference,
            minutesSaved: minutes ?? session?.minutesSaved,
            failed: failed
        )
        isTakingOver = false
        Haptics.success()
        LiveActivityManager.shared.end(summary: summary)
        stop()
        if minutes == nil { Task { await refreshResult() } }
    }

    /// `ended` may not carry minutes saved; the session row might.
    private func refreshResult() async {
        guard let detail = try? await api.getCall(callId), var r = result else { return }
        r.minutesSaved = r.minutesSaved ?? detail.session.minutesSaved
        r.referenceNumber = r.referenceNumber ?? detail.session.referenceNumber
        result = r
    }

    private func show(_ kind: InAppAlert.Kind, _ title: String, _ message: String?) {
        switch kind {
        case .urgent: Haptics.attention()
        case .good: Haptics.success()
        case .info: Haptics.tap()
        }
        alert = InAppAlert(kind: kind, title: title, message: message)
    }

    // MARK: User actions

    func takeOver() async {
        Haptics.tap()
        isTakingOver = true  // stop showing agent controls immediately
        takeoverReason = nil
        await run { try await self.api.takeover(self.callId) }
    }

    func handBack() async {
        Haptics.tap()
        isTakingOver = false
        await run { try await self.api.handback(self.callId) }
    }

    func cancelCall() async {
        await run { try await self.api.cancel(self.callId) }
    }

    /// Type-to-talk: spoken verbatim to the rep.
    func say(_ text: String) async -> Bool {
        let line = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !line.isEmpty else { return false }
        pendingEchoes.append(line)
        captions.append(Caption(speaker: .user, text: line, isFinal: true))
        return await run { try await self.api.type(self.callId, text: line) }
    }

    func answerHold(keepWaiting: Bool) async {
        holdCheckMinutes = nil
        await run { try await self.api.continueHold(self.callId, keepWaiting: keepWaiting) }
    }

    /// Sends an ask_user answer once. Nothing is stored.
    func answer(_ ask: AskRequest, text: String) async -> Bool {
        let ok = await run { try await self.api.answer(requestId: ask.requestId, answer: text) }
        if ok { router.markAnswered(ask.requestId) }
        return ok
    }

    @discardableResult
    private func run(_ work: @escaping () async throws -> Void) async -> Bool {
        do {
            try await work()
            errorText = nil
            return true
        } catch {
            errorText = (error as? LocalizedError)?.errorDescription ?? "Could not reach the server."
            Haptics.error()
            return false
        }
    }
}
