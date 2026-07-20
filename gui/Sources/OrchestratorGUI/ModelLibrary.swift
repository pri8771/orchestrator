import Foundation
import SwiftUI

// MARK: - Model Library: open-model search + per-phase routing (V2 spec §12)

// One hit from the engine's `--search-models --json` (curated registry merged
// with live Hugging Face GGUF repos — every id is directly `ollama pull`-able).
struct RemoteModelHit: Identifiable, Equatable {
    var id: String        // curated tag or "hf.co/<org>/<repo>"
    var label: String
    var source: String    // "curated" | "huggingface"
    var license: String
    var sizeGB: Double?
    var downloads: Int?
    var likes: Int?
    var installed: Bool

    static func parse(searchJSON data: Data) -> (hits: [RemoteModelHit], note: String) {
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return ([], "Search failed — the engine returned no JSON. Is python3 available?")
        }
        var hits: [RemoteModelHit] = []
        for raw in (root["results"] as? [Any]) ?? [] {
            guard let m = raw as? [String: Any],
                  let id = m["id"] as? String, !id.isEmpty else { continue }
            hits.append(RemoteModelHit(id: id,
                                       label: (m["label"] as? String) ?? "",
                                       source: (m["source"] as? String) ?? "",
                                       license: (m["license"] as? String) ?? "",
                                       sizeGB: m["size_gb"] as? Double,
                                       downloads: m["downloads"] as? Int,
                                       likes: m["likes"] as? Int,
                                       installed: (m["installed"] as? Bool) ?? false))
        }
        return (hits, (root["note"] as? String) ?? "")
    }
}

// Per-role model/effort overrides within one phase (model_routing.json
// phases.<key>.roles.<role>): "worker" patches every parallel build lane,
// "integrator" patches only the integrator's turn — the cheap-workers/
// expensive-integrator split (modelrouting.py's ROLE_NAMES, applied by
// orchestrator.py's _apply_role_routing). That function only ever reads
// these six fields — "agents", "gemini_reasoning", and "ollama_reasoning"
// are accepted at the JSON-schema level (PHASE_FIELDS) but never consulted
// per-role, so they're intentionally not modeled here.
struct RoleFields: Equatable {
    var claude = ""
    var codex = ""
    var codexReasoning = ""
    var claudeReasoning = ""
    var gemini = ""
    var ollama = ""
    var isEmpty: Bool {
        claude.isEmpty && codex.isEmpty && codexReasoning.isEmpty
            && claudeReasoning.isEmpty && gemini.isEmpty && ollama.isEmpty
    }

    func model(for agent: String) -> String {
        switch agent {
        case "claude": return claude
        case "codex": return codex
        case "gemini": return gemini
        case "ollama": return ollama
        default: return ""
        }
    }
    mutating func setModel(_ value: String, for agent: String) {
        switch agent {
        case "claude": claude = value
        case "codex": codex = value
        case "gemini": gemini = value
        case "ollama": ollama = value
        default: break
        }
    }
    func effort(for agent: String) -> String {
        switch agent {
        case "claude": return claudeReasoning
        case "codex": return codexReasoning
        default: return ""
        }
    }
    mutating func setEffort(_ value: String, for agent: String) {
        switch agent {
        case "claude": claudeReasoning = value
        case "codex": codexReasoning = value
        default: break
        }
    }

    // Parses one role's sub-dict ("worker" or "integrator") from JSON.
    static func parse(_ raw: [String: Any]) -> RoleFields {
        var f = RoleFields()
        f.claude = (raw["claude"] as? String) ?? ""
        f.codex = (raw["codex"] as? String) ?? ""
        f.codexReasoning = (raw["codex_reasoning"] as? String) ?? ""
        f.claudeReasoning = (raw["claude_reasoning"] as? String) ?? ""
        f.gemini = (raw["gemini"] as? String) ?? ""
        f.ollama = (raw["ollama"] as? String) ?? ""
        return f
    }

    // Serializes back to JSON, dropping empty fields (never emits "").
    var jsonObject: [String: Any] {
        var out: [String: Any] = [:]
        if !claude.isEmpty { out["claude"] = claude }
        if !codex.isEmpty { out["codex"] = codex }
        if !codexReasoning.isEmpty { out["codex_reasoning"] = codexReasoning }
        if !claudeReasoning.isEmpty { out["claude_reasoning"] = claudeReasoning }
        if !gemini.isEmpty { out["gemini"] = gemini }
        if !ollama.isEmpty { out["ollama"] = ollama }
        return out
    }
}

