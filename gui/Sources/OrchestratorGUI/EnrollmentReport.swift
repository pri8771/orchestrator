import SwiftUI

enum EnrollmentTrustBadge: String, Equatable {
    case verified = "Verified"
    case flagged = "Flagged"
    case notApplicable = "Not applicable"
    case unverified = "Unverified"

    static func from(verdict: String) -> EnrollmentTrustBadge {
        switch verdict.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "compliant": return .verified
        case "non-compliant": return .flagged
        case "not-applicable": return .notApplicable
        case "cannot-determine": return .unverified
        default: return .unverified
        }
    }
}

enum EnrollmentProvenanceKind: String, Equatable {
    case unverified = "Unverified claim"
    case research = "External research"
}

struct EnrollmentFinding: Equatable, Identifiable {
    let id: String
    let rule: String
    let verdict: String
    let badge: EnrollmentTrustBadge
    let evidencePaths: [String]
    let why: String
}

struct EnrollmentLintGap: Equatable, Identifiable {
    let id: String
    let kind: String
    let location: String
    let detail: String
}

struct EnrollmentProvenanceNote: Equatable, Identifiable {
    let id: String
    let kind: EnrollmentProvenanceKind
    let text: String
}

struct EnrollmentReportModel: Equatable {
    let findings: [EnrollmentFinding]
    let lintGaps: [EnrollmentLintGap]
    let provenanceNotes: [EnrollmentProvenanceNote]
    let reportBody: String
    let warnings: [String]
}

enum EnrollmentCLI {
    static func arguments(engine: URL, root: URL, source: URL) -> [String] {
        [engine.path, "--root", root.path, "--enroll", source.path]
    }

    static func createdSlug(output: String) -> String? {
        output.split(whereSeparator: \.isNewline).compactMap { line -> String? in
            let prefix = "ENROLLED:"
            let value = line.trimmingCharacters(in: .whitespaces)
            guard value.hasPrefix(prefix) else { return nil }
            let slug = value.dropFirst(prefix.count)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            return slug.isEmpty ? nil : slug
        }.last
    }
}

enum EnrollmentReportLoader {
    static func load(projectDir: URL,
                     phaseOutputs: [String: String] = [:]) -> EnrollmentReportModel {
        let artifact = newestFinalComplianceArtifact(projectDir: projectDir)
        let findingParse = artifact.map(parseFindings)
            ?? (findings: [], warning: nil)
        let body = artifact.flatMap { artifact in
            try? String(contentsOf: artifact.dir.appendingPathComponent("body.md"),
                        encoding: .utf8)
        } ?? ""
        let lint = parseLint(projectDir: projectDir)
        let trustText = [body, phaseOutputs["doc_rebuild"] ?? ""]
            .filter { !$0.isEmpty }.joined(separator: "\n")
        var warnings = lint.warning.map { [$0] } ?? []
        if let warning = findingParse.warning { warnings.append(warning) }
        if artifact == nil {
            warnings.append("No final compliance report is available yet.")
        }
        return EnrollmentReportModel(
            findings: findingParse.findings,
            lintGaps: lint.gaps,
            provenanceNotes: parseProvenanceNotes(trustText),
            reportBody: body,
            warnings: warnings)
    }

    private struct Artifact {
        let dir: URL
        let meta: [String: Any]
        let sortKey: String
    }

