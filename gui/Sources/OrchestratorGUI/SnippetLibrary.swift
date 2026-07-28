import Foundation

enum SnippetVariableKind: String, CaseIterable, Equatable {
    case string, number, boolean, choice
}

struct SnippetVariable: Identifiable, Equatable {
    var name: String
    var label: String
    var kind: SnippetVariableKind
    var options: [String]
    var required: Bool
    var defaultValue: String?
    var id: String { name }
}

enum SnippetSaveScope {
    case fleet
    case section(String)
    case project(URL)
}

struct SnippetLoadResult: Equatable {
    var snippets: [PromptSnippet]
    var warnings: [String]
}

enum SnippetLibrary {
    static let seeded: [PromptSnippet] = [
        PromptSnippet(name: "audit-this", phase: "",
                      text: "Audit this for correctness, risks, and missing evidence, focusing on {{focus}}.",
                      variables: [SnippetVariable(name: "focus", label: "Focus",
                                                  kind: .string, options: [],
                                                  required: false, defaultValue: nil)]),
        PromptSnippet(name: "poke-holes", phase: "",
                      text: "Poke holes in this proposal and name the strongest counterargument."),
        PromptSnippet(name: "simplify", phase: "",
                      text: "Simplify this without losing the important constraints or edge cases."),
        PromptSnippet(name: "devils-advocate", phase: "",
                      text: "Take the devil's-advocate position and make the best case against this."),
        PromptSnippet(name: "whats-missing", phase: "",
                      text: "What is missing, assumed, or insufficiently supported here?"),
        PromptSnippet(name: "summarize-decisions", phase: "",
                      text: "Summarize the decisions, rationale, owners, and unresolved questions."),
        PromptSnippet(name: "make-it-concrete", phase: "",
                      text: "Make this concrete with specific steps, examples, and acceptance criteria."),
        PromptSnippet(name: "ship-check", phase: "",
                      text: "Run a ship-readiness check focused on {{focus}} and list only actionable blockers.",
                      variables: [SnippetVariable(name: "focus", label: "Focus",
                                                  kind: .string, options: [],
                                                  required: false, defaultValue: nil)]),
    ]

    static func ensureSeeded(at url: URL, fileManager: FileManager = .default) {
        guard !fileManager.fileExists(atPath: url.path) else { return }
        try? fileManager.createDirectory(at: url.deletingLastPathComponent(),
                                         withIntermediateDirectories: true)
        save(seeded, to: url)
    }

    static func load(fleetURL: URL, sectionURL: URL? = nil,
                     projectURL: URL? = nil) -> SnippetLoadResult {
        ensureSeeded(at: fleetURL)
        var merged: [String: PromptSnippet] = [:]
        var warnings: [String] = []
        for (label, url) in [("fleet", Optional(fleetURL)),
                             ("section", sectionURL), ("project", projectURL)] {
            guard let url else { continue }
            let layer = loadLayer(at: url, label: label)
            warnings.append(contentsOf: layer.warnings)
            for snippet in layer.snippets { merged[snippet.name] = snippet }
        }
        return SnippetLoadResult(snippets: merged.values.sorted { $0.name < $1.name },
                                 warnings: warnings)
    }

    static func loadLayer(at url: URL, label: String) -> SnippetLoadResult {
        guard FileManager.default.fileExists(atPath: url.path) else {
            return SnippetLoadResult(snippets: [], warnings: [])
        }
        guard let data = try? Data(contentsOf: url),
              let array = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]] else {
            return SnippetLoadResult(snippets: [],
                                     warnings: ["Snippet \(label) layer is unreadable — skipped."])
        }
        var snippets: [PromptSnippet] = []
        var warnings: [String] = []
        for object in array {
            guard let name = object["name"] as? String, !name.isEmpty,
                  let text = object["text"] as? String, !text.isEmpty else {
                warnings.append("Snippet \(label) layer contains an invalid entry — skipped.")
                continue
            }
            let decoded = decodeVariables(object["variables"], snippet: name)
            warnings.append(contentsOf: decoded.warnings)
            snippets.append(PromptSnippet(name: name,
                                          phase: object["phase"] as? String ?? "",
                                          text: text, variables: decoded.variables,
                                          warning: decoded.warnings.first))
        }
        return SnippetLoadResult(snippets: snippets, warnings: warnings)
    }

    private static func decodeVariables(_ raw: Any?, snippet: String)
        -> (variables: [SnippetVariable], warnings: [String]) {
        guard let raw else { return ([], []) }
        guard let array = raw as? [[String: Any]] else {
            return ([], ["Snippet '\(snippet)' has malformed variables — plain-text insert only."])
        }
        var result: [SnippetVariable] = []
        for object in array {
            guard let name = object["name"] as? String, !name.isEmpty,
                  let rawKind = object["type"] as? String,
                  let kind = SnippetVariableKind(rawValue: rawKind) else {
                return ([], ["Snippet '\(snippet)' has malformed variables — plain-text insert only."])
            }
            let options = object["options"] as? [String] ?? []
            if kind == .choice && options.isEmpty {
                return ([], ["Snippet '\(snippet)' has a choice without options — plain-text insert only."])
            }
            let defaultValue: String?
            if let value = object["default"] as? String { defaultValue = value }
            else if let value = object["default"] as? NSNumber {
                defaultValue = value.stringValue
            } else { defaultValue = nil }
            result.append(SnippetVariable(
                name: name, label: object["label"] as? String ?? name,
                kind: kind, options: options,
                required: object["required"] as? Bool ?? false,
                defaultValue: defaultValue))
        }
        return (result, [])
    }

    @discardableResult
    static func save(_ snippets: [PromptSnippet], to url: URL) -> Bool {
        let objects = snippets.map { snippet -> [String: Any] in
            var object: [String: Any] = ["name": snippet.name,
                                         "phase": snippet.phase,
                                         "text": snippet.text]
            if !snippet.variables.isEmpty {
                object["variables"] = snippet.variables.map { variable -> [String: Any] in
                    var item: [String: Any] = ["name": variable.name,
                                               "label": variable.label,
                                               "type": variable.kind.rawValue,
                                               "required": variable.required]
                    if !variable.options.isEmpty { item["options"] = variable.options }
                    if let value = variable.defaultValue { item["default"] = value }
                    return item
                }
            }
            return object
        }
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                 withIntermediateDirectories: true)
        // Report whether the write landed — a swallowed failure left edits
        // alive only in memory, silently vanishing on relaunch. Callers that
        // claim "saved" (saveSnippets) must check this.
        do {
            let data = try JSONSerialization.data(withJSONObject: objects,
                                                  options: [.prettyPrinted, .sortedKeys])
            try data.write(to: url, options: .atomic)
            return true
        } catch { return false }
    }
}

