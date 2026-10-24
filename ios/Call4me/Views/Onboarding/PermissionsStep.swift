import SwiftUI

/// Alerts permission and a check that Live Activities are on.
struct PermissionsStep: View {
    let onDone: () -> Void
    @State private var alertsOn: Bool?
    @State private var liveOn = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                Text("Alerts")
                    .font(.title2.bold())
                Text("We buzz and show a banner when a person answers or when the AI needs you.")
                    .font(.title3)

                BigButton(title: alertsOn == true ? "Alerts are on" : "Turn on alerts",
                          systemImage: alertsOn == true ? "checkmark.circle.fill" : "bell.badge") {
                    Task {
                        alertsOn = await PushRegistrar.requestPermission()
                        Haptics.success()
                    }
                }
                .disabled(alertsOn == true)

                if alertsOn == false {
                    Label("Alerts are off. Turn them on in the Settings app.", systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.orange)
                }

                Label(liveOn ? "Lock screen updates are on." : "Lock screen updates are off. Turn on Live Activities in the Settings app.",
                      systemImage: liveOn ? "checkmark.circle.fill" : "lock.iphone")
                    .font(.body)

                Text("To dictate, tap the mic on the keyboard.")
                    .foregroundStyle(.secondary)

                BigButton(title: "Start using call4me", systemImage: "checkmark", action: onDone)
            }
            .padding()
        }
        .task { liveOn = LiveActivityManager.shared.isEnabled }
    }
}
