// V3 board 3.0 (sub-PR B): the GUI half of the nested-layout contract.
//
// Everything here mirrors orchestrator.py byte-for-byte and is pinned by
// the SHARED fixture tests/fixtures/lock_encoding.json (a Python test and
// a Swift test both consume it; neither side may "adjust" it to pass).
//
// Session id: flat "name" or nested "project/section/chat" — the one
// string the engine, events, Project.name, appLocks keys and search all
// carry. Lock filename stem: flat ids RAW (a live flat run's lock must
// survive the upgrade); nested ids percent-encode the NFC-normalized id
// (unreserved A-Za-z0-9_.~-, uppercase hex, UTF-8 bytes) + "." + the
// first 8 hex of SHA256 over the NFC-normalized LOWERCASED id — so lock
// files coalesce exactly when a case/normalization-insensitive
// filesystem (macOS APFS default) coalesces the session dirs.

import Foundation
import CryptoKit

enum SessionLayout {
    static let sectionsMarker = ".orch-sections"

    static func isNested(_ id: String) -> Bool { id.contains("/") }

    /// Mirror of the engine's valid_app_slug: one plain folder segment.
    static func validSlug(_ s: String) -> Bool {
        !s.isEmpty && !s.contains("..") && !s.hasPrefix(".")
            && !s.contains("/") && !s.contains("\\")
    }

    /// Exact mirror of urllib.parse.quote(s, safe=""): UTF-8 bytes,
    /// unreserved A-Za-z0-9_.~- kept, everything else %XX uppercase.
    static func percentEncode(_ s: String) -> String {
        var out = ""
        for byte in Array(s.utf8) {
            let c = Character(UnicodeScalar(byte))
            if (byte >= 0x41 && byte <= 0x5A) || (byte >= 0x61 && byte <= 0x7A)
                || (byte >= 0x30 && byte <= 0x39)
                || c == "_" || c == "." || c == "~" || c == "-" {
                out.append(c)
            } else {
                out += String(format: "%%%02X", byte)
            }
        }
        return out
    }

    static func encodeLockName(_ id: String) -> String {
        guard isNested(id) else { return id }
        let nfc = id.precomposedStringWithCanonicalMapping
        let quoted = percentEncode(nfc)
        // lowercased(), NOT a full casefold — Python's str.lower() and
        // Swift's lowercased() both implement Unicode default case
        // conversion, so the two sides agree byte-for-byte.
        let canon = nfc.lowercased()
        let digest = SHA256.hash(data: Data(canon.utf8))
        let hex8 = digest.prefix(4).map { String(format: "%02x", $0) }.joined()
        return "\(quoted).\(hex8)"
    }

    /// Inverse for lock-dir scanning: an encoded nested stem decodes to
    /// its session id (verified by re-encoding — a flat dir that merely
    /// LOOKS encoded fails the round-trip and stays raw).
    static func decodeLockStem(_ stem: String) -> String {
        guard let dot = stem.lastIndex(of: "."), stem.contains("%2F") else {
            return stem
        }
        let quoted = String(stem[..<dot])
        guard let decoded = quoted.removingPercentEncoding,
              encodeLockName(decoded) == stem else {
            return stem
        }
        return decoded
    }

    /// Nested-aware discovery — the ONE implementation both the static
    /// loader and the store use. Mirrors the engine's rules exactly:
    /// flat dirs with initial_prompt/ are projects (never recursed);
    /// root-level agent_state.json = legacy project (never recursed);
    /// nested recursion only under engine-minted .orch-sections marker
    /// dirs; dot/.orch_archived skips and slug validation at every level.
    static func discoverApps(rootURL: URL) -> [String] {
        let fm = FileManager.default
        guard let items = try? fm.contentsOfDirectory(atPath: rootURL.path)
        else { return [] }
        var apps: [String] = []
        for name in items.sorted() {
            if name.hasPrefix(".") { continue }
            let dir = rootURL.appendingPathComponent(name)
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: dir.path, isDirectory: &isDir),
                  isDir.boolValue else { continue }
            if fm.fileExists(atPath: dir.appendingPathComponent(
                    ".orch_archived").path) { continue }
            if fm.fileExists(atPath: dir.appendingPathComponent(
                    "initial_prompt/initial_prompt.md").path) {
                apps.append(name)
                continue
            }
            if fm.fileExists(atPath: dir.appendingPathComponent(
                    "agent_state.json").path) { continue }
            guard validSlug(name),
                  fm.fileExists(atPath: dir.appendingPathComponent(
                    sectionsMarker).path),
                  let sections = try? fm.contentsOfDirectory(atPath: dir.path)
            else { continue }
            for section in sections.sorted() {
                if section.hasPrefix(".") || !validSlug(section) { continue }
                let sdir = dir.appendingPathComponent(section)
                var sIsDir: ObjCBool = false
                guard fm.fileExists(atPath: sdir.path, isDirectory: &sIsDir),
                      sIsDir.boolValue,
                      !fm.fileExists(atPath: sdir.appendingPathComponent(
                        ".orch_archived").path),
                      let chats = try? fm.contentsOfDirectory(atPath: sdir.path)
                else { continue }
                for chat in chats.sorted() {
                    if chat.hasPrefix(".") || !validSlug(chat) { continue }
                    let cdir = sdir.appendingPathComponent(chat)
                    var cIsDir: ObjCBool = false
                    guard fm.fileExists(atPath: cdir.path,
                                        isDirectory: &cIsDir),
                          cIsDir.boolValue,
                          !fm.fileExists(atPath: cdir.appendingPathComponent(
                            ".orch_archived").path),
                          fm.fileExists(atPath: cdir.appendingPathComponent(
                            "initial_prompt/initial_prompt.md").path)
                    else { continue }
                    apps.append("\(name)/\(section)/\(chat)")
                }
            }
        }
        return apps
    }

    /// Lock file URL for a session id under the workspace lock dir.
    static func lockURL(rootURL: URL, id: String) -> URL {
        rootURL.appendingPathComponent(
            ".orch-locks/\(encodeLockName(id)).lock")
    }
}