    private static func newestFinalComplianceArtifact(projectDir: URL) -> Artifact? {
        let root = projectDir.appendingPathComponent("artifacts", isDirectory: true)
        guard let dirs = try? FileManager.default.contentsOfDirectory(
            at: root, includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]) else { return nil }
        return dirs.compactMap { dir -> Artifact? in
            let metaURL = dir.appendingPathComponent("meta.json")
            guard let data = try? Data(contentsOf: metaURL),
                  let meta = (try? JSONSerialization.jsonObject(with: data))
                    as? [String: Any],
                  meta["type"] as? String == "compliance_report",
                  meta["status"] as? String == "final" else { return nil }
            let key = (meta["ts"] as? String) ?? (meta["id"] as? String) ?? dir.lastPathComponent
            return Artifact(dir: dir, meta: meta, sortKey: key)
        }.max { $0.sortKey < $1.sortKey }
    }

    private static func parseFindings(_ artifact: Artifact)
        -> (findings: [EnrollmentFinding], warning: String?) {
        let fields = artifact.meta["fields"] as? [String: Any]
        guard let raw = (fields?["findings"] as? [Any])
                ?? (artifact.meta["findings"] as? [Any]), !raw.isEmpty else {
            return ([], "The final compliance artifact has no readable findings; review its raw metadata.")
        }
        let parsed = raw.enumerated().compactMap { index, value -> EnrollmentFinding? in
            guard let finding = value as? [String: Any] else { return nil }
            let rule = (finding["rule"] as? String) ?? "Unknown rule"
            let verdict = (finding["verdict"] as? String) ?? "cannot-determine"
            let evidence = (finding["evidence_paths"] as? [Any])?
                .compactMap { $0 as? String } ?? []
            return EnrollmentFinding(
                id: "\(rule)-\(index)", rule: rule, verdict: verdict,
                badge: .from(verdict: verdict), evidencePaths: evidence,
                why: (finding["why"] as? String) ?? "No rationale was recorded.")
        }
        let warning = parsed.count == raw.count ? nil
            : "Some compliance findings are malformed and could not be displayed."
        return (parsed, warning)
    }

    private static func parseLint(projectDir: URL)
        -> (gaps: [EnrollmentLintGap], warning: String?) {
        let url = projectDir.appendingPathComponent("docs/provenance_lint.json")
        guard FileManager.default.fileExists(atPath: url.path) else {
            return ([], "No documentation provenance lint has been recorded; cleanliness is unknown.")
        }
        guard let data = try? Data(contentsOf: url),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let raw = root["violations"] as? [Any] else {
            return ([], "Documentation provenance lint is unreadable; cleanliness is unknown.")
        }
        let gaps = raw.enumerated().compactMap { index, value -> EnrollmentLintGap? in
            guard let gap = value as? [String: Any] else { return nil }
            let source = (gap["source"] as? String) ?? "rebuilt docs"
            let line: String
            if let number = gap["line"] as? Int { line = String(number) } else { line = "?" }
            return EnrollmentLintGap(
                id: "\(source)-\(line)-\(index)",
                kind: (gap["kind"] as? String) ?? "provenance_violation",
                location: "\(source):\(line)",
                detail: (gap["detail"] as? String) ?? "Provenance needs human review.")
        }
        return (gaps, nil)
    }

    private static func parseProvenanceNotes(_ text: String) -> [EnrollmentProvenanceNote] {
        var notes: [EnrollmentProvenanceNote] = []
        for (index, line) in text.split(whereSeparator: \.isNewline).enumerated() {
            let value = String(line).trimmingCharacters(in: .whitespaces)
            let kind: EnrollmentProvenanceKind?
            if value.contains("[UNVERIFIED]") {
                kind = .unverified
            } else if value.contains("[RESEARCH:") {
                kind = .research
            } else {
                kind = nil
            }
            if let kind {
                notes.append(EnrollmentProvenanceNote(
                    id: "\(kind.rawValue)-\(index)", kind: kind, text: value))
            }
        }
        return notes
    }
}

struct EnrollmentReportView: View {
    let project: Project
    @Environment(\.dismiss) private var dismiss

    private var report: EnrollmentReportModel {
        EnrollmentReportLoader.load(projectDir: project.dirURL,
                                    phaseOutputs: project.phaseOutputs)
    }

