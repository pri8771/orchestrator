import Foundation

// Parses a single phase Markdown file into a structured transcript.
//
// The orchestrator writes a strict layout (see orchestrator.py):
//   # <app> — <Title>
//   ## Original Prompt   ```<prompt>```
//   ## Phase Purpose     <purpose>
//   ## Rubric            <blurb>
//   ## Transcript
//     ### Round 1 / ### Iteration 1 / ### Forced Vote (...)
//     **Codex — Round 1**            <body...>   From Codex
//     **Claude — Round 1**           ...
//     **Coordinator (Gemini) — decision after round 1**  ...
//   ## Coordinator Decision          (footer — only when phase finished)
//   ## Final Output                  <final>
//   ---  CONSENSUS: YES / VOTE_DECISION: YES|NO
enum TranscriptParser {

    // A whole-line header like **Codex — Round 1** (bold spans the entire line).
    // The pattern is a compile-time constant, so this never actually fails in
    // practice — but a crash-on-failure here is inconsistent with the rest of
    // this codebase's fail-open philosophy (every reader degrades instead of
    // taking the app down). `try?` + a nil-safe isHeader(): if compilation
    // somehow failed, no line is ever recognized as a speaker header, so
    // parsing degrades to "no messages extracted" rather than crashing at
    // launch (transcript files never bring the app down, per the module's
    // own header comment for the rest of this parser).
    private static let headerRegex = try? NSRegularExpression(
        pattern: #"^\*\*\s*(Codex|Claude|Gemini|Local|Ollama|Coordinator|Integrator|Repair|You|Human)\b.*\*\*$"#)

    static func parse(_ text: String) -> PhaseTranscript {
        var t = PhaseTranscript()
        t.exists = true
        let lines = text.components(separatedBy: "\n")

        t.originalPrompt = extractFenced(lines, after: "## Original Prompt")
        t.purpose = extractSection(lines, after: "## Phase Purpose")
        t.finalOutput = extractFinalOutput(lines)
        t.marker = extractMarker(lines)

        guard let tStart = indexOfHeading(lines, "## Transcript") else { return t }
        // Transcript ends at the footer "## Coordinator Decision" if present.
        let tEnd = indexOfHeading(lines, "## Coordinator Decision", from: tStart + 1)
            ?? lines.count

        var messages: [ChatMessage] = []
        var section = ""
        var curHeader: String? = nil
        var curSpeaker: Speaker = .system
        var bodyLines: [String] = []
        var msgId = 0

        func flush() {
            guard let h = curHeader else { return }
            let body = cleanBody(bodyLines)
            messages.append(ChatMessage(id: msgId, speaker: curSpeaker,
                                        header: h, section: section, body: body,
                                        persona: extractPersona(h)))
            msgId += 1
            curHeader = nil
            bodyLines = []
        }

        var i = tStart + 1
        while i < tEnd {
            let raw = lines[i]
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("### ") {
                flush()
                section = String(line.dropFirst(4)).trimmingCharacters(in: .whitespaces)
            } else if isHeader(line) {
                flush()
                let inner = stripBold(line)
                curHeader = inner
                curSpeaker = Speaker.classify(inner)
            } else if curHeader != nil {
                bodyLines.append(raw)
            }
            i += 1
        }
        flush()
        t.messages = messages
        return t
    }

    // MARK: helpers

    private static func isHeader(_ line: String) -> Bool {
        guard let headerRegex else { return false }
        let range = NSRange(line.startIndex..<line.endIndex, in: line)
        return headerRegex.firstMatch(in: line, range: range) != nil
    }

    // The parenthesized tag right after the speaker name in a header, e.g.
    // "Codex (Product Strategist · Skeptic) — Round 1" -> "Product Strategist · Skeptic".
    // For a coordinator ("Coordinator (Gemini) — …") it yields the model name.
    private static func extractPersona(_ header: String) -> String {
        // Persona parens sit BEFORE the em-dash separator ("Codex (Skeptic) —
        // Round 1"); anything after it is an annotation, not a persona — the
        // skipped-turn header "Codex — Round 1 (skipped: CLI unavailable)"
        // must never grow a "skipped: CLI unavailable" persona chip.
        let scope = header.range(of: " — ").map { header[..<$0.lowerBound] }
            ?? header[...]
        guard let open = scope.firstIndex(of: "("),
              let close = scope[open...].firstIndex(of: ")") else { return "" }
        let inner = scope[scope.index(after: open)..<close]
            .trimmingCharacters(in: .whitespaces)
        return inner
    }

    private static func stripBold(_ line: String) -> String {
        var s = line.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("**") { s.removeFirst(2) }
        if s.hasSuffix("**") { s.removeLast(2) }
        return s.trimmingCharacters(in: .whitespaces)
    }

    private static func cleanBody(_ lines: [String]) -> String {
        var out = lines
        // Drop leading/trailing blank lines.
        while let f = out.first, f.trimmingCharacters(in: .whitespaces).isEmpty { out.removeFirst() }
        while let l = out.last, l.trimmingCharacters(in: .whitespaces).isEmpty { out.removeLast() }
        // Drop the mandatory trailing signature line ("From Codex" etc).
        if let l = out.last {
            let t = l.trimmingCharacters(in: .whitespaces)
            if t == "From Codex" || t == "From Claude" || t == "From Gemini" {
                out.removeLast()
                while let l2 = out.last, l2.trimmingCharacters(in: .whitespaces).isEmpty { out.removeLast() }
            }
        }
        return out.joined(separator: "\n")
    }

    private static func indexOfHeading(_ lines: [String], _ heading: String,
                                       from: Int = 0) -> Int? {
        var i = from
        while i < lines.count {
            if lines[i].trimmingCharacters(in: .whitespaces) == heading { return i }
            i += 1
        }
        return nil
    }

    // Content between the first ``` fence after `marker`.
    private static func extractFenced(_ lines: [String], after marker: String) -> String {
        guard let start = indexOfHeading(lines, marker) else { return "" }
        var i = start + 1
        while i < lines.count, !lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("```") { i += 1 }
        guard i < lines.count else { return "" }
        i += 1
        var out: [String] = []
        while i < lines.count, !lines[i].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
            out.append(lines[i]); i += 1
        }
        return out.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // Plain text after `marker` until the next "## " heading or blank-then-heading.
    private static func extractSection(_ lines: [String], after marker: String) -> String {
        guard let start = indexOfHeading(lines, marker) else { return "" }
        var out: [String] = []
        var i = start + 1
        while i < lines.count {
            let t = lines[i].trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("## ") { break }
            out.append(lines[i]); i += 1
        }
        return out.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // The footer "## Final Output" (the LAST occurrence — coordinator messages may
    // embed their own), up to the "---" marker rule.
    private static func extractFinalOutput(_ lines: [String]) -> String? {
        guard let _ = indexOfHeading(lines, "## Coordinator Decision") else { return nil }
        // Find the last "## Final Output".
        var idx: Int? = nil
        for (n, l) in lines.enumerated() where l.trimmingCharacters(in: .whitespaces) == "## Final Output" {
            idx = n
        }
        guard let start = idx else { return nil }
        var out: [String] = []
        var i = start + 1
        while i < lines.count {
            if lines[i].trimmingCharacters(in: .whitespaces) == "---" { break }
            out.append(lines[i]); i += 1
        }
        let s = out.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        return s.isEmpty ? nil : s
    }

    private static func extractMarker(_ lines: [String]) -> String? {
        for l in lines.reversed() {
            let t = l.trimmingCharacters(in: .whitespaces)
            if t == "CONSENSUS: YES" || t == "VOTE_DECISION: YES" || t == "VOTE_DECISION: NO" {
                return t
            }
        }
        return nil
    }
}
