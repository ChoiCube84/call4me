import UserNotifications

/// Push categories sent by the backend (`aps.category`) and their actions.
enum PushCategory {
    static let askUser = "ASK_USER"
    static let humanAnswered = "HUMAN_ANSWERED"
    static let holdContinue = "HOLD_CONTINUE"
    static let callback = "CALLBACK"
    static let callEnded = "CALL_ENDED"
    static let takeover = "TAKEOVER"
}

enum PushAction {
    static let answer = "ANSWER"
    static let keepWaiting = "KEEP_WAITING"
    static let stopWaiting = "STOP_WAITING"
    static let takeOver = "TAKE_OVER"
    static let open = "OPEN"
}

enum NotificationCategories {
    static func register() {
        let answer = UNTextInputNotificationAction(
            identifier: PushAction.answer,
            title: "Answer",
            options: [],
            textInputButtonTitle: "Send",
            textInputPlaceholder: "Type your answer"
        )
        let open = UNNotificationAction(identifier: PushAction.open, title: "Open", options: [.foreground])
        let keep = UNNotificationAction(identifier: PushAction.keepWaiting, title: "Keep waiting", options: [])
        let stop = UNNotificationAction(identifier: PushAction.stopWaiting, title: "Stop", options: [.destructive])
        let takeOver = UNNotificationAction(identifier: PushAction.takeOver, title: "Take over", options: [.foreground])

        let categories: Set<UNNotificationCategory> = [
            UNNotificationCategory(identifier: PushCategory.askUser, actions: [answer, open], intentIdentifiers: []),
            UNNotificationCategory(identifier: PushCategory.humanAnswered, actions: [open, takeOver], intentIdentifiers: []),
            UNNotificationCategory(identifier: PushCategory.holdContinue, actions: [keep, stop], intentIdentifiers: []),
            UNNotificationCategory(identifier: PushCategory.callback, actions: [open], intentIdentifiers: []),
            UNNotificationCategory(identifier: PushCategory.callEnded, actions: [open], intentIdentifiers: []),
            UNNotificationCategory(identifier: PushCategory.takeover, actions: [takeOver, open], intentIdentifiers: []),
        ]
        UNUserNotificationCenter.current().setNotificationCategories(categories)
    }
}
