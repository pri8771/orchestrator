import CoreHaptics
import UIKit

@MainActor
protocol PhaseHapticPlaying {
    func playPhaseStartHaptic()
    func playPhaseCompletionHaptic()
}

@MainActor
struct HapticManager: PhaseHapticPlaying {
    func playPhaseStartHaptic() {
        playImpact(style: .light)
    }

    func playPhaseCompletionHaptic() {
        playImpact(style: .medium)
    }

    private func playImpact(style: UIImpactFeedbackGenerator.FeedbackStyle) {
        guard CHHapticEngine.capabilitiesForHardware().supportsHaptics else {
            return
        }

        let generator = UIImpactFeedbackGenerator(style: style)
        generator.prepare()
        generator.impactOccurred()
    }
}
