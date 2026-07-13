import SwiftUI
import AppKit

// MARK: - Legacy dark tokens (WorkflowBuilder sheet only)
//
// The factory dashboard these tokens skinned was deleted in M5 (§9). The
// workflow-builder sheet still renders in this dark mono style, so the tokens
// survive until that sheet is restyled onto the DS ramp — nothing else may
// adopt them.
struct ThemeTokens {
    static let bg = Color(hex: 0x0D1117)            // window background
    static let card = Color(hex: 0x161B22)          // cards / selected rows
    static let borderSubtle = Color(hex: 0x21262D)  // hairline separators
    static let borderStrong = Color(hex: 0x30363D)  // control borders
    static let well = Color(hex: 0x010409)          // terminal well
    static let accent = Color(hex: 0x9FEF00)        // lime
    static let accentOn = Color(hex: 0x0D1117)      // text on lime
    static let ok = Color(hex: 0x3FB950)
    static let fail = Color(hex: 0xF85149)
    static let text = Color(hex: 0xE6EDF3)
    static let muted = Color(hex: 0x7D8590)
    static let dim = Color(hex: 0x484F58)
    static let toggleOn = Color(hex: 0x238636)

    // Status glyphs — always paired with color, never color alone.
    static let glyphRunning = "●"
    static let glyphDone = "✓"
    static let glyphFailed = "✗"
    static let glyphQueued = "◌"

    static func mono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }
}

private extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: 1)
    }
}

// A small pulsing dot used for "live / thinking" affordances. Purely decorative
// for assistive tech — the surrounding view supplies a textual status.
struct PulseDot: View {
    let color: Color
    @State private var on = false
    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 8, height: 8)
            .opacity(on ? 1 : 0.3)
            .animation(.easeInOut(duration: 0.8).repeatForever(autoreverses: true), value: on)
            .onAppear { on = true }
            .accessibilityHidden(true)
    }
}

// MARK: - Run log sheet

// Non-modal, docked run-log panel. Lives at the bottom of the window via
// `.safeAreaInset` so the toolbar (New chat, Run, …) stays fully interactive
// while a run streams — you can add and launch more projects without closing it.
struct RunLogPanel: View {
    @EnvironmentObject var store: OrchestratorStore
    @Binding var isPresented: Bool

    // Resizable (not a fixed 200pt) and remembered across launches — a fixed
    // height ate too much of a modest-sized window. Bounds keep it from
    // disappearing entirely or swallowing the whole transcript pane.
    @AppStorage("runLogPanelHeight") private var contentHeight: Double = 160
    @State private var dragStartHeight: Double? = nil
    private let minContentHeight: Double = 90
    private let maxContentHeight: Double = 420

    var body: some View {
        VStack(spacing: 0) {
            // Drag handle to resize the panel.
            Rectangle()
                .fill(Color.clear)
                .frame(height: 7)
                .overlay(Capsule().fill(Color.secondary.opacity(0.35)).frame(width: 34, height: 3))
                .contentShape(Rectangle())
                .onHover { inside in
                    if inside { NSCursor.resizeUpDown.push() } else { NSCursor.pop() }
                }
                .gesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { value in
                            let start = dragStartHeight ?? contentHeight
                            dragStartHeight = dragStartHeight ?? contentHeight
                            contentHeight = min(maxContentHeight,
                                                max(minContentHeight, start - value.translation.height))
                        }
                        .onEnded { _ in dragStartHeight = nil }
                )
            Divider()
            HStack(spacing: 8) {
                Image(systemName: "terminal").font(DS.font.caption).foregroundStyle(.secondary)
                Text("Run log").font(DS.font.callout)
                if store.orchestratorRunning {
                    PulseDot(color: DS.accent.color)
                    Text("running").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("Copy all") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(store.runLog, forType: .string)
                }
                    .buttonStyle(.plain).font(.caption).foregroundStyle(.secondary)
                    .disabled(store.runLog.isEmpty)
                    .accessibilityLabel("Copy run log")
                    .accessibilityHint("Copies the visible run log to the clipboard")
                Button("Clear") { store.runLog = "" }
                    .buttonStyle(.plain).font(.caption).foregroundStyle(.secondary)
                    .disabled(store.runLog.isEmpty)
                    .accessibilityLabel("Clear run log")
                Button { isPresented = false } label: {
                    Image(systemName: "chevron.down").font(.subheadline.weight(.semibold))
                }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                .help("Hide the run log")
                .accessibilityLabel("Hide run log")
            }
            .padding(.horizontal, 12).padding(.vertical, 6)
            .background(.ultraThinMaterial)

            ScrollViewReader { proxy in
                ScrollView {
                    Text(store.runLog.isEmpty
                         ? "No run started yet.\n\nUse Run to launch an orchestrator pass for the selected project. This panel is non-modal — you can keep adding projects while a run streams. Drag the handle above to resize."
                         : store.runLog)
                        .font(.system(.subheadline, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                        .padding(10)
                        .id("logEnd")
                }
                .onChange(of: store.runLog) { _ in
                    withAnimation { proxy.scrollTo("logEnd", anchor: .bottom) }
                }
            }
            .frame(height: contentHeight)
            .background(Color(nsColor: .textBackgroundColor).opacity(0.6))
        }
    }
}
