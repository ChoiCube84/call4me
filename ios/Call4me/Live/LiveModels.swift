import Foundation

struct Caption: Identifiable, Equatable {
    let id = UUID()
    let speaker: Speaker
    var text: String
    var isFinal: Bool
}

/// A visible in-app alert. Always shown with a haptic.
struct InAppAlert: Identifiable, Equatable {
    enum Kind { case info, good, urgent }
    let id = UUID()
    let kind: Kind
    let title: String
    let message: String?
}

struct CallResult: Equatable {
    var outcome: String
    var referenceNumber: String?
    var minutesSaved: Double?
    var failed: Bool
}
