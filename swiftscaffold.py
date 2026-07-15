#!/usr/bin/env python3
"""
swiftscaffold.py — turn a library_mining extraction contract into a compilable
Swift Package Manager skeleton.

The library_mining workflow's agents PLAN reusable packages (an extraction-json
contract naming a package and its proposed public API). This module does the
deterministic follow-on the roadmap called out: render that contract into a real
SPM package on disk whose public API surface exists as always-compiling STUBS,
so `swift build` proves the skeleton is coherent and a human/agents can fill in
the real implementations.

Honesty boundary: this does NOT extract or merge real code from the mined repos
(that needs live agents). It emits stubs — an empty protocol, a struct with a
public init, an enum with a placeholder case, a no-arg func — with the agent's
proposed signature preserved as a /// doc comment (never as live code that could
reference undeclared types and break the build). The deliverable is
"here is the compilable shape; implement it", not "here is the library".

Standard library only. Pure/deterministic and never raises — unit tests call it
directly with a hand-authored dict, no agents or tokens involved.

Design decisions from the adversarial design review:
 - No Tests/ target. `swift build` doesn't compile test targets, so a generated
   Tests/ file would be unverified surface; declaring a .testTarget whose dir
   we then skip would even break `swift build` (SPM requires the dir). The
   deliverable is the library target, fully covered by plain `swift build`.
 - No platforms / no raw contract strings in Package.swift. The manifest is
   executed by SwiftPM, so it's 100% templated — product/target names derive
   from the sanitized package name; agent-authored platform/product strings are
   never interpolated in.
 - Every identifier is already sanitized by parse_extraction_blocks, but the
   package name is re-sanitized here before it's ever used in a path, so the
   dest directory is structurally incapable of path traversal.
"""

import os

import schemas as schemalib


def _doc_comment_safe(text):
    """Collapse an arbitrary (possibly multi-line, possibly hostile) string to a
    single safe /// doc-comment line. ALL Swift line terminators are stripped —
    \\n, \\r, U+2028 (line sep), U+2029 (paragraph sep) — so a crafted or merely
    multi-line signature can never escape the comment and inject live Swift."""
    if not isinstance(text, str):
        text = str(text)
    for term in ("\r\n", "\n", "\r", " ", " "):
        text = text.replace(term, " ")
    return " ".join(text.split())   # collapse remaining runs of whitespace


def _render_api_decl(item):
    """One always-compiling public declaration for a {kind, name, signature}
    entry. The proposed signature rides along as a /// comment; the actual
    declaration is a minimal body the compiler always accepts."""
    # Re-sanitize defensively: the parser normally sanitizes names, but the
    # scaffold must be robust when called directly (tests, or any caller that
    # hand-builds a dict) — an un-sanitized reserved word like `func`/`default`
    # would otherwise emit a hard Swift compile error. An unusable name skips
    # the declaration rather than breaking the build.
    name = schemalib.sanitize_swift_identifier(item.get("name"))
    if not name:
        return ""
    kind = item.get("kind")
    sig = item.get("signature")
    lines = []
    if sig:
        lines.append("/// Proposed: %s" % _doc_comment_safe(sig))
    if kind == "protocol":
        lines.append("public protocol %s {}" % name)
    elif kind == "struct":
        lines.append("public struct %s {" % name)
        lines.append("    public init() {}")
        lines.append("}")
    elif kind == "class":
        lines.append("public final class %s {" % name)
        lines.append("    public init() {}")
        lines.append("}")
    elif kind == "enum":
        lines.append("public enum %s {" % name)
        lines.append("    case placeholder")
        lines.append("}")
    elif kind == "func":
        lines.append("public func %s() {}" % name)
    else:
        return ""   # unknown kind — parser should have filtered it
    return "\n".join(lines)


def render_sources_swift(pkg):
    """`import Foundation` plus one public stub per public_api item. An empty
    public_api still yields a valid (empty-but-buildable) library source."""
    parts = ["import Foundation", ""]
    decls = [d for d in (_render_api_decl(i) for i in pkg.get("public_api", [])) if d]
    if decls:
        parts.append("\n\n".join(decls))
        parts.append("")
    return "\n".join(parts) + "\n"


def render_package_swift(pkg):
    """A minimal, fully-templated SPM manifest: one library product + one target,
    named after the sanitized package name. No platforms, no test target, no raw
    contract strings (the manifest is executed by SwiftPM, so nothing
    agent-authored is interpolated in)."""
    name = pkg["package_name"]
    return (
        "// swift-tools-version:5.9\n"
        "import PackageDescription\n\n"
        "let package = Package(\n"
        '    name: "%s",\n'
        "    products: [\n"
        '        .library(name: "%s", targets: ["%s"]),\n'
        "    ],\n"
        "    targets: [\n"
        '        .target(name: "%s"),\n'
        "    ]\n"
        ")\n" % (name, name, name, name)
    )


def scaffold_spm_package(pkg, dest_dir):
    """Write <dest_dir>/<Name>/{Package.swift, Sources/<Name>/<Name>.swift} from
    an extraction contract. Returns a manifest dict {ok, package_dir, files,
    api_count, errors}. Idempotent (overwrites). Only ever touches dest_dir.
    Never raises."""
    errors = []
    manifest = {"ok": False, "package_dir": "", "files": [], "api_count": 0,
                "errors": errors}
    try:
        raw_name = (pkg or {}).get("package_name")
        # Re-sanitize defensively: the dest path is composed ONLY from a
        # sanitized identifier, so it can never contain "/", "..", etc. — path
        # traversal into dest_dir is structurally impossible regardless of what
        # the caller passed.
        name = schemalib.sanitize_swift_identifier(raw_name)
        if not name:
            errors.append("unusable package_name: %r" % (raw_name,))
            return manifest
        pkg = dict(pkg or {})
        pkg["package_name"] = name

        pkg_dir = os.path.join(dest_dir, name)
        src_dir = os.path.join(pkg_dir, "Sources", name)
        os.makedirs(src_dir, exist_ok=True)

        rel_manifest = "Package.swift"
        rel_source = os.path.join("Sources", name, "%s.swift" % name)
        with open(os.path.join(pkg_dir, rel_manifest), "w", encoding="utf-8") as fh:
            fh.write(render_package_swift(pkg))
        with open(os.path.join(pkg_dir, rel_source), "w", encoding="utf-8") as fh:
            fh.write(render_sources_swift(pkg))

        manifest["ok"] = True
        manifest["package_dir"] = pkg_dir
        manifest["files"] = [rel_manifest, rel_source]
        manifest["api_count"] = len(pkg.get("public_api", []))
        return manifest
    except OSError as exc:
        errors.append("write failed: %s" % exc)
        return manifest
    except Exception as exc:  # defensive: scaffolding must never abort a phase
        errors.append("unexpected: %s" % exc)
        return manifest