// Both roles a phase can carry per-role overrides for. Equatable/isEmpty so
// PhaseRoute's own isEmpty and dirty-tracking keep working unmodified.
struct RoleOverrides: Equatable {
    var worker = RoleFields()
    var integrator = RoleFields()
    var isEmpty: Bool { worker.isEmpty && integrator.isEmpty }
}

// GUI-side mirror of model_routing.json (per-phase model overrides + the
// cloud->local fallback switch). Tolerant load, atomic save — same file the
// engine's modelrouting.py reads.
struct PhaseRoute: Equatable {
    var claude = ""
    var codex = ""
    var codexReasoning = ""
    // Claude's --effort per phase. Written to "claude_reasoning" — the field
    // _apply_phase_routing reads (engine loader support tracked separately).
    var claudeReasoning = ""
    var gemini = ""
    var ollama = ""
    var agents = ""
    // V3 9.4: roster intent. composition uses the engine's existing agent
    // filter vocabulary; castSize deterministically truncates/repeats it.
    var composition = ""
    var castSize: Int? = nil
    var timeout = 0            // seconds per turn; 0 = run default
    // Per-role (worker/integrator) overrides within this phase; nil when the
    // phase carries none. Kept separate from isEmpty's field list below so a
    // phase with ONLY role overrides still round-trips (isEmpty covers it).
    var roles: RoleOverrides?
    // Per-phase debate budget: nil = workflow default, 0 = unlimited (until
    // natural consensus — the engine skips the forced vote), 1-99 = a cap.
    var rounds: Int? = nil
    // Operator instructions for THIS project's version of the phase — the
    // engine splices them into every turn's context (binding guidance).
    var instructions = ""
    var isEmpty: Bool {
        claude.isEmpty && codex.isEmpty && codexReasoning.isEmpty
            && claudeReasoning.isEmpty && gemini.isEmpty && ollama.isEmpty
            && agents.isEmpty && composition.isEmpty && castSize == nil
            && timeout == 0 && (roles?.isEmpty ?? true)
            && rounds == nil && instructions.isEmpty
    }

    // Grid accessors: the per-phase model / effort override for one agent.
    func model(for agent: String) -> String {
        switch agent {
        case "claude": return claude
        case "codex": return codex
        case "gemini": return gemini
        case "ollama": return ollama
        default: return ""
        }
    }
    mutating func setModel(_ value: String, for agent: String) {
        switch agent {
        case "claude": claude = value
        case "codex": codex = value
        case "gemini": gemini = value
        case "ollama": ollama = value
        default: break
        }
    }
    func effort(for agent: String) -> String {
        switch agent {
        case "claude": return claudeReasoning
        case "codex": return codexReasoning
        default: return ""
        }
    }
    mutating func setEffort(_ value: String, for agent: String) {
        switch agent {
        case "claude": claudeReasoning = value
        case "codex": codexReasoning = value
        default: break
        }
    }
}

struct ModelRouting: Equatable {
    var enabled = true
    var cloudToLocal = true
    var fallbackModel = ""
    // Ordered rescue steps per agent: sibling provider models ("gpt-5.3-codex-
    // spark") and/or local tags ("local:qwen3-coder:30b"), tried top to bottom.
    var chains: [String: [String]] = [:]
    var phases: [String: PhaseRoute] = [:]