struct SnippetFormDraft: Identifiable, Equatable {
    var snippet: PromptSnippet
    var values: [String: String]
    var id: String { snippet.name }

    init(snippet: PromptSnippet) {
        self.snippet = snippet
        values = Dictionary(uniqueKeysWithValues: snippet.variables.compactMap { variable in
            variable.defaultValue.map { (variable.name, $0) }
        })
    }

    var blockingVariables: [SnippetVariable] {
        snippet.variables.filter { variable in
            guard variable.required else { return false }
            let value = values[variable.name]?.trimmingCharacters(in: .whitespaces) ?? ""
            if value.isEmpty { return true }
            if variable.kind == .number { return Double(value) == nil }
            if variable.kind == .choice { return !variable.options.contains(value) }
            return false
        }
    }

    var canInsert: Bool { blockingVariables.isEmpty }

    var renderedText: String {
        var rendered = snippet.text
        for variable in snippet.variables {
            let token = "{{\(variable.name)}}"
            rendered = rendered.replacingOccurrences(
                of: token, with: values[variable.name] ?? "")
        }
        return rendered
    }

    var lintWarnings: [String] {
        let tokens = SnippetComposerLogic.tokens(in: snippet.text)
        let declared = Set(snippet.variables.map(\.name))
        var warnings = tokens.subtracting(declared).sorted().map {
            "Undeclared token {{\($0)}} remains literal."
        }
        warnings += declared.subtracting(tokens).sorted().map {
            "Declared variable '\($0)' is unused."
        }
        return warnings
    }
}

enum SnippetComposerLogic {
    enum CommandResolution: Equatable {
        case notCommand
        case refusal(String)
        case snippet(PromptSnippet)
    }

    static func tokens(in text: String) -> Set<String> {
        guard let regex = try? NSRegularExpression(
            pattern: #"\{\{([A-Za-z0-9_-]+)\}\}"#) else { return [] }
        let ns = text as NSString
        return Set(regex.matches(in: text, range: NSRange(location: 0, length: ns.length))
            .compactMap { match in
                guard match.numberOfRanges == 2 else { return nil }
                return ns.substring(with: match.range(at: 1))
            })
    }

    static func matches(draft: String, snippets: [PromptSnippet]) -> [PromptSnippet] {
        let query: String
        if draft.hasPrefix("/snippet ") {
            query = String(draft.dropFirst("/snippet ".count))
        } else if draft.hasPrefix("/"), !draft.dropFirst().contains(where: { $0.isWhitespace }) {
            query = String(draft.dropFirst())
        } else { return [] }
        return snippets.compactMap { snippet in
            CommandPaletteView.fuzzyScore(query, snippet.name).map { (snippet, $0) }
        }.sorted { $0.1 != $1.1 ? $0.1 > $1.1 : $0.0.name < $1.0.name }
            .map(\.0)
    }

    static func resolveCommand(_ draft: String,
                               snippets: [PromptSnippet]) -> CommandResolution {
        let trimmed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed == "/snippet" || trimmed.hasPrefix("/snippet ") else {
            return .notCommand
        }
        let name = trimmed == "/snippet" ? "" : String(trimmed.dropFirst(9))
            .trimmingCharacters(in: .whitespaces)
        guard !name.isEmpty else {
            return .refusal("Choose a snippet name; nothing was sent.")
        }
        guard let snippet = snippets.first(where: { $0.name == name }) else {
            return .refusal("Unknown snippet '\(name)' — nothing was sent.")
        }
        return .snippet(snippet)
    }
}
