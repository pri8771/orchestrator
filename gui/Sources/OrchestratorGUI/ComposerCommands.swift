import Foundation

enum ComposerCommandKind: String, CaseIterable, Equatable {
    case builtin, template, delegation, meta
}

struct ComposerCommand: Identifiable, Equatable {
    let name: String
    let kind: ComposerCommandKind
    let description: String
    var id: String { name }
}

enum CommandLibrary {
    struct LoadResult: Equatable {
        let commands: [ComposerCommand]
        let warnings: [String]
    }

    static func load(fleetURL: URL, sectionURL: URL?, projectURL: URL?) -> LoadResult {
        var merged: [String: ComposerCommand] = [:]
        var warnings: [String] = []
        let layers: [(String, URL?)] = [
            ("fleet", fleetURL), ("section", sectionURL), ("project", projectURL)]
        for (label, url) in layers {
            guard let url else { continue }
            let layer = loadLayer(at: url, label: label)
            warnings += layer.warnings
            for command in layer.commands { merged[command.name] = command }
        }
        return LoadResult(commands: merged.values.sorted { $0.name < $1.name },
                          warnings: warnings)
    }

    static func loadLayer(at url: URL, label: String) -> LoadResult {
        guard FileManager.default.fileExists(atPath: url.path) else {
            return LoadResult(commands: [], warnings: [])
        }
        guard let data = try? Data(contentsOf: url),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let rows = root["commands"] as? [Any] else {
            return LoadResult(commands: [],
                              warnings: ["Commands: (label) commands.json is unreadable; layer skipped."])
        }
        var commands: [ComposerCommand] = []
        var warnings: [String] = []
        for row in rows {
            guard let object = row as? [String: Any],
                  let name = object["name"] as? String, !name.isEmpty,
                  let rawKind = object["kind"] as? String,
                  let kind = ComposerCommandKind(rawValue: rawKind) else {
                warnings.append("Commands: (label) has an invalid entry; skipped.")
                continue
            }
            commands.append(ComposerCommand(
                name: name, kind: kind,
                description: (object["description"] as? String) ?? ""))
        }
        return LoadResult(commands: commands, warnings: warnings)
    }
}

enum ComposerSuggestion: Identifiable, Equatable {
    case command(ComposerCommand)
    case snippet(PromptSnippet)

    var id: String {
        switch self {
        case .command(let command): return "command:\(command.name)"
        case .snippet(let snippet): return "snippet:\(snippet.name)"
        }
    }

    var name: String {
        switch self {
        case .command(let command): return command.name
        case .snippet(let snippet): return snippet.name
        }
    }

    var kindLabel: String {
        switch self {
        case .command(let command): return command.kind.rawValue
        case .snippet: return "snippet"
        }
    }

    var detail: String {
        switch self {
        case .command(let command): return command.description
        case .snippet(let snippet): return snippet.text
        }
    }
}

enum ComposerAutocompleteLogic {
    static func matches(draft: String, commands: [ComposerCommand],
                        snippets: [PromptSnippet]) -> [ComposerSuggestion] {
        guard draft.hasPrefix("/"),
              !draft.dropFirst().contains(where: { $0.isWhitespace }) else { return [] }
        let query = String(draft.dropFirst())
        let candidates = commands.map(ComposerSuggestion.command)
            + snippets.map(ComposerSuggestion.snippet)
        return candidates.compactMap { suggestion in
            CommandPaletteView.fuzzyScore(query, suggestion.name)
                .map { (suggestion, $0) }
        }.sorted {
            if $0.1 != $1.1 { return $0.1 > $1.1 }
            if $0.0.name != $1.0.name { return $0.0.name < $1.0.name }
            return $0.0.kindLabel < $1.0.kindLabel
        }.map(\.0)
    }

    static func movedIndex(current: Int?, delta: Int, count: Int) -> Int? {
        guard count > 0 else { return nil }
        guard let current else { return delta > 0 ? 0 : count - 1 }
        return min(max(current + delta, 0), count - 1)
    }
}