    static func load(from url: URL) -> ModelRouting {
        var r = ModelRouting()
        guard let data = try? Data(contentsOf: url),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return r }
        r.enabled = (root["enabled"] as? Bool) ?? true
        if let fb = root["fallback"] as? [String: Any] {
            r.cloudToLocal = (fb["cloud_to_local"] as? Bool) ?? true
            r.fallbackModel = (fb["local_model"] as? String) ?? ""
            for (agent, raw) in (fb["chains"] as? [String: Any]) ?? [:] {
                if let steps = raw as? [String], !steps.isEmpty {
                    r.chains[agent] = steps
                }
            }
        }
        for (key, raw) in (root["phases"] as? [String: Any]) ?? [:] {
            guard let ov = raw as? [String: Any] else { continue }
            var p = PhaseRoute()
            p.claude = (ov["claude"] as? String) ?? ""
            p.codex = (ov["codex"] as? String) ?? ""
            p.codexReasoning = (ov["codex_reasoning"] as? String) ?? ""
            p.claudeReasoning = (ov["claude_reasoning"] as? String) ?? ""
            p.gemini = (ov["gemini"] as? String) ?? ""
            p.ollama = (ov["ollama"] as? String) ?? ""
            p.agents = (ov["agents"] as? String) ?? ""
            p.composition = (ov["composition"] as? String) ?? ""
            p.castSize = ov["cast_size"] as? Int
            p.timeout = (ov["timeout"] as? Int) ?? 0
            if let rolesRaw = ov["roles"] as? [String: Any] {
                var ro = RoleOverrides()
                if let w = rolesRaw["worker"] as? [String: Any] { ro.worker = RoleFields.parse(w) }
                if let i = rolesRaw["integrator"] as? [String: Any] { ro.integrator = RoleFields.parse(i) }
                if !ro.isEmpty { p.roles = ro }
            }
            p.rounds = ov["rounds"] as? Int
            p.instructions = (ov["instructions"] as? String) ?? ""
            if !p.isEmpty { r.phases[key] = p }
        }
        return r
    }

    func save(to url: URL) {
        var phasesObj: [String: Any] = [:]
        for (key, p) in phases where !p.isEmpty {
            var ov: [String: Any] = [:]
            if !p.claude.isEmpty { ov["claude"] = p.claude }
            if !p.codex.isEmpty { ov["codex"] = p.codex }
            if !p.codexReasoning.isEmpty { ov["codex_reasoning"] = p.codexReasoning }
            if !p.claudeReasoning.isEmpty { ov["claude_reasoning"] = p.claudeReasoning }
            if !p.gemini.isEmpty { ov["gemini"] = p.gemini }
            if !p.ollama.isEmpty { ov["ollama"] = p.ollama }
            if !p.agents.isEmpty { ov["agents"] = p.agents }
            if !p.composition.isEmpty { ov["composition"] = p.composition }
            if let size = p.castSize { ov["cast_size"] = size }
            if p.timeout > 0 { ov["timeout"] = p.timeout }
            if let ro = p.roles, !ro.isEmpty {
                var rolesObj: [String: Any] = [:]
                if !ro.worker.isEmpty { rolesObj["worker"] = ro.worker.jsonObject }
                if !ro.integrator.isEmpty { rolesObj["integrator"] = ro.integrator.jsonObject }
                if !rolesObj.isEmpty { ov["roles"] = rolesObj }
            }
            if let r = p.rounds { ov["rounds"] = r }
            if !p.instructions.isEmpty { ov["instructions"] = p.instructions }
            phasesObj[key] = ov
        }
        var fb: [String: Any] = ["cloud_to_local": cloudToLocal,
                                 "local_model": fallbackModel]
        let cleanChains = chains.filter { !$0.value.isEmpty }
        if !cleanChains.isEmpty { fb["chains"] = cleanChains }
        let obj: [String: Any] = [
            "schema_version": 1,
            "_docs": "Per-phase model routing + fallback ladder. Edited by the GUI (Settings -> Routing); field reference in modelrouting.py.",
            "enabled": enabled,
            "fallback": fb,
            "phases": phasesObj,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: obj,
                                                  options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: url, options: .atomic)
        }
    }
}

// MARK: - Library types: reusable phase-prompt snippets + saved run profiles

struct PromptSnippet: Identifiable, Equatable {
    var name: String
    var phase: String      // "" = usable in any phase
    var text: String
    var variables: [SnippetVariable] = []
    var warning: String? = nil
    var id: String { name }
}

struct RunProfile: Identifiable, Equatable {
    var name: String
    var workflow: String
    var url: URL
    var id: String { url.path }
}

// Compact count formatting for download numbers ("2.4M", "51k").
func compactCount(_ n: Int) -> String {
    if n >= 1_000_000 { return String(format: "%.1fM", Double(n) / 1_000_000) }
    if n >= 1_000 { return String(format: "%.0fk", Double(n) / 1_000) }
    return "\(n)"
}

// MARK: - Search UI (embedded in Settings -> Local Models)

