import Combine
import SwiftUI

/// The entire home screen: no tab bar, no nav stack. The ring IS the app.
/// Settings and History are auxiliary and only ever appear as sheets.
struct ContentView: View {
    @Environment(TimerEngine.self) private var engine
    @Environment(\.scenePhase) private var scenePhase

    @State private var showingSettings = false
    @State private var showingHistory = false
    @State private var justReconciled = false

    // Drives the once-a-second UI refresh; `TimerEngine` itself owns no ticking
    // timer, only the reconciliation math, so the view is what has to ask
    // "what time is it now" on a cadence. Connected/disconnected explicitly
    // (not `.autoconnect()`) so it stops dead on pause and on backgrounding
    // instead of ticking uselessly off-screen.
    @State private var now = Date()
    @State private var tickerConnection: (any Cancellable)?
    private let ticker = Timer.publish(every: 1, on: .main, in: .common)

    var body: some View {
        VStack(spacing: 28) {
            toolbar

            Spacer()

            TimerRingView(
                phase: engine.phase,
                progress: progress,
                remainingSeconds: Int(remainingTime.rounded(.up))
            )
            .padding(.horizontal, 36)
            .scaleEffect(justReconciled ? 1.05 : 1.0)
            .opacity(justReconciled ? 0.82 : 1.0)
            .animation(.spring(response: 0.4, dampingFraction: 0.7), value: justReconciled)

            Text(cycleLabel)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)

            Spacer()

            controls
                .padding(.bottom, 36)
        }
        .padding(.top, 8)
        .onReceive(ticker) { firedAt in
            now = firedAt
            reconcileWithTransition()
        }
        .onAppear {
            reconcileWithTransition()
            syncTicker()
        }
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .active {
                reconcileWithTransition()
                syncTicker()
            } else {
                stopTicker()
            }
        }
        .onChange(of: engine.isPaused) { _, _ in
            syncTicker()
        }
        .sheet(isPresented: $showingSettings) {
            SettingsView()
        }
        .sheet(isPresented: $showingHistory) {
            HistoryView()
        }
    }

    // MARK: - Pieces

    private var toolbar: some View {
        HStack {
            Button {
                showingHistory = true
            } label: {
                Image(systemName: "list.bullet")
                    .font(.title2)
            }
            .accessibilityLabel("Session History")

            Spacer()

            Button {
                showingSettings = true
            } label: {
                Image(systemName: "gearshape")
                    .font(.title2)
            }
            .accessibilityLabel("Settings")
        }
        .padding(.horizontal, 24)
    }

    private var controls: some View {
        HStack(spacing: 20) {
            Button {
                engine.reset()
            } label: {
                Label("Reset", systemImage: "arrow.counterclockwise")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .controlSize(.large)

            Button {
                if engine.isPaused {
                    // A phase that never progressed (fresh launch, or right after
                    // Reset) must go through start() — resume() only shifts the
                    // existing clock and skips both the phase-start haptic and
                    // picking up a duration changed in Settings while paused.
                    if isFreshPhase {
                        engine.start()
                    } else {
                        engine.resume()
                    }
                } else {
                    engine.pause()
                }
            } label: {
                Label(
                    engine.isPaused ? "Start" : "Pause",
                    systemImage: engine.isPaused ? "play.fill" : "pause.fill"
                )
                .font(.headline)
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
        .padding(.horizontal, 24)
    }

    // MARK: - Derived display state
    // Deliberately thin: these read TimerEngine's computed remaining-time surface,
    // they don't reimplement the countdown math (that math lives in TimerEngine only).

    private var remainingTime: TimeInterval {
        engine.remainingTimeInterval(at: now)
    }

    /// True while paused at the very beginning of a phase (never started, or
    /// just Reset) — as opposed to paused partway through a running phase.
    private var isFreshPhase: Bool {
        engine.isPaused && remainingTime >= engine.phaseDuration
    }

    private var progress: Double {
        guard engine.phaseDuration > 0 else { return 0 }
        return 1 - (remainingTime / engine.phaseDuration)
    }

    private var cycleLabel: String {
        switch engine.phase {
        case .work:
            let position = (engine.completedWorkSessions % max(engine.sessionsBeforeLongBreak, 1)) + 1
            return "Work \(position) of \(engine.sessionsBeforeLongBreak)"
        case .shortBreak, .longBreak:
            return "Cycle \(engine.completedWorkSessions)"
        }
    }

    private func reconcileWithTransition() {
        let phaseBefore = engine.phase
        engine.reconcile()
        if engine.phase != phaseBefore {
            justReconciled = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                justReconciled = false
            }
        }
    }

    /// Connects or disconnects the once-a-second ticker to match run state.
    /// Only ticks while both running (not paused) and foregrounded — otherwise
    /// it'd burn wakeups updating a ring nobody can see, or one that's
    /// deliberately holding still.
    private func syncTicker() {
        guard scenePhase == .active, !engine.isPaused else {
            stopTicker()
            return
        }
        guard tickerConnection == nil else { return }
        now = Date()
        tickerConnection = ticker.connect()
    }

    private func stopTicker() {
        tickerConnection?.cancel()
        tickerConnection = nil
    }
}
