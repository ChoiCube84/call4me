import ActivityKit
import Foundation

/// Starts, updates, and ends the call Live Activity. The backend also updates
/// it by APNs using the push token we send to `POST /devices`.
@MainActor
final class LiveActivityManager {
    static let shared = LiveActivityManager()

    private var activity: Activity<CallActivityAttributes>?
    private var content: CallActivityAttributes.ContentState?

    var isEnabled: Bool { ActivityAuthorizationInfo().areActivitiesEnabled }

    func start(session: CallSession) {
        guard isEnabled, activity?.attributes.sessionId != session.id else { return }
        let state = CallActivityAttributes.ContentState(
            state: session.state,
            startedAt: session.startedAt.timeIntervalSince1970,
            holdStartedAt: nil,
            detail: session.company
        )
        let attrs = CallActivityAttributes(sessionId: session.id, company: session.company)
        do {
            let activity = try Activity.request(
                attributes: attrs,
                content: ActivityContent(state: state, staleDate: nil),
                pushType: .token
            )
            self.activity = activity
            self.content = state
            observePushToken(activity)
        } catch {
            // Live Activities off or limit reached; the in-app view still works.
        }
    }

    /// Update from a live `state` message while the app is open.
    func update(backendState: String, detail: String? = nil) {
        guard let activity, var next = content else { return }
        let wasOnHold = next.phase == .onHold
        next.state = backendState
        if next.phase == .onHold, !wasOnHold {
            next.holdStartedAt = Date().timeIntervalSince1970
        } else if next.phase != .onHold {
            next.holdStartedAt = nil
        }
        next.detail = detail ?? next.detail
        content = next
        let alert = next.phase.needsAttention
            ? AlertConfiguration(title: "call4me", body: "\(next.phase.title)", sound: .default)
            : nil
        Task { await activity.update(ActivityContent(state: next, staleDate: nil), alertConfiguration: alert) }
    }

    func end(summary: String?) {
        guard let activity, var final = content else { return }
        final.state = "ENDED"
        final.holdStartedAt = nil
        final.detail = summary ?? "Call finished"
        self.activity = nil
        content = nil
        Task {
            await activity.end(ActivityContent(state: final, staleDate: nil),
                               dismissalPolicy: .after(Date().addingTimeInterval(15 * 60)))
        }
    }

    private func observePushToken(_ activity: Activity<CallActivityAttributes>) {
        Task {
            for await token in activity.pushTokenUpdates {
                PushRegistrar.sendLiveActivityToken(token)
            }
        }
    }
}
