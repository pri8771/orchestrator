import Foundation
import SwiftData

enum Mode: String, Codable, CaseIterable, Sendable, Identifiable {
    case work
    case shortBreak
    case longBreak

    var id: String { rawValue }

    var displayName: String {
        switch self {
        case .work:
            return "Work"
        case .shortBreak:
            return "Short Break"
        case .longBreak:
            return "Long Break"
        }
    }
}

@Model
final class SessionEntry {
    var completedAt: Date
    var mode: Mode
    var durationSeconds: Int

    init(completedAt: Date, mode: Mode, durationSeconds: Int) {
        self.completedAt = completedAt
        self.mode = mode
        self.durationSeconds = durationSeconds
    }
}
