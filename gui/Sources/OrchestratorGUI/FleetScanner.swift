import Foundation

// A value-only boundary around the store's disk scan. It never touches
// ObservableObject or main-actor state; OrchestratorStore still owns request
// coalescing, watchdog recovery, generation rejection, and snapshot apply.
struct FleetScanInput: Sendable {
    let rootURL: URL
    let logsDirURL: URL
    let workflowsDirURL: URL
    let rolesURL: URL
    let modelPresetsURL: URL
    let configURL: URL
    let artifactRegistryURL: URL
    let manualStops: [String: Date]
    let runningProcessNames: Set<String>
    let commandProjectName: String?
    let delayForTests: TimeInterval
    let projectCache: [String: ProjectScanCacheEntry]
    let projectIntervals: [String: TimeInterval]
    let scanDate: Date
}

struct FleetScanSnapshot {
    let config: BackgroundConfigLoader.Snapshot
    let projects: [Project]
    let chat: ChatMetadataSnapshot
    let commandArtifact: ArtifactRouteRef?
    let artifacts: [String: [ArtifactSummary]]
    let workers: [String: [BuildWorker]]
    let costs: [String: ProjectCosts]
    let locks: [String: AppLockInfo]
    let staleLocks: Set<String>
    let autorunDisabled: Set<String>
    let queueFile: (order: [String], lanes: Int)?
    let events: [String: [EngineEvent]]
    let health: FleetHealthSummary
    let projectCache: [String: ProjectScanCacheEntry]
    let conductor: ConductorOversightSnapshot
    let missionControl: MissionControlSnapshot
}

enum FleetScanner {
    nonisolated static func scan(_ input: FleetScanInput) -> FleetScanSnapshot {
        if input.delayForTests > 0 {
            Thread.sleep(forTimeInterval: input.delayForTests)
        }
        let config = BackgroundConfigLoader.load(
            workflowsDirURL: input.workflowsDirURL,
            rolesURL: input.rolesURL,
            modelPresetsURL: input.modelPresetsURL,
            configURL: input.configURL)
        let workflowIndex = Dictionary(config.workflows.map { ($0.name, $0) },
                                       uniquingKeysWith: { current, _ in current })
        let names = BackgroundProjectLoader.discoverApps(rootURL: input.rootURL)
        let projectBatch = BackgroundProjectLoader.loadProjectsCached(
            names: names,
            rootURL: input.rootURL,
            workflowsByName: workflowIndex,
            defaultWorkflow: workflowIndex["app_build"],
            manualStops: input.manualStops,
            runningProcessNames: input.runningProcessNames,
            cache: input.projectCache,
            dueIntervals: input.projectIntervals,
            now: input.scanDate)
        let projects = projectBatch.projects
        let chat = ChatMetadataIndex.scan(rootURL: input.rootURL)
        let commandArtifact = input.commandProjectName.flatMap { name -> ArtifactRouteRef? in
            let projectID = name.components(separatedBy: "/").first ?? name
            return ArtifactRouteIndex.latestRoutable(
                projectDir: input.rootURL.appendingPathComponent(projectID))
        }
        let artifacts = ArtifactIndex.scan(
            rootURL: input.rootURL, projectNames: names,
            registryURL: input.artifactRegistryURL)
        var workers: [String: [BuildWorker]] = [:]
        for project in projects where project.running
            && (project.nextAgent?.contains("+") ?? false) {
            workers[project.name] = BackgroundProjectLoader.computeParallelBuildWorkers(
                for: project, logsDirURL: input.logsDirURL)
        }
        workers = workers.filter { !$0.value.isEmpty }
        let costs = CostsScanner.scan(rootURL: input.rootURL, names: names)
        let locks = FactoryScanner.scanLocks(rootURL: input.rootURL)
        let staleLocks = FactoryScanner.staleLockNames(in: locks)
        let autorunDisabled = FactoryScanner.scanAutorunDisabled(
            rootURL: input.rootURL, names: names)
        let queueFile = FactoryScanner.readQueueFile(rootURL: input.rootURL)
        let events = EventsScanner.scan(rootURL: input.rootURL, names: names)
        let running = Set(projects.filter(\.running).map(\.name))
            .union(locks.keys.filter { !staleLocks.contains($0) })
        let failed = Set(projects.filter {
            $0.status == .aborted
                || ($0.status == .done && $0.latestVerify?.ok == false)
        }.map(\.name))
        let health = EventsScanner.summarize(
            eventsByProject: events,
            runningProjects: running,
            failedProjects: failed)
        let missionControl = MissionControlDisk.scan(
            rootURL: input.rootURL, costs: costs, now: input.scanDate)
        let conductor = missionControl.oversight
        return FleetScanSnapshot(
            config: config, projects: projects, chat: chat,
            commandArtifact: commandArtifact, artifacts: artifacts,
            workers: workers, costs: costs, locks: locks,
            staleLocks: staleLocks, autorunDisabled: autorunDisabled,
            queueFile: queueFile, events: events, health: health,
            projectCache: projectBatch.cache, conductor: conductor,
            missionControl: missionControl)
    }
}
