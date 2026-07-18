import XCTest

// Generic UI crawler + declared-flow interpreter (engine: uicrawl.py).
//
// Drives ANY installed app by bundle id. Two jobs:
//   1. testCrawl — bounded BFS over the app's screens: tap every hittable
//      element, detect screen changes via an accessibility signature, record
//      dead taps (tap changed nothing), verify back-navigation restores the
//      previous screen, screenshot every screen discovered, and record any
//      crash together with the exact tap path that reached it.
//   2. testFlows — execute declared user journeys (flows.json: steps as DATA,
//      never generated Swift) and record pass/fail with the failing step.
//
// Configuration comes from TEST_RUNNER_-prefixed env vars (xcodebuild strips
// the prefix): CRAWL_TARGET (bundle id, required), CRAWL_REPORT_DIR (host
// absolute path — simulator processes share the host filesystem, the same
// mechanism snapshot-testing libraries rely on), CRAWL_MAX_SCREENS,
// CRAWL_MAX_SECONDS, CRAWL_FLOWS (path to flows.json).
//
// The report is written incrementally after every step so a crash or test
// timeout still leaves a usable report on disk.

private struct Env {
    static var target: String { ProcessInfo.processInfo.environment["CRAWL_TARGET"] ?? "" }
    static var reportDir: String { ProcessInfo.processInfo.environment["CRAWL_REPORT_DIR"] ?? "" }
    static var maxScreens: Int { Int(ProcessInfo.processInfo.environment["CRAWL_MAX_SCREENS"] ?? "") ?? 25 }
    static var maxSeconds: Double { Double(ProcessInfo.processInfo.environment["CRAWL_MAX_SECONDS"] ?? "") ?? 300 }
    static var flowsPath: String { ProcessInfo.processInfo.environment["CRAWL_FLOWS"] ?? "" }
}

// One tappable element, identified stably enough to re-query after the tree
// mutates (queries go stale the moment the UI changes).
private struct Target: Hashable {
    let kind: String        // "button" | "cell" | "link" | "tab" | "navbutton" | "field"
    let identifier: String
    let label: String
    var display: String { identifier.isEmpty ? label : identifier }
    var json: [String: String] { ["kind": kind, "id": identifier, "label": label] }
}

// MARK: - Fuzzy flow-token matching (engine: uicrawl.py) — pure helpers

/// What a flow step is trying to do, so the fuzzy tier can scope its search and
/// decide whether the match must be hittable.
enum MatchPurpose { case tap, assertExists }

/// A flow token is eligible for FUZZY (last-resort) matching only if it has at
/// least 2 characters AND contains at least one letter. This deterministically
/// REFUSES bare glyph/symbol tokens ("+", "x", ".", "°") — they fall through to
/// a hard FAIL rather than being guessed at. The build-side identifier contract
/// (flows.json tokens -> .accessibilityIdentifier) is what carries the symbol
/// case; the matcher never tries to bridge a glyph to a descriptive label.
func isFuzzyEligible(_ key: String) -> Bool {
    return key.count >= 2 && key.contains(where: { $0.isLetter })
}

/// True iff `key` occurs in `label` as a WHOLE WORD, case-insensitive: every
/// character adjacent to an occurrence is a string boundary or a non-alphanumeric
/// character. So "Start" matches "Start steeping Oolong" but NOT "Restart"/
/// "Started", and "Save" does NOT match "Unsaved". Pure index arithmetic — `key`
/// is never compiled into a regex, so a key like "+" or ".*" cannot inject a
/// pattern or widen the match.
func labelMatchesWord(_ key: String, _ label: String) -> Bool {
    let k = Array(key.lowercased())
    let l = Array(label.lowercased())
    guard !k.isEmpty, k.count <= l.count else { return false }
    func isWordChar(_ c: Character) -> Bool { return c.isLetter || c.isNumber }
    var i = 0
    while i <= l.count - k.count {
        if Array(l[i ..< i + k.count]) == k {
            let beforeOK = (i == 0) || !isWordChar(l[i - 1])
            let after = i + k.count
            let afterOK = (after == l.count) || !isWordChar(l[after])
            if beforeOK && afterOK { return true }
        }
        i += 1
    }
    return false
}

