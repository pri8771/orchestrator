# DEPENDENCIES.md

No third-party dependencies. Verified 2026-07-05:

- **Engine** (`orchestrator-v2-source/*.py`): Python 3 standard library only —
  no pip packages, no PyYAML (config.yaml is parsed by a built-in minimal
  reader).
- **GUI** (`orchestrator-v2-source/gui/`): a dependency-free SwiftPM executable
  (`Package.swift` declares zero external packages); SwiftUI/AppKit/Combine are
  system frameworks.
- **External tools used at run time, not build time**: the logged-in agent CLIs
  (codex, claude, gemini/agy), git, and optionally Xcode/xcodebuild for build
  verification and Ollama for local models. None are needed to build or test
  Orchestrator V2 itself except the Swift toolchain for the GUI.

License policy for additions:

- Build-time SDKs/packages must be permissive for commercial use: MIT,
  Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, or equivalent.
- Avoid GPL, AGPL, SSPL, source-available/proprietary, non-commercial, and
  field-of-use-restricted licenses unless explicitly approved for a specific
  integration.
- Curated local model entries must declare a license URL and pass the
  commercial-use/permissive-license registry test.
