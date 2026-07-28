import Foundation
import Darwin
import Combine

// README's no-cost promise: engine/CLI child processes must never see
// pay-as-you-go credentials or base-URL overrides — every call counts
// against the logged-in CLIs. ONE canonical list for every GUI launch
// site, kept in lockstep with run.sh's `unset` block (step 2); three
// drifting copies previously left the enrollment launch holding
// GOOGLE_APPLICATION_CREDENTIALS (Vertex billing) and custom base URLs.
enum APIKeyEnv {
    static let strippedAPIKeyVars = [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "GOOGLE_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GENAI_API_KEY",
    ]
}

@MainActor
final class RunController: ObservableObject {
    struct Hooks {
        let appendLog: (String) -> Void
        let surfaceError: (String) -> Void
        let refresh: () -> Void
        let terminated: (String, Int32, Bool) -> Void
    }

    private var runningProcesses: [String: Process] = [:]
    private(set) var manualStops: [String: Date] = [:]
    @Published private(set) var stoppableProjects: Set<String> = []

    private let isPidAlive: (Int32) -> Bool
    private let signalPid: (Int32, Int32) -> Void
    private let graceNanoseconds: UInt64

    init(isPidAlive: @escaping (Int32) -> Bool = { kill($0, 0) == 0 },
         signalPid: @escaping (Int32, Int32) -> Void = { _ = kill($0, $1) },
         graceNanoseconds: UInt64 = 5_000_000_000) {
        self.isPidAlive = isPidAlive
        self.signalPid = signalPid
        self.graceNanoseconds = graceNanoseconds
    }

    var runningProcessNames: Set<String> {
        Set(runningProcesses.compactMap { $0.value.isRunning ? $0.key : nil })
    }

    func hasTrackedProcess(_ name: String) -> Bool {
        runningProcesses[name] != nil
    }

    func isRunning(_ name: String) -> Bool {
        runningProcesses[name]?.isRunning == true
    }

    func clearManualStop(_ name: String) {
        manualStops[name] = nil
    }

    func canStop(_ name: String) -> Bool {
        stoppableProjects.contains(name) && isRunning(name)
    }

    func launch(python: String, script: URL, arguments: [String],
                rootURL: URL, project: String?, hooks: Hooks) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.currentDirectoryURL = rootURL
        process.arguments = [script.path] + arguments
        var environment = ProcessInfo.processInfo.environment
        for key in APIKeyEnv.strippedAPIKeyVars {
            environment.removeValue(forKey: key)
        }
        process.environment = environment

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in
                hooks.appendLog(text)
                hooks.refresh()
            }
        }
        process.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            let uncaught = finished.terminationReason == .uncaughtSignal
            Task { @MainActor in
                pipe.fileHandleForReading.readabilityHandler = nil
                guard let self else { return }
                if let project {
                    self.runningProcesses[project] = nil
                    self.stoppableProjects.remove(project)
                }
                hooks.appendLog(uncaught
                    ? "\n[killed by signal \(status)]\n"
                    : "\n[exited with code \(status)]\n")
                if let project {
                    hooks.terminated(project, status, uncaught)
                }
                hooks.refresh()
            }
        }
        do {
            try process.run()
            if let project {
                runningProcesses[project] = process
                stoppableProjects.insert(project)
                manualStops[project] = nil
            }
        } catch {
            pipe.fileHandleForReading.readabilityHandler = nil
            hooks.appendLog("Failed to launch: \(error.localizedDescription)\n")
        }
    }

    func stopOwned(_ name: String, rootURL: URL, legacyLockURL: URL,
                   hooks: Hooks) {
        guard let process = runningProcesses[name] else {
            hooks.appendLog("\(name) wasn't launched from this window, so it can't be stopped here.\n")
            return
        }
        manualStops[name] = Date()
        hooks.appendLog("Stopping \(name)…\n")
        let pid = process.processIdentifier
        if process.isRunning { process.terminate() }
        Task { @MainActor [weak self] in
            guard let self else { return }
            try? await Task.sleep(nanoseconds: self.graceNanoseconds)
            if process.isRunning {
                self.signalPid(pid, SIGKILL)
                hooks.appendLog("\(name) didn't exit within 5s — killed.\n")
            }
            for lockURL in [SessionLayout.lockURL(rootURL: rootURL, id: name),
                            legacyLockURL] {
                guard let text = try? String(contentsOf: lockURL, encoding: .utf8) else { continue }
                let ownerPid = text.split(separator: " ")
                    .first { $0.hasPrefix("pid=") }
                    .flatMap { Int32($0.dropFirst(4)) }
                if ownerPid == pid || !(ownerPid.map(self.isPidAlive) ?? false) {
                    self.clearLock(lockURL, name: name, hooks: hooks)
                }
            }
            hooks.refresh()
        }
        hooks.refresh()
    }

    func stopExternal(_ name: String, pid: Int32, lockURL: URL,
                      hooks: Hooks) {
        manualStops[name] = Date()
        guard pid > 0 else {
            clearLock(lockURL, name: name, hooks: hooks)
            hooks.refresh()
            return
        }
        guard isPidAlive(pid) else {
            hooks.appendLog("\(name)'s lock names pid \(pid), which is no longer running — clearing the stale lock.\n")
            clearLock(lockURL, name: name, hooks: hooks)
            hooks.refresh()
            return
        }
        signalPid(pid, SIGTERM)
        hooks.appendLog("Stopping \(name) (pid \(pid))…\n")
        Task { @MainActor [weak self] in
            guard let self else { return }
            try? await Task.sleep(nanoseconds: self.graceNanoseconds)
            if self.isPidAlive(pid) {
                self.signalPid(pid, SIGKILL)
                hooks.appendLog("\(name) didn't exit within 5s — killed.\n")
            }
            self.clearLock(lockURL, name: name, hooks: hooks)
            hooks.refresh()
        }
        hooks.refresh()
    }

    private func clearLock(_ url: URL, name: String, hooks: Hooks) {
        do {
            try FileManager.default.removeItem(at: url)
        } catch {
            if FileManager.default.fileExists(atPath: url.path) {
                hooks.surfaceError("Could not clear the run lock for \(name) at \(url.path): "
                    + error.localizedDescription)
            }
        }
    }
}