struct OpenModelSearchSection: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var query = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                Text("Find open-source models").fontWeight(.medium)
                Spacer()
                if store.modelSearchInFlight { ProgressView().controlSize(.small) }
            }
            HStack(spacing: 6) {
                TextField("search the curated registry + Hugging Face (e.g. qwen coder, llama, deepseek)",
                          text: $query)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { store.searchOpenModels(query) }
                Button("Search") { store.searchOpenModels(query) }
                    .disabled(query.trimmingCharacters(in: .whitespaces).isEmpty
                              || store.modelSearchInFlight)
            }
            .font(.callout)
            ForEach(store.modelSearchResults) { hit in
                SearchHitRow(hit: hit)
            }
            if !store.modelSearchNote.isEmpty {
                Text(store.modelSearchNote).font(.caption2)
                    .foregroundStyle(DS.status.warning.color)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text("Every result runs fully on this Mac via Ollama. Hugging Face results are GGUF repos pulled as hf.co/<org>/<repo>; confirm each model card's license before shipping it commercially.")
                .font(DS.font.caption).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct SearchHitRow: View {
    @EnvironmentObject var store: OrchestratorStore
    let hit: RemoteModelHit

    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(hit.id).font(DS.font.monoInline)
                    .lineLimit(1).truncationMode(.middle)
                HStack(spacing: 8) {
                    // Source badges are identity, not state: accent for the
                    // curated registry, neutral gray for Hugging Face.
                    Text(hit.source == "curated" ? "curated" : "Hugging Face")
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(hit.source == "curated" ? DS.accent.fill
                                                            : DS.status.idlePill.fill,
                                    in: Capsule())
                    if !hit.license.isEmpty { Text(hit.license) }
                    if let d = hit.downloads { Text("\(compactCount(d)) pulls") }
                    if let s = hit.sizeGB { Text(String(format: "%.1fGB", s)) }
                }
                .font(DS.font.caption2).foregroundStyle(.secondary)
            }
            Spacer()
            if let p = store.pullProgress[hit.id] {
                if p < 0 {
                    ProgressView().controlSize(.small)
                } else {
                    ProgressView(value: p).frame(width: 90)
                    Text("\(Int(p * 100))%").font(DS.font.caption2).monospacedDigit()
                        .foregroundStyle(.secondary)
                }
            } else if hit.installed {
                Label("installed", systemImage: "checkmark.seal.fill")
                    .font(DS.font.caption2)
                    .foregroundStyle(DS.status.success.color)
            } else {
                Button {
                    if hit.source == "curated" {
                        store.pullModelInApp(hit.id)
                    } else {
                        store.addOpenModel(hit)
                    }
                } label: {
                    Label("Download", systemImage: "arrow.down.circle")
                }
                .font(.caption)
                .accessibilityLabel("Download \(hit.id)")
            }
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Settings -> Defaults routing sections (per-phase models, fallback,
// presets). Standalone wrapper kept for reuse; the sections embed directly in
// Settings › Defaults (Native Pro M2).

struct ModelRoutingSettings: View {
    var body: some View {
        Form { ModelRoutingSections() }
            .formStyle(.grouped)
            .padding()
    }
}

struct ModelRoutingSections: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var routing = ModelRouting()
    @State private var loaded = false

    private var installedLocal: [String] {
        (store.localModels?.registry ?? []).filter(\.installed).map(\.id)
    }

    var body: some View {
        Group {
            Section("Model routing") {
                Toggle("Route models by phase", isOn: $routing.enabled)
                Text("Give each phase the model it deserves: strong, slow models where quality is decided; fast, cheap, or local models everywhere else. Changes take effect on the next run. Edit the per-phase plan in the routing grid below; ladders live in Library › Models & Agents.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Local safety net") {
                Toggle("Local safety net — never lose a turn", isOn: $routing.cloudToLocal)
                Picker("Safety-net model", selection: $routing.fallbackModel) {
                    Text("Automatic (your default, then first installed)").tag("")
                    ForEach(installedLocal, id: \.self) { Text($0).tag($0) }
                }
                Text("The last rung of every ladder: an installed model that runs entirely on this Mac. A phase concludes as long as one model is still working — stand-ins take over coordination automatically.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .onAppear {
            if !loaded {
                routing = store.readModelRouting()
                loaded = true
            }
        }
        .onChange(of: routing) { _, newValue in
            if loaded {
                // Preserve fields the grid owns: re-read, merge only the
                // sections these controls edit, then write.
                var current = store.readModelRouting()
                current.enabled = newValue.enabled
                current.cloudToLocal = newValue.cloudToLocal
                current.fallbackModel = newValue.fallbackModel
                store.writeModelRouting(current)
            }
        }
    }
}

// One agent's rescue ladder: ordered steps tried when it fails mid-run.
struct FallbackChainEditor: View {
    @EnvironmentObject var store: OrchestratorStore
    let agent: String
    @Binding var chain: [String]

    private var displayName: String { (Speaker(rawValue: agent) ?? .system).display }
    private var installedLocal: [String] {
        (store.localModels?.registry ?? []).filter(\.installed).map(\.id)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text("If \(displayName) hits a limit, try in order:")
                    .font(.caption).fontWeight(.medium)
                Spacer()
                Menu {
                    Section("Same provider, different model") {
                        ForEach(store.modelPresets(agent).filter { !chain.contains($0) },
                                id: \.self) { m in
                            Button(m) { chain.append(m) }
                        }
                    }
                    Section("Local, on this Mac") {
                        ForEach(installedLocal.filter { !chain.contains("local:\($0)") },
                                id: \.self) { m in
                            Button(m) { chain.append("local:\(m)") }
                        }
                    }
                } label: {
                    Label("Add step", systemImage: "plus.circle")
                }
                .menuStyle(.borderlessButton)
                .font(.caption)
                .fixedSize()
                .accessibilityLabel("Add fallback step for \(displayName)")
            }
            if chain.isEmpty {
                Text("No custom steps — the local safety net below still catches failures.")
                    .font(.caption2).foregroundStyle(.tertiary)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(Array(chain.enumerated()), id: \.offset) { i, step in
                            HStack(spacing: 4) {
                                Text("\(i + 1)").font(DS.font.caption2)
                                    .foregroundStyle(.secondary)
                                Text(step).font(DS.font.monoCaption)
                                    .lineLimit(1)
                                Button {
                                    chain.remove(at: i)
                                } label: {
                                    Image(systemName: "xmark.circle.fill")
                                        .font(DS.font.caption2)
                                }
                                .buttonStyle(.plain).foregroundStyle(.secondary)
                                .accessibilityLabel("Remove \(step)")
                            }
                            .padding(.horizontal, 7).padding(.vertical, 3)
                            .background(Color.secondary.opacity(0.12), in: Capsule())
                        }
                    }
                }
            }
        }
        .padding(.vertical, 2)
    }
}

