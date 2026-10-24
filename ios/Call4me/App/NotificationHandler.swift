import UserNotifications

/// Handles pushes in the foreground and notification action buttons
/// (including the text-input answer from the lock screen).
final class NotificationHandler: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationHandler()
    private let api = APIClient()

    // Foreground: always show a banner and buzz. Never sound-only.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        let payload = payload(for: notification)
        await MainActor.run {
            if payload.isUrgent { Haptics.attention() } else { Haptics.success() }
            if let ask = payload.askRequest { AppRouter.shared.show(ask) }
        }
        return [.banner, .list, .badge]
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        let payload = payload(for: response.notification)
        switch response.actionIdentifier {
        case PushAction.answer:
            guard let text = (response as? UNTextInputNotificationResponse)?.userText else { return }
            await sendAnswer(text, payload: payload)
        case PushAction.keepWaiting, PushAction.stopWaiting:
            guard let id = payload.sessionId else { return }
            try? await api.continueHold(id, keepWaiting: response.actionIdentifier == PushAction.keepWaiting)
        case PushAction.takeOver:
            guard let id = payload.sessionId else { return }
            await MainActor.run {
                AppRouter.shared.open(callId: id)
                AppRouter.shared.wantsTakeover = true
            }
        default:
            await MainActor.run {
                if let id = payload.sessionId { AppRouter.shared.open(callId: id) }
                if let ask = payload.askRequest { AppRouter.shared.show(ask) }
            }
        }
    }

    /// Posts the answer once. The text is not stored or logged anywhere.
    private func sendAnswer(_ text: String, payload: PushPayload) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let requestId = payload.requestId, !trimmed.isEmpty else { return }
        do {
            try await api.answer(requestId: requestId, answer: trimmed)
            await MainActor.run { AppRouter.shared.markAnswered(requestId) }
        } catch {
            await notifyAnswerFailed(sessionId: payload.sessionId)
        }
    }

    /// Visible fallback if the answer did not reach the server.
    private func notifyAnswerFailed(sessionId: String?) async {
        let content = UNMutableNotificationContent()
        content.title = "Answer not sent"
        content.body = "Open call4me and try again."
        content.interruptionLevel = .timeSensitive
        if let sessionId { content.userInfo = ["session_id": sessionId] }
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        try? await UNUserNotificationCenter.current().add(request)
    }

    private func payload(for notification: UNNotification) -> PushPayload {
        let content = notification.request.content
        return PushPayload(category: content.categoryIdentifier, userInfo: content.userInfo, body: content.body)
    }
}