    var body: some View {
        let model = report
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: DS.space.xxs) {
                    Text("Enrollment report").font(DS.font.title)
                    Text(project.name).font(DS.font.caption).foregroundStyle(DS.textSecondary)
                }
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(DS.space.margin)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: DS.space.zone) {
                    ForEach(model.warnings, id: \.self) { warning in
                        InlineBanner(kind: .warning, title: "Evidence incomplete",
                                     message: warning)
                    }
                    reportSection("Compliance findings", symbol: "checklist") {
                        if model.findings.isEmpty {
                            Text("No findings were recorded.")
                                .foregroundStyle(DS.textSecondary)
                        } else {
                            ForEach(model.findings) { finding in findingCard(finding) }
                        }
                    }
                    reportSection("Documentation provenance gaps",
                                  symbol: "doc.text.magnifyingglass") {
                        if model.lintGaps.isEmpty {
                            Label("No recorded provenance gaps", systemImage: "checkmark.circle.fill")
                                .foregroundStyle(DS.status.success.color)
                        } else {
                            ForEach(model.lintGaps) { gap in lintCard(gap) }
                        }
                    }
                    reportSection("Trust annotations", symbol: "eye") {
                        if model.provenanceNotes.isEmpty {
                            Text("No [UNVERIFIED] or [RESEARCH] annotations were recorded.")
                                .foregroundStyle(DS.textSecondary)
                        } else {
                            ForEach(model.provenanceNotes) { note in provenanceCard(note) }
                        }
                    }
                    if !model.reportBody.isEmpty {
                        reportSection("Report body", symbol: "doc.plaintext") {
                            Text(model.reportBody)
                                .font(DS.font.monoWell)
                                .lineSpacing(DS.font.monoWellLineSpacing)
                                .textSelection(.enabled)
                        }
                    }
                }
                .padding(DS.space.margin)
            }
        }
        .frame(minWidth: 720, minHeight: 620)
    }

    private func reportSection<Content: View>(_ title: String, symbol: String,
                                              @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            Label(title, systemImage: symbol).font(DS.font.headline)
            content()
        }
    }

    private func findingCard(_ finding: EnrollmentFinding) -> some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack(alignment: .top) {
                Text(finding.rule).font(DS.font.body.weight(.medium)).textSelection(.enabled)
                Spacer()
                trustBadge(finding.badge)
            }
            Text(finding.why).font(DS.font.body).foregroundStyle(DS.textSecondary)
                .textSelection(.enabled)
            if !finding.evidencePaths.isEmpty {
                Text(finding.evidencePaths.joined(separator: " · "))
                    .font(DS.font.monoCaption).foregroundStyle(DS.textTertiary)
                    .textSelection(.enabled)
            }
        }
        .padding(DS.space.m)
        .background(RoundedRectangle(cornerRadius: DS.radius.card).fill(DS.cardBg))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.card)
            .stroke(findingTint(finding.badge).stroke, lineWidth: 1))
    }

    private func lintCard(_ gap: EnrollmentLintGap) -> some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Label(gap.kind.replacingOccurrences(of: "_", with: " "),
                  systemImage: "exclamationmark.triangle.fill")
                .font(DS.font.body.weight(.medium)).foregroundStyle(DS.status.warning.color)
            Text(gap.location).font(DS.font.monoCaption).foregroundStyle(DS.textSecondary)
            Text(gap.detail).font(DS.font.body).textSelection(.enabled)
        }
        .padding(DS.space.m)
        .background(RoundedRectangle(cornerRadius: DS.radius.card)
            .fill(DS.status.warning.fill))
    }

    private func provenanceCard(_ note: EnrollmentProvenanceNote) -> some View {
        let tint = note.kind == .unverified ? DS.status.warning : DS.accent
        return VStack(alignment: .leading, spacing: DS.space.xxs) {
            Label(note.kind.rawValue,
                  systemImage: note.kind == .unverified ? "questionmark.diamond.fill" : "books.vertical.fill")
                .font(DS.font.body.weight(.medium)).foregroundStyle(tint.color)
            Text(note.text).font(DS.font.body).textSelection(.enabled)
        }
        .padding(DS.space.m)
        .background(RoundedRectangle(cornerRadius: DS.radius.card).fill(tint.fill))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.card)
            .stroke(tint.stroke, lineWidth: 1))
    }

    private func trustBadge(_ badge: EnrollmentTrustBadge) -> some View {
        let tint = findingTint(badge)
        return Text(badge.rawValue)
            .font(DS.font.caption2)
            .foregroundStyle(tint.color)
            .padding(.horizontal, DS.space.xs).padding(.vertical, DS.space.xxs)
            .background(Capsule().fill(tint.fill))
            .overlay(Capsule().stroke(tint.stroke, lineWidth: 1))
    }

    private func findingTint(_ badge: EnrollmentTrustBadge) -> DS.Tint {
        switch badge {
        case .verified: return DS.status.success
        case .flagged: return DS.status.error
        case .notApplicable: return DS.status.idlePill
        case .unverified: return DS.status.warning
        }
    }
}