final class CrawlerTests: XCTestCase {

    private var report: [String: Any] = [:]
    private var screens: [String: [String: Any]] = [:]   // sig -> record
    private var edges: [[String: Any]] = []
    private var deadTaps: [[String: Any]] = []
    private var backViolations: [[String: Any]] = []
    private var crashes: [[String: Any]] = []
    private var a11yIssues: [[String: Any]] = []
    private var shotIndex = 0

    override func setUpWithError() throws {
        continueAfterFailure = true
        try XCTSkipIf(Env.target.isEmpty, "CRAWL_TARGET not set")
        try XCTSkipIf(Env.reportDir.isEmpty, "CRAWL_REPORT_DIR not set")
        try? FileManager.default.createDirectory(
            atPath: Env.reportDir, withIntermediateDirectories: true)
    }

    // MARK: - Screen signature & element enumeration

    private func signature(_ app: XCUIApplication) -> String {
        // Cheap, stable-enough screen fingerprint: the identifiers/labels of
        // the visible interactive + text elements. Deliberately excludes
        // frames (keyboard/scroll jitter) and values (a timer ticking must
        // not look like a navigation).
        var parts: [String] = []
        for (name, query) in [
            ("b", app.buttons), ("c", app.cells), ("t", app.staticTexts),
            ("f", app.textFields), ("s", app.secureTextFields),
            ("g", app.segmentedControls), ("w", app.switches),
        ] {
            let els = query.allElementsBoundByIndex.prefix(60)
            for e in els {
                parts.append("\(name):\(e.identifier)|\(e.label.prefix(40))")
            }
        }
        return String(parts.joined(separator: ";").hashValue, radix: 16)
    }

    private func tappables(_ app: XCUIApplication) -> [Target] {
        var out: [Target] = []
        var seen = Set<Target>()
        func add(_ kind: String, _ e: XCUIElement) {
            guard e.isHittable else { return }
            let t = Target(kind: kind, identifier: e.identifier, label: e.label)
            guard !t.display.isEmpty, !seen.contains(t) else { return }
            seen.insert(t)
            out.append(t)
        }
        for e in app.tabBars.buttons.allElementsBoundByIndex { add("tab", e) }
        for e in app.navigationBars.buttons.allElementsBoundByIndex { add("navbutton", e) }
        for e in app.buttons.allElementsBoundByIndex.prefix(40) { add("button", e) }
        for e in app.cells.allElementsBoundByIndex.prefix(25) { add("cell", e) }
        for e in app.links.allElementsBoundByIndex.prefix(15) { add("link", e) }
        for e in app.textFields.allElementsBoundByIndex.prefix(10) { add("field", e) }
        return out
    }

    private func find(_ app: XCUIApplication, _ t: Target) -> XCUIElement? {
        let query: XCUIElementQuery
        switch t.kind {
        case "tab": query = app.tabBars.buttons
        case "navbutton": query = app.navigationBars.buttons
        case "cell": query = app.cells
        case "link": query = app.links
        case "field": query = app.textFields
        default: query = app.buttons
        }
        let byId = t.identifier.isEmpty ? nil
            : query.matching(identifier: t.identifier).firstMatch
        if let e = byId, e.exists { return e }
        let byLabel = query.matching(NSPredicate(format: "label == %@", t.label)).firstMatch
        return byLabel.exists ? byLabel : nil
    }

    private func screenshot(_ name: String) -> String {
        shotIndex += 1
        let file = String(format: "%02d_%@.png", shotIndex,
                          name.replacingOccurrences(of: "/", with: "_").prefix(40) as CVarArg)
        let path = (Env.reportDir as NSString).appendingPathComponent(file)
        try? XCUIScreen.main.screenshot().pngRepresentation
            .write(to: URL(fileURLWithPath: path))
        return file
    }

