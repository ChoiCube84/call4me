import ActivityKit
import SwiftUI
import WidgetKit

/// Lock screen banner + Dynamic Island for an active call.
struct CallLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: CallActivityAttributes.self) { context in
            LockScreenCallView(company: context.attributes.company, state: context.state)
                .activityBackgroundTint(context.state.phase.needsAttention ? Color.orange.opacity(0.35) : nil)
                .widgetURL(deepLink(context.attributes.sessionId))
        } dynamicIsland: { context in
            let state = context.state
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Image(systemName: state.phase.symbol)
                        .font(.title2)
                        .foregroundStyle(PhaseColor.color(state.phase))
                }
                DynamicIslandExpandedRegion(.trailing) {
                    PhaseTimer(state: state)
                        .font(.title3.monospacedDigit())
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(state.phase.title)
                        .font(.headline)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    Text(state.detail ?? context.attributes.company)
                        .font(.body)
                        .lineLimit(2)
                }
            } compactLeading: {
                Image(systemName: state.phase.symbol)
                    .foregroundStyle(PhaseColor.color(state.phase))
            } compactTrailing: {
                PhaseTimer(state: state)
                    .monospacedDigit()
                    .frame(maxWidth: 52)
            } minimal: {
                Image(systemName: state.phase.symbol)
                    .foregroundStyle(PhaseColor.color(state.phase))
            }
            .widgetURL(deepLink(context.attributes.sessionId))
        }
    }

    private func deepLink(_ sessionId: String) -> URL? {
        URL(string: "call4me://call/\(sessionId)")
    }
}
