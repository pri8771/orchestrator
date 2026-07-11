// swift-tools-version: 5.9
import PackageDescription

// Native macOS SwiftUI front-end for the autonomous multi-agent orchestrator.
// Builds as a plain SwiftPM executable (no .xcodeproj). SwiftUI / AppKit /
// Combine are system frameworks, so there are zero external dependencies.
let package = Package(
    name: "OrchestratorGUI",
    // .v14 is required for `.inspector` (Native Pro shell, DESIGN-NATIVE-PRO.md §3/M0).
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "OrchestratorGUI",
            path: "Sources/OrchestratorGUI"
        ),
        // Unit tests for the pure GUI↔engine bridge logic (EngineLogic.swift).
        // Swift 5.5+ supports @testable-importing an executable target directly,
        // so no library split is needed.
        .testTarget(
            name: "OrchestratorGUITests",
            dependencies: ["OrchestratorGUI"],
            path: "Tests/OrchestratorGUITests"
        )
    ]
)
