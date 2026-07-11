import Foundation
import Observation

@MainActor
@Observable
final class TimerEngine {
    var phase: Mode
    var phaseStartedAt: Date
    var phaseDuration: TimeInterval
    var isPaused: Bool
    var pausedAt: Date?
    var completedWorkSessions: Int

    var workDurationMinutes: Int
    var shortBreakDurationMinutes: Int
    var longBreakDurationMinutes: Int
    var sessionsBeforeLongBreak: Int

    @ObservationIgnored
    private let now: @Sendable () -> Date

    @ObservationIgnored
    private var haptics: any PhaseHapticPlaying

    @ObservationIgnored
    private var sessionRecorder: (any SessionEntryRecording)?

    init(
        workDurationMinutes: Int = 25,
        shortBreakDurationMinutes: Int = 5,
        longBreakDurationMinutes: Int = 15,
        sessionsBeforeLongBreak: Int = 4,
        now: @escaping @Sendable () -> Date = Date.init,
        haptics: (any PhaseHapticPlaying)? = nil,
        sessionRecorder: (any SessionEntryRecording)? = nil
    ) {
        let clampedWork = max(1, workDurationMinutes)
        let clampedShortBreak = max(1, shortBreakDurationMinutes)
        let clampedLongBreak = max(1, longBreakDurationMinutes)
        let clampedSessionCount = max(1, sessionsBeforeLongBreak)
        let currentTime = now()

        self.phase = .work
        self.phaseStartedAt = currentTime
        self.phaseDuration = TimeInterval(clampedWork * 60)
        self.isPaused = true
        self.pausedAt = currentTime
        self.completedWorkSessions = 0
        self.workDurationMinutes = clampedWork
        self.shortBreakDurationMinutes = clampedShortBreak
        self.longBreakDurationMinutes = clampedLongBreak
        self.sessionsBeforeLongBreak = clampedSessionCount
        self.now = now
        self.haptics = haptics ?? HapticManager()
        self.sessionRecorder = sessionRecorder
    }

    var remainingTimeInterval: TimeInterval {
        remainingTimeInterval(at: now())
    }

    var remainingTime: TimeInterval {
        remainingTimeInterval
    }

    var remainingWholeSeconds: Int {
        Int(ceil(remainingTimeInterval))
    }

    var coachLabel: String {
        guard phase == .work else {
            return phase.displayName
        }

        let currentWorkNumber = (completedWorkSessions % sessionsBeforeLongBreak) + 1
        return "Work \(currentWorkNumber) of \(sessionsBeforeLongBreak)"
    }

    func start() {
        guard isPaused else {
            return
        }

        phaseDuration = configuredDuration(for: phase)
        phaseStartedAt = now()
        pausedAt = nil
        isPaused = false
        haptics.playPhaseStartHaptic()
    }

    func pause() {
        guard !isPaused else {
            return
        }

        pausedAt = now()
        isPaused = true
    }

    func resume() {
        guard isPaused else {
            return
        }

        let resumedAt = now()
        let anchor = pausedAt ?? resumedAt
        phaseStartedAt = phaseStartedAt.addingTimeInterval(resumedAt.timeIntervalSince(anchor))
        pausedAt = nil
        isPaused = false
    }

    func reset() {
        phaseDuration = configuredDuration(for: phase)
        let currentTime = now()
        phaseStartedAt = currentTime
        pausedAt = isPaused ? currentTime : nil
    }

    func reconcile() {
        guard !isPaused else {
            return
        }

        let currentTime = now()
        guard isCurrentPhaseComplete(at: currentTime) else {
            return
        }

        let completionDate = phaseStartedAt.addingTimeInterval(phaseDuration)
        completeCurrentPhase(using: completionDate)
        advanceToNextPhase(from: currentTime)
    }

    func updateConfiguration(
        workDurationMinutes: Int,
        shortBreakDurationMinutes: Int,
        longBreakDurationMinutes: Int,
        sessionsBeforeLongBreak: Int
    ) {
        let wasAlreadyComplete = !isPaused && isCurrentPhaseComplete(at: now())

        self.workDurationMinutes = max(1, workDurationMinutes)
        self.shortBreakDurationMinutes = max(1, shortBreakDurationMinutes)
        self.longBreakDurationMinutes = max(1, longBreakDurationMinutes)
        self.sessionsBeforeLongBreak = max(1, sessionsBeforeLongBreak)

        // Keep an in-progress phase in sync, but don't undo a completion that
        // already elapsed while the app was backgrounded.
        if !wasAlreadyComplete {
            phaseDuration = configuredDuration(for: phase)
        }
    }

    func remainingTimeInterval(at referenceDate: Date) -> TimeInterval {
        let effectiveEndDate: Date
        if isPaused {
            effectiveEndDate = pausedAt ?? referenceDate
        } else {
            effectiveEndDate = referenceDate
        }

        let elapsed = max(0, effectiveEndDate.timeIntervalSince(phaseStartedAt))
        return max(0, phaseDuration - elapsed)
    }

    private func configuredDuration(for mode: Mode) -> TimeInterval {
        switch mode {
        case .work:
            return TimeInterval(workDurationMinutes * 60)
        case .shortBreak:
            return TimeInterval(shortBreakDurationMinutes * 60)
        case .longBreak:
            return TimeInterval(longBreakDurationMinutes * 60)
        }
    }

    private func isCurrentPhaseComplete(at currentTime: Date) -> Bool {
        remainingTimeInterval(at: currentTime) <= 0
    }

    private func completeCurrentPhase(using completionDate: Date) {
        sessionRecorder?.recordSession(
            completedAt: completionDate,
            mode: phase,
            durationSeconds: Int(phaseDuration)
        )
        haptics.playPhaseCompletionHaptic()
    }

    private func advanceToNextPhase(from currentTime: Date) {
        let nextPhase = nextPhase(afterCompleting: phase)
        phase = nextPhase
        phaseDuration = configuredDuration(for: nextPhase)
        phaseStartedAt = currentTime
        pausedAt = nil
        isPaused = false
        haptics.playPhaseStartHaptic()
    }

    private func nextPhase(afterCompleting mode: Mode) -> Mode {
        switch mode {
        case .work:
            let completedSessions = completedWorkSessions + 1
            completedWorkSessions = completedSessions
            if completedSessions.isMultiple(of: sessionsBeforeLongBreak) {
                return .longBreak
            }
            return .shortBreak
        case .shortBreak, .longBreak:
            return .work
        }
    }
}