    private func flushReport() {
        // testCrawl and testFlows run as SEPARATE XCTestCase instances, so
        // the report file is merged (read-overlay-write), never overwritten.
        let path = (Env.reportDir as NSString).appendingPathComponent("crawl_report.json")
        var merged: [String: Any] = [:]
        if let data = FileManager.default.contents(atPath: path),
           let existing = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            merged = existing
        }
        merged["target"] = Env.target
        if !screens.isEmpty || merged["screens"] == nil { merged["screens"] = Array(screens.values) }
        if !edges.isEmpty || merged["edges"] == nil { merged["edges"] = edges }
        if !deadTaps.isEmpty || merged["dead_taps"] == nil { merged["dead_taps"] = deadTaps }
        if !backViolations.isEmpty || merged["back_violations"] == nil { merged["back_violations"] = backViolations }
        if !crashes.isEmpty || merged["crashes"] == nil { merged["crashes"] = crashes }
        if !a11yIssues.isEmpty || merged["a11y_issues"] == nil { merged["a11y_issues"] = a11yIssues }
        for (k, v) in report { merged[k] = v }
        if let data = try? JSONSerialization.data(withJSONObject: merged,
                                                  options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: URL(fileURLWithPath: path))
        }
    }

    private func settle() { Thread.sleep(forTimeInterval: 0.9) }

    private func dismissAlertIfAny(_ app: XCUIApplication) {
        // System permission alerts run in springboard; app alerts in-app.
        let alert = app.alerts.firstMatch
        if alert.exists, alert.buttons.count > 0 {
            alert.buttons.element(boundBy: alert.buttons.count - 1).tap()
            settle()
        }
    }

    private func goBack(_ app: XCUIApplication) -> Bool {
        let nav = app.navigationBars.firstMatch
        if nav.exists, nav.buttons.count > 0 {
            let backBtn = nav.buttons.element(boundBy: 0)
            if backBtn.isHittable {
                backBtn.tap(); settle(); return true
            }
        }
        // Sheets: drag down; pushed views without a visible button: edge swipe.
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.02, dy: 0.5))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: end)
        settle()
        return true
    }

    // MARK: - Crawl

    func testCrawl() throws {
        let app = XCUIApplication(bundleIdentifier: Env.target)
        app.launch()
        settle()
        let deadline = Date().addingTimeInterval(Env.maxSeconds)

        // path[sig] = tap sequence that first reached the screen (repro steps
        // for the report; a crash path becomes a permanent regression flow).
        var pathTo: [String: [[String: String]]] = [:]
        var untried: [String: [Target]] = [:]
        var currentPath: [[String: String]] = []

        func recordScreen(_ sig: String, _ app: XCUIApplication) {
            guard screens[sig] == nil else { return }
            screens[sig] = ["sig": sig,
                            "screenshot": screenshot("screen_\(sig)"),
                            "path": pathTo[sig] ?? []]
            untried[sig] = tappables(app)
            // iOS 17 accessibility audit per newly-discovered screen. The
            // handler returning true marks issues handled (recorded, not a
            // test failure — the engine reports them as warnings).
            if #available(iOS 17.0, *) {
                try? app.performAccessibilityAudit(for: .all) { issue in
                    self.a11yIssues.append([
                        "screen": sig,
                        "type": String(describing: issue.auditType),
                        "detail": String(issue.compactDescription.prefix(160)),
                    ])
                    return true
                }
            }
            flushReport()
        }

        var sig = signature(app)
        pathTo[sig] = []
        recordScreen(sig, app)

        while Date() < deadline && screens.count <= Env.maxScreens {
            dismissAlertIfAny(app)
            guard app.state == .runningForeground else {
                crashes.append(["path": currentPath, "note": "app left foreground"])
                flushReport()
                app.launch(); settle()
                sig = signature(app); currentPath = pathTo[sig] ?? []
                recordScreen(sig, app)
                continue
            }
            guard var queue = untried[sig], !queue.isEmpty else {
                // Screen exhausted: back toward root, or relaunch when stuck.
                if currentPath.isEmpty { break }   // root exhausted -> done
                _ = goBack(app)
                let after = signature(app)
                if after == sig {   // back did nothing — hard reset to root
                    app.terminate(); app.launch(); settle()
                    currentPath = []
                    sig = signature(app)
                    recordScreen(sig, app)
                } else {
                    currentPath = pathTo[after] ?? []
                    sig = after
                    recordScreen(sig, app)
                }
                continue
            }
            let target = queue.removeFirst()
            untried[sig] = queue

            guard let el = find(app, target) else { continue }
            if target.kind == "field" {
                el.tap(); settle()
                app.typeText("Sample")
                // Keyboards cover the screen; dismiss via return if present.
                if app.keyboards.buttons["return"].exists {
                    app.keyboards.buttons["return"].tap()
                }
                settle()
                continue
            }
            el.tap()
            settle()

            if app.state != .runningForeground {
                crashes.append(["path": currentPath + [target.json],
                                "tapped": target.json,
                                "note": "app crashed or exited on tap"])
                flushReport()
                app.launch(); settle()
                currentPath = []
                sig = signature(app)
                recordScreen(sig, app)
                continue
            }
            dismissAlertIfAny(app)
            let newSig = signature(app)
            if newSig == sig {
                deadTaps.append(["screen": sig, "tapped": target.json])
                flushReport()
                continue
            }
            // Real transition or in-place change.
            let isNew = screens[newSig] == nil
            edges.append(["from": sig, "to": newSig, "via": target.json])
            if isNew {
                pathTo[newSig] = currentPath + [target.json]
                recordScreen(newSig, app)
                // Back-restoration check: leave, come back, compare.
                _ = goBack(app)
                let restored = signature(app)
                if restored != sig, screens[restored] == nil {
                    backViolations.append(["expected": sig, "got": restored,
                                           "after_visiting": newSig,
                                           "via": target.json])
                    flushReport()
                    // Keep crawling from wherever we landed.
                    pathTo[restored] = currentPath
                    recordScreen(restored, app)
                    sig = restored
                    currentPath = pathTo[restored] ?? []
                } // else: back worked; sig/currentPath unchanged.
            } else {
                sig = newSig
                currentPath = pathTo[newSig] ?? []
            }
        }
        report["bounded"] = ["screens": screens.count >= Env.maxScreens,
                            "time": Date() >= deadline]
        flushReport()
    }

    // MARK: - Declared flows

    func testFlows() throws {
        try XCTSkipIf(Env.flowsPath.isEmpty, "CRAWL_FLOWS not set")
        guard let data = FileManager.default.contents(atPath: Env.flowsPath),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let flows = root["flows"] as? [[String: Any]], !flows.isEmpty
        else { throw XCTSkip("no flows declared") }

        var results: [[String: Any]] = []
        let app = XCUIApplication(bundleIdentifier: Env.target)

        for flow in flows {
            let name = (flow["name"] as? String) ?? "unnamed"
            let steps = (flow["steps"] as? [[String: Any]]) ?? []
            app.terminate(); app.launch(); settle()
            var failure: String? = nil
            for (i, step) in steps.enumerated() {
                dismissAlertIfAny(app)
                if let label = step["tap"] as? String {
                    guard let el = locate(app, label), el.waitForExistence(timeout: 5) else {
                        failure = "step \(i + 1): no tappable element ‘\(label)’"; break
                    }
                    el.tap(); settle()
                } else if let text = step["type"] as? String {
                    let into = (step["into"] as? String) ?? ""
                    let field = into.isEmpty ? app.textFields.firstMatch
                        : (locate(app, into, kinds: [app.textFields, app.secureTextFields])
                           ?? app.textFields.firstMatch)
                    guard field.waitForExistence(timeout: 5) else {
                        failure = "step \(i + 1): no text field ‘\(into)’"; break
                    }
                    field.tap(); app.typeText(text); settle()
                } else if let label = step["assert_exists"] as? String {
                    if !(locate(app, label, purpose: .assertExists)?
                            .waitForExistence(timeout: 5) ?? false)
                        && !app.staticTexts[label].waitForExistence(timeout: 2) {
                        failure = "step \(i + 1): expected ‘\(label)’ on screen"; break
                    }
                } else if step["back"] != nil {
                    _ = goBack(app)
                }
                if app.state != .runningForeground {
                    failure = "step \(i + 1): app crashed"; break
                }
            }
            results.append(["name": name, "passed": failure == nil,
                            "failure": failure ?? ""])
        }
        report["flows"] = results
        flushReport()
    }

    private func locate(_ app: XCUIApplication, _ key: String,
                        kinds: [XCUIElementQuery]? = nil,
                        purpose: MatchPurpose = .tap) -> XCUIElement? {
        let queries = kinds ?? [app.buttons, app.cells, app.tabBars.buttons,
                                app.navigationBars.buttons, app.links,
                                app.staticTexts, app.textFields]
        // PASS 1 — EXACT (byte-identical to the original matcher): identifier,
        // then label. Every currently-green flow keeps matching here; the fuzzy
        // PASS 2 runs ONLY when PASS 1 found nothing (a step that would already
        // FAIL), so it can only ever rescue a would-be failure, never break a
        // passing one.
        for q in queries {
            let byId = q.matching(identifier: key).firstMatch
            if byId.exists { return byId }
            let byLabel = q.matching(NSPredicate(format: "label == %@", key)).firstMatch
            if byLabel.exists { return byLabel }
        }
        return fuzzyLocate(app, key, kinds: kinds, purpose: purpose)
    }

    /// PASS 2 — GUARDED FUZZY (last resort). Rescues visible-label drift ("Start"
    /// -> accessibilityLabel "Start steeping Oolong") WITHOUT ever relaxing the
    /// gate's pass/fail decision. Five guards: (1) eligibility — glyph-only tokens
    /// are refused; (2) label-only — never fuzzes an identifier; (3) word-boundary
    /// only (labelMatchesWord); (4) unique-or-refuse — >1 distinct match FAILS the
    /// step (ambiguity is under-specification, never a guess); (5) hittable — a
    /// fuzzy TAP target must be hittable (a dead/offscreen control can't satisfy a
    /// tap). A genuinely absent control still yields zero matches and FAILS.
    private func fuzzyLocate(_ app: XCUIApplication, _ key: String,
                             kinds: [XCUIElementQuery]?,
                             purpose: MatchPurpose) -> XCUIElement? {
        guard isFuzzyEligible(key) else { return nil }
        let queries: [XCUIElementQuery]
        if let kinds = kinds {
            queries = kinds
        } else if purpose == .assertExists {
            // assert_exists may match a static text — an asserted string is
            // often user-visible content, not an interactive control.
            queries = [app.buttons, app.cells, app.tabBars.buttons,
                       app.navigationBars.buttons, app.links, app.staticTexts]
        } else {
            // a tap/type target must be an interactive control, never a label.
            queries = [app.buttons, app.cells, app.tabBars.buttons,
                       app.navigationBars.buttons, app.links, app.textFields]
        }
        let requireHittable = (purpose != .assertExists)
        var matches: [XCUIElement] = []
        var seen = Set<String>()
        for q in queries {
            for el in q.allElementsBoundByIndex.prefix(40) {
                guard el.exists, labelMatchesWord(key, el.label) else { continue }
                if requireHittable && !el.isHittable { continue }
                // Dedupe so a control and a duplicate query hit don't inflate the
                // ambiguity count (elementType+id+label+frame identifies one).
                let sig = "\(el.elementType.rawValue)|\(el.identifier)|\(el.label)|\(el.frame)"
                if seen.insert(sig).inserted { matches.append(el) }
            }
        }
        return matches.count == 1 ? matches[0] : nil
    }
}
