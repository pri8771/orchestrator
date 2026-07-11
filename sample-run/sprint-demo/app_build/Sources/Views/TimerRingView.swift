import SwiftUI

/// Pure presentation: draws the circular countdown given already-computed values.
/// Owns no timer, no state — every value is a projection the caller computed from TimerEngine.
struct TimerRingView: View {
    let phase: Mode
    let progress: Double // 0 (just started) ... 1 (complete)
    let remainingSeconds: Int

    private var accentColor: Color {
        switch phase {
        case .work: return .orange
        case .shortBreak: return .mint
        case .longBreak: return .blue
        }
    }

    private var timeLabel: String {
        let minutes = remainingSeconds / 60
        let seconds = remainingSeconds % 60
        return String(format: "%02d:%02d", minutes, seconds)
    }

    var body: some View {
        ZStack {
            Circle()
                .stroke(.quaternary, lineWidth: 14)

            Circle()
                .trim(from: 0, to: min(max(progress, 0), 1))
                .stroke(accentColor, style: StrokeStyle(lineWidth: 14, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .animation(.linear(duration: 1), value: progress)

            VStack(spacing: 8) {
                Text(timeLabel)
                    .font(.system(size: 56, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .contentTransition(.numericText())
                    .animation(.default, value: remainingSeconds)

                Text(phase.displayName)
                    .font(.headline)
                    .foregroundStyle(accentColor)
                    .textCase(.uppercase)
                    .tracking(1.2)
            }
        }
        .aspectRatio(1, contentMode: .fit)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(phase.displayName), \(timeLabel) remaining")
    }
}

#Preview {
    TimerRingView(phase: .work, progress: 0.35, remainingSeconds: 17 * 60 + 42)
        .padding(40)
}
