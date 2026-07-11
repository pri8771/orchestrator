import Foundation
import SwiftData

@MainActor
protocol SessionEntryRecording {
    func recordSession(completedAt: Date, mode: Mode, durationSeconds: Int)
}

@MainActor
final class SwiftDataSessionEntryRecorder: SessionEntryRecording {
    private let modelContext: ModelContext

    init(modelContext: ModelContext) {
        self.modelContext = modelContext
    }

    func recordSession(completedAt: Date, mode: Mode, durationSeconds: Int) {
        modelContext.insert(
            SessionEntry(
                completedAt: completedAt,
                mode: mode,
                durationSeconds: durationSeconds
            )
        )
    }
}