struct PhaseRouteRow: View {
    @EnvironmentObject var store: OrchestratorStore
    let phase: PhaseDef
    let isHeavy: Bool
    @Binding var route: PhaseRoute

    private static let participantOptions: [(String, String)] = [
        ("", "Everyone"), ("cloud", "Cloud only"), ("local", "Local only"),
        ("claude,codex", "Claude + Codex"),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(phase.title).font(.callout).fontWeight(.medium)
                if phase.writes || isHeavy {
                    // Accent, not purple — purple means fallback, exclusively.
                    Text(phase.writes ? "BUILD" : "CRITICAL")
                        .font(DS.font.caption2)
                        .padding(.horizontal, 4).padding(.vertical, 1)
                        .background(DS.accent.fill, in: Capsule())
                        .foregroundStyle(DS.accent.color)
                }
                Spacer()
                if !route.isEmpty {
                    Button("Reset") { route = PhaseRoute() }
                        .font(.caption2).buttonStyle(.plain).foregroundStyle(.secondary)
                }
            }
            HStack(spacing: 10) {
                labeledPicker("Claude", selection: $route.claude,
                              options: [""] + store.modelPresets("claude"),
                              display: { $0.isEmpty ? "default" : $0 })
                labeledPicker("Codex", selection: $route.codex,
                              options: [""] + store.modelPresets("codex"),
                              display: { $0.isEmpty ? "default" : $0 })
            }
            HStack(spacing: 10) {
                labeledPicker("Effort", selection: $route.codexReasoning,
                              options: ["", "low", "medium", "high"],
                              display: { $0.isEmpty ? "default" : $0 })
                labeledPicker("Who", selection: $route.agents,
                              options: Self.participantOptions.map(\.0),
                              display: { key in
                                  Self.participantOptions.first { $0.0 == key }?.1 ?? key
                              })
                HStack(spacing: 4) {
                    Text("Time").font(.caption2).foregroundStyle(.secondary)
                        .frame(width: 40, alignment: .leading)
                    Picker("", selection: $route.timeout) {
                        Text("default").tag(0)
                        Text("5 min").tag(300)
                        Text("15 min").tag(900)
                        Text("30 min").tag(1800)
                        Text("60 min").tag(3600)
                    }
                    .labelsHidden().pickerStyle(.menu).font(.caption)
                    .accessibilityLabel("Thinking time for \(phase.title)")
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.vertical, 3)
    }

    @ViewBuilder
    private func labeledPicker(_ label: String, selection: Binding<String>,
                               options: [String],
                               display: @escaping (String) -> String) -> some View {
        HStack(spacing: 4) {
            Text(label).font(.caption2).foregroundStyle(.secondary)
                .frame(width: 40, alignment: .leading)
            Picker("", selection: selection) {
                ForEach(options, id: \.self) { Text(display($0)).tag($0) }
            }
            .labelsHidden().pickerStyle(.menu).font(.caption)
            .accessibilityLabel("\(label) for \(phase.title)")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
