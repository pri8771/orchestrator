import Foundation
import Combine

// V3 8.1 ownership map. OrchestratorStore remains the fleet coordinator:
// workspace/config, project discovery order, queue/shepherd state, aggregate
// health/costs, search, routing, and the single refresh timer. SessionModel
// owns state whose identity is one session: scanned run status, chat lifecycle
// and concierge history/draft/thinking state, transcript parses, live stream
// tail position, and parallel-worker status. FleetScanner is the shared
// read-only scan service; RunController owns processes, stop timestamps, pid
// signaling, and lock cleanup. Store properties retained for views are
// forwarding shims, not a second source of per-session truth.
@MainActor
final class SessionModel: ObservableObject, Identifiable {
    let id: String

    @Published var project: Project?
    @Published var chatSession: ChatSession?
    @Published var chatMessages: [ConciergeMessage] = []
    @Published var chatInput = ""
    @Published var chatThinking = false
    @Published var buildWorkers: [BuildWorker]?

    var transcriptCache: [String: (fingerprint: FileFingerprint,
                                   value: PhaseTranscript)] = [:]
    var projectScanCache: ProjectScanCacheEntry?
    var streamTailCache: StreamTailCache?
    weak var runController: RunController?

    struct StreamTailCache: Sendable {
        var path: String
        var turnID: String
        var agent: String
        var offset: UInt64
        var remainder: Data
        var text: String
        var mtime: Date
        var lastSeq: Int
    }

    init(id: String) {
        self.id = id
    }
}
