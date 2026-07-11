import XCTest
@testable import FocusFlow

@MainActor
final class TimerEngineTests: XCTestCase {
    func testCoachLabelUsesWorkCycleCountAndBreakModeLabels() {
        let startDate = Date(timeIntervalSince1970: 500)
        var currentDate = startDate
        let engine = TimerEngine(now: { currentDate })

        XCTAssertEqual(engine.coachLabel, "Work 1 of 4")

        engine.start()
        currentDate = startDate.addingTimeInterval(25 * 60)
        engine.reconcile()

        XCTAssertEqual(engine.phase, .shortBreak)
        XCTAssertEqual(engine.coachLabel, "Short Break")

        currentDate = startDate.addingTimeInterval(25 * 60 + 5 * 60)
        engine.reconcile()

        XCTAssertEqual(engine.phase, .work)
        XCTAssertEqual(engine.completedWorkSessions, 1)
        XCTAssertEqual(engine.coachLabel, "Work 2 of 4")
    }

    func testConfigurationChangesAreClampedToPositiveDurations() {
        let engine = TimerEngine(
            workDurationMinutes: 0,
            shortBreakDurationMinutes: -3,
            longBreakDurationMinutes: 0,
            sessionsBeforeLongBreak: 0
        )

        XCTAssertEqual(engine.workDurationMinutes, 1)
        XCTAssertEqual(engine.shortBreakDurationMinutes, 1)
        XCTAssertEqual(engine.longBreakDurationMinutes, 1)
        XCTAssertEqual(engine.sessionsBeforeLongBreak, 1)
    }

    func testConfigurationUpdatesActivePhaseDurationImmediately() {
        let startDate = Date(timeIntervalSince1970: 750)
        var currentDate = startDate
        let engine = TimerEngine(now: { currentDate })

        engine.start()
        currentDate = startDate.addingTimeInterval(10 * 60)

        engine.updateConfiguration(
            workDurationMinutes: 30,
            shortBreakDurationMinutes: 7,
            longBreakDurationMinutes: 20,
            sessionsBeforeLongBreak: 6
        )

        XCTAssertEqual(engine.phaseDuration, 30 * 60)
        XCTAssertEqual(engine.remainingWholeSeconds, 20 * 60)
        XCTAssertEqual(engine.sessionsBeforeLongBreak, 6)
    }

    func testConfigurationEditsDoNotUndoAnAlreadyCompletedActivePhase() {
        let startDate = Date(timeIntervalSince1970: 900)
        var currentDate = startDate
        let recorder = RecorderSpy()
        let engine = TimerEngine(
            now: { currentDate },
            sessionRecorder: recorder
        )

        engine.start()
        currentDate = startDate.addingTimeInterval(25 * 60 + 1)

        engine.updateConfiguration(
            workDurationMinutes: 30,
            shortBreakDurationMinutes: 7,
            longBreakDurationMinutes: 20,
            sessionsBeforeLongBreak: 6
        )

        XCTAssertEqual(engine.remainingWholeSeconds, 0)

        engine.reconcile()

        XCTAssertEqual(recorder.entries.count, 1)
        XCTAssertEqual(recorder.entries.first?.durationSeconds, 25 * 60)
        XCTAssertEqual(engine.phase, .shortBreak)
    }

    func testReconcileLogsSingleCompletionAndAdvancesExactlyOnePhase() {
        let startDate = Date(timeIntervalSince1970: 1_000)
        var currentDate = startDate
        let recorder = RecorderSpy()
        let haptics = HapticsSpy()
        let engine = TimerEngine(
            now: { currentDate },
            haptics: haptics,
            sessionRecorder: recorder
        )

        engine.start()
        currentDate = startDate.addingTimeInterval(25 * 60)
        engine.reconcile()

        XCTAssertEqual(recorder.entries.count, 1)
        XCTAssertEqual(recorder.entries.first?.completedAt, startDate.addingTimeInterval(25 * 60))
        XCTAssertEqual(recorder.entries.first?.mode, .work)
        XCTAssertEqual(recorder.entries.first?.durationSeconds, 25 * 60)
        XCTAssertEqual(engine.phase, .shortBreak)
        XCTAssertEqual(engine.completedWorkSessions, 1)
        XCTAssertEqual(engine.phaseDuration, 5 * 60)
        XCTAssertFalse(engine.isPaused)
        XCTAssertEqual(haptics.phaseStartCount, 2)
        XCTAssertEqual(haptics.phaseCompletionCount, 1)
    }

