import CoreHaptics
import UIKit

/// Haptic cues. Every alert in the app pairs one of these with a visual
/// change; we never rely on sound (CLAUDE.md rule 7).
enum Haptics {
    /// Something good happened (person answered, call done).
    static func success() { notify(.success) }

    /// Look at your phone (needs input, please take over, hold check).
    static func attention() {
        notify(.warning)
        StrongBuzz.shared.play()
    }

    static func error() { notify(.error) }

    static func tap() {
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    private static func notify(_ type: UINotificationFeedbackGenerator.FeedbackType) {
        let g = UINotificationFeedbackGenerator()
        g.prepare()
        g.notificationOccurred(type)
    }
}

/// A longer, stronger CoreHaptics pattern (three firm pulses) for alerts the
/// user must not miss. Falls back silently on devices without haptics.
final class StrongBuzz {
    static let shared = StrongBuzz()
    private var engine: CHHapticEngine?

    private init() {
        guard CHHapticEngine.capabilitiesForHardware().supportsHaptics else { return }
        engine = try? CHHapticEngine()
        engine?.resetHandler = { [weak self] in try? self?.engine?.start() }
    }

    func play() {
        guard let engine else { return }
        let events = (0..<3).map { i in
            CHHapticEvent(
                eventType: .hapticContinuous,
                parameters: [
                    CHHapticEventParameter(parameterID: .hapticIntensity, value: 1.0),
                    CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.6),
                ],
                relativeTime: Double(i) * 0.35,
                duration: 0.2
            )
        }
        do {
            try engine.start()
            let player = try engine.makePlayer(with: CHHapticPattern(events: events, parameters: []))
            try player.start(atTime: CHHapticTimeImmediate)
        } catch {
            // No haptics available; the visual alert still shows.
        }
    }
}
