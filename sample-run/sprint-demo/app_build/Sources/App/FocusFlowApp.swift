import SwiftData
import SwiftUI

@main
@MainActor
struct FocusFlowApp: App {
    private let modelContainer: ModelContainer
    @State private var engine: TimerEngine

    init() {
        let container: ModelContainer
        do {
            container = try ModelContainer(for: SessionEntry.self)
        } catch {
            fatalError("Failed to create ModelContainer for SessionEntry: \(error)")
        }
        modelContainer = container

        let recorder = SwiftDataSessionEntryRecorder(modelContext: container.mainContext)
        _engine = State(initialValue: TimerEngine(sessionRecorder: recorder))
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(engine)
        }
        .modelContainer(modelContainer)
    }
}