    func testResumeFoldsPausedTimeWithoutChangingRemainingWorkTime() {
        let startDate = Date(timeIntervalSince1970: 2_000)
        var currentDate = startDate
        let engine = TimerEngine(now: { currentDate })

        engine.start()
        currentDate = startDate.addingTimeInterval(10 * 60)
        engine.pause()
        currentDate = startDate.addingTimeInterval(17 * 60)

        XCTAssertEqual(engine.remainingWholeSeconds, 15 * 60)

        engine.resume()

        XCTAssertFalse(engine.isPaused)
        XCTAssertEqual(engine.remainingWholeSeconds, 15 * 60)
    }

    func testReconcileStopsAfterOnePhaseEvenIfBackgroundedPastMultipleBoundaries() {
        let startDate = Date(timeIntervalSince1970: 3_000)
        var currentDate = startDate
        let recorder = RecorderSpy()
        let engine = TimerEngine(
            now: { currentDate },
            sessionRecorder: recorder
        )

        engine.start()
        currentDate = startDate.addingTimeInterval(40 * 60)
        engine.reconcile()

        XCTAssertEqual(recorder.entries.count, 1)
        XCTAssertEqual(engine.phase, .shortBreak)
        XCTAssertEqual(engine.completedWorkSessions, 1)
        XCTAssertEqual(engine.phaseDuration, 5 * 60)

        engine.reconcile()

        XCTAssertEqual(recorder.entries.count, 1)
        XCTAssertEqual(engine.phase, .shortBreak)
    }

    func testResetOnAPausedOrNeverStartedTimerStaysPaused() {
        let startDate = Date(timeIntervalSince1970: 4_500)
        let currentDate = startDate
        let engine = TimerEngine(now: { currentDate })

        XCTAssertTrue(engine.isPaused)

        engine.reset()

        XCTAssertTrue(engine.isPaused)
        XCTAssertEqual(engine.remainingWholeSeconds, 25 * 60)
    }

    func testResetDoesNotWriteHistoryOrChangeCycleCount() {
        let startDate = Date(timeIntervalSince1970: 4_000)
        var currentDate = startDate
        let recorder = RecorderSpy()
        let engine = TimerEngine(
            now: { currentDate },
            sessionRecorder: recorder
        )

        engine.start()
        currentDate = startDate.addingTimeInterval(6 * 60)
        engine.reset()

        XCTAssertEqual(recorder.entries.count, 0)
        XCTAssertEqual(engine.phase, .work)
        XCTAssertEqual(engine.completedWorkSessions, 0)
        XCTAssertFalse(engine.isPaused)
        XCTAssertEqual(engine.phaseDuration, 25 * 60)
        XCTAssertEqual(engine.remainingWholeSeconds, 25 * 60)
    }
}

@MainActor
private final class RecorderSpy: SessionEntryRecording {
    private(set) var entries: [SessionEntry] = []

    func recordSession(completedAt: Date, mode: Mode, durationSeconds: Int) {
        entries.append(
            SessionEntry(
                completedAt: completedAt,
                mode: mode,
                durationSeconds: durationSeconds
            )
        )
    }
}

@MainActor
private final class HapticsSpy: PhaseHapticPlaying {
    private(set) var phaseStartCount = 0
    private(set) var phaseCompletionCount = 0

    func playPhaseStartHaptic() {
        phaseStartCount += 1
    }

    func playPhaseCompletionHaptic() {
        phaseCompletionCount += 1
    }
}
