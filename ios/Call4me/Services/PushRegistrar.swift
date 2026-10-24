import UIKit
import UserNotifications

/// Asks for notification permission and sends device tokens to `POST /devices`.
enum PushRegistrar {
    /// Request permission and register for APNs. Alerts are always a banner
    /// plus haptic; sound is optional extra for users with some hearing.
    /// Time-sensitive delivery comes from the entitlement + payload.
    @MainActor
    static func requestPermission() async -> Bool {
        let center = UNUserNotificationCenter.current()
        let granted = (try? await center.requestAuthorization(options: [.alert, .badge, .sound])) ?? false
        if granted { UIApplication.shared.registerForRemoteNotifications() }
        return granted
    }

    /// Call on launch if permission was already given.
    @MainActor
    static func registerIfAllowed() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        if settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional {
            UIApplication.shared.registerForRemoteNotifications()
        }
    }

    static func sendAPNsToken(_ token: Data) {
        send(DeviceRegistration(userId: AppSettings.shared.userId, apnsToken: token.hexString))
    }

    static func sendLiveActivityToken(_ token: Data) {
        send(DeviceRegistration(userId: AppSettings.shared.userId, liveActivityToken: token.hexString))
    }

    private static func send(_ reg: DeviceRegistration) {
        Task {
            // Endpoint may not exist on older backends; failure is not fatal.
            try? await APIClient().registerDevice(reg)
        }
    }
}

extension Data {
    var hexString: String { map { String(format: "%02x", $0) }.joined() }
}
