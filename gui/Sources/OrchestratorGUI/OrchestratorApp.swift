import SwiftUI
import AppKit

// Lets AppKit (the AppDelegate) re-open the single `Window` scene. A singleton
// `Window` — unlike a `WindowGroup` — is not recreated by AppKit's default
// reopen, so the Dock-icon path must call SwiftUI's openWindow explicitly. We
// capture that action from a scene context at launch and stash it here.
final class SceneBridge {
    static let shared = SceneBridge()
    var openMainWindow: (() -> Void)?
}

// Bringing a SwiftPM executable to the foreground: without a bundle, the
// process launches as an accessory, so force a regular activation policy and
// pull the window to the front.
final class AppDelegate: NSObject, NSApplicationDelegate {
    // Updated by OrchestratorStore on every refresh tick: true while any project
    // is actively running, so closing the window doesn't kill an in-flight run.
    static var runsActive = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        // A binary launched from a terminal starts as a background app, and modern
        // macOS blocks a background app from activating itself — so the window opens
        // *behind* Terminal (or seemingly "never appears"). orderFrontRegardless()
        // forces the window visible without needing app activation, and we retry a
        // few times because the scene's NSWindow may not exist on the first tick.
        bringWindowToFront()
        for delay in [0.15, 0.4, 1.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
                self?.bringWindowToFront()
            }
        }
    }

    private func bringWindowToFront() {
        NSApp.activate(ignoringOtherApps: true)
        if let w = NSApp.windows.first(where: { $0.canBecomeMain }) {
            w.makeKeyAndOrderFront(nil)
            w.orderFrontRegardless()   // shows even while the app is in the background
        }
    }

    // §10.1: stay resident (Dock-icon, window closed) while work is in flight so a
    // background run survives closing the window; quit normally when idle. The Dock
    // icon remains, so the user can reopen the window or quit at any time.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return !AppDelegate.runsActive
    }

    // Reopen the main window when the Dock icon is clicked after the window closed
    // (e.g. it was closed while a run kept the app resident). A singleton Window
    // isn't recreated by activate alone, so explicitly re-open it via SwiftUI.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            SceneBridge.shared.openMainWindow?()
            NSApp.activate(ignoringOtherApps: true)
        }
        return true
    }
}

@main
struct OrchestratorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var store = OrchestratorStore()

    init() {
        // Defeat the intermittent "relaunch headless with zero windows" state.
        // With a MenuBarExtra present, macOS window-state restoration can bring
        // the app back with no window (when the last session's window was closed
        // while a run kept the process alive). Forcing this false makes the app
        // always open its window fresh on launch rather than restoring "no windows".
        UserDefaults.standard.set(false, forKey: "NSQuitAlwaysKeepsWindows")
    }

    var body: some Scene {
        // A single-instance Window (not WindowGroup): this app is single-window,
        // and an explicit id lets the menu-bar item and the Dock icon reliably
        // re-open it when it's been closed — the recovery path if it ever comes
        // up headless.
        Window("Orchestrator", id: "main") {
            RootView().environmentObject(store)
        }
        .windowStyle(.titleBar)
        .windowToolbarStyle(.unified)
        .defaultSize(width: 1100, height: 700)
        // Menu-bar commands + keyboard shortcuts for the primary actions.
        // The Native Pro shell owns selection/sheet state, so the actions
        // relay through store.uiCommand (observed in AppShellView).
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New App") { store.uiCommand = .newChat }
                    .keyboardShortcut("n", modifiers: .command)
                Button("Command Palette") { store.showCommandPalette = true }
                    .keyboardShortcut("k", modifiers: .command)
            }
            // Native Pro shell: inspector toggle + project search focus.
            CommandGroup(after: .sidebar) {
                Button("Toggle Inspector") { store.uiCommand = .toggleInspector }
                    .keyboardShortcut("i", modifiers: [.option, .command])
                Button("Find Project") { store.uiCommand = .focusSearch }
                    .keyboardShortcut("f", modifiers: .command)
            }
            CommandMenu("Run") {
                Button("Run Selected Project") { store.uiCommand = .runSelected }
                    .keyboardShortcut("r", modifiers: .command)
                Button("Refresh Workspace") { store.refresh() }
                    .keyboardShortcut("r", modifiers: [.command, .shift])
                Divider()
                Button("Toggle Run Log") { store.uiCommand = .toggleLog }
                    .keyboardShortcut("l", modifiers: .command)
            }
        }

        // Cmd+, opens Settings (General / Engine / Defaults / Advanced).
        Settings {
            SettingsView().environmentObject(store)
        }

        // Menu-bar convenience: a glance at run status across all projects (§9.7).
        MenuBarExtra("Orchestrator", systemImage: store.orchestratorRunning ? "cpu.fill" : "cpu") {
            MenuBarContent().environmentObject(store)
        }
    }
}

// Scene root. Exists to read @Environment(\.openWindow) — only available inside a
// scene's view tree — and hand it to SceneBridge so AppKit's Dock-reopen path can
// re-materialize the window. Also owns the store.start() launch hook.
private struct RootView: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        ContentView()
            .onAppear {
                SceneBridge.shared.openMainWindow = { openWindow(id: "main") }
                store.start()
            }
    }
}

// Menu-bar dropdown: per-project run state + quick actions.
struct MenuBarContent: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.openWindow) private var openWindow

    private var running: Int { store.projects.filter { $0.running }.count }

    var body: some View {
        if store.projects.isEmpty {
            Text("No projects yet")
        } else {
            Text(running > 0 ? "\(running) run\(running == 1 ? "" : "s") in progress" : "Idle")
            if !store.runQueue.isEmpty { Text("\(store.runQueue.count) queued") }
            Divider()
            ForEach(store.projects) { p in
                let mark = p.running ? "▶︎" : (p.status == .done ? "✓" : (p.status == .aborted ? "✗" : "•"))
                // Status word for VoiceOver — the glyph alone conveys state by
                // shape/color, which a screen reader can't announce.
                let word = p.running ? "running" : (p.status == .done ? "done"
                    : (p.status == .aborted ? "failed" : "idle"))
                Text("\(mark)  \(p.name)")
                    .accessibilityLabel("\(p.name), \(word)")
            }
        }
        Divider()
        Button("Open Orchestrator") {
            openWindow(id: "main")            // re-materializes the window if closed
            NSApp.activate(ignoringOtherApps: true)
        }
        Button("Quit") { NSApp.terminate(nil) }
    }
}
