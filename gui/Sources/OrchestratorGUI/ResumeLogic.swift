import Foundation

// GUI auto-resume for crashed runs (design: adversarial review 2026-07-17).
//
// A run that dies without cleanup (crash, SIGKILL, reboot) leaves its per-app
// engine lock (<root>/.orch-locks/<app>.lock) behind with a dead pid — until
// this feature, the GUI treated mere lock presence as "running", so a corpse
// stayed pinned in the Running section forever, and only shepherd.sh would
// ever relaunch it. The GUI now DETECTS the stale lock (same dead-pid rule as
// shepherd's locked()) and OFFERS a one-click resume — it never auto-launches:
// the GUI has zero automatic launch paths today, crash-loop accounting lives
// in shepherd's persistent retry counter (not here), and when shepherd is
// active it already relaunches within ~2 minutes. A click is an explicit user
// action, exactly like the existing Run button.
//
// Everything in this file is pure (no store, no I/O beyond the injected pid
// probe) so ResumeLogicTests can cover the decision table without a UI.

/// One crashed-run offer, derived each refresh tick. `deadPid == nil` means
/// the lock names no pid at all — the GUI cannot verify liveness, and the
/// banner copy must say so rather than pretend (the interface never lies).
struct ResumeOffer: Equatable, Identifiable {
    var id: String { name }
    let name: String
    let deadPid: Int32?
    let since: Date
}

enum ResumeAdvisor {

    /// Offer candidates: stale-locked apps minus every exclusion.
    /// - autorunDisabled: operator parked the app — NEVER offered (the
    ///   8dab091 invariant, enforced at the offer layer and re-checked at
    ///   click time).
    /// - doneOrMissing: a leftover lock on a finished/removed project is
    ///   cleanup, not a resume.
    /// - guiOwnedLive: this GUI session holds a live Process handle — its
    ///   terminationHandler owns that lifecycle.
    /// - queuedOrLaunching: already on its way back up.
    /// - manuallyStopped: the operator just stopped it — stopRun's 5s
    ///   SIGTERM→SIGKILL grace window must not read as a crash.
    static func candidates(staleLocks: Set<String>,
                           locks: [String: AppLockInfo],
                           autorunDisabled: Set<String>,
                           doneOrMissing: Set<String>,
                           guiOwnedLive: Set<String>,
                           queuedOrLaunching: Set<String>,
                           manuallyStopped: Set<String>) -> [ResumeOffer] {
        staleLocks
            .subtracting(autorunDisabled)
            .subtracting(doneOrMissing)
            .subtracting(guiOwnedLive)
            .subtracting(queuedOrLaunching)
            .subtracting(manuallyStopped)
            .sorted()
            .map { name in
                let lock = locks[name]
                return ResumeOffer(name: name,
                                   deadPid: lock?.pid,
                                   since: lock?.since ?? Date.distantPast)
            }
    }

    /// Staleness must SETTLE before an offer shows: the engine creates the
    /// lock O_EXCL and then writes the payload, so a scan between the two
    /// parses a nil pid and would flicker a one-tick false "crashed". Stamp
    /// when a name first went stale, prune the moment it stops being stale
    /// (so a re-crash restarts the window).
    static func settledFirstSeen(previous: [String: Date],
                                 nowStale: Set<String>,
                                 now: Date) -> [String: Date] {
        var out: [String: Date] = [:]
        for name in nowStale {
            out[name] = previous[name] ?? now
        }
        return out
    }

    /// Names whose staleness has persisted for at least `window` seconds
    /// (two 1.5s refresh ticks by default, with margin).
    static func settled(_ firstSeen: [String: Date],
                        now: Date,
                        window: TimeInterval = 3.5) -> Set<String> {
        Set(firstSeen.filter { now.timeIntervalSince($0.value) >= window }.keys)
    }

    /// Whether a resume may launch immediately instead of queueing.
    /// `orchestratorRunning` is a 240s state-mtime heuristic, so a freshly
    /// crashed app counts ITSELF as running for up to 4 minutes — a plain
    /// runOrQueue would park the resume in the queue behind the corpse it is
    /// resuming. Launch immediately iff nothing else is running or queued:
    /// the only "running" project may be the crashed app itself (its dead pid
    /// was just verified; the engine's own lock arbitration backstops us).
    static func immediateLaunchAllowed(resuming name: String,
                                       runningProjectNames: Set<String>,
                                       launchingName: String?,
                                       queueEmpty: Bool) -> Bool {
        launchingName == nil && queueEmpty
            && runningProjectNames.subtracting([name]).isEmpty
    }

    /// Banner copy, honest about what the GUI actually knows. Dead pid: state
    /// the fact. Nil pid: admit the lock can't be verified. Shepherd active:
    /// say it should relaunch on its own so "Resume now" is a shortcut, not
    /// the only path.
    static func bannerText(deadPid: Int32?,
                           since: Date,
                           shepherdActive: Bool,
                           now: Date = Date()) -> String {
        var text: String
        if let pid = deadPid {
            text = "This run's process (pid \(pid)) is gone — started \(elapsedDescription(since: since, now: now))."
        } else {
            text = "This app's run lock names no pid — the GUI can't verify whether it is still running."
        }
        if shepherdActive {
            text += " Shepherd is active and should relaunch it within ~2 minutes."
        }
        return text
    }

    /// "5 minutes ago" with integer math — deterministic for tests, no
    /// formatter locale/calendar dependence.
    static func elapsedDescription(since: Date, now: Date) -> String {
        let secs = max(0, Int(now.timeIntervalSince(since)))
        if secs < 60 { return "moments ago" }
        let mins = secs / 60
        if mins < 60 { return mins == 1 ? "1 minute ago" : "\(mins) minutes ago" }
        let hours = mins / 60
        if hours < 24 { return hours == 1 ? "1 hour ago" : "\(hours) hours ago" }
        let days = hours / 24
        return days == 1 ? "1 day ago" : "\(days) days ago"
    }
}
