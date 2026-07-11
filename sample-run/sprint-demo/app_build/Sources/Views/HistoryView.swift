import SwiftData
import SwiftUI

struct HistoryView: View {
    @Environment(\.dismiss) private var dismiss
    @Query(sort: \SessionEntry.completedAt, order: .reverse) private var entries: [SessionEntry]

    var body: some View {
        NavigationStack {
            Group {
                if entries.isEmpty {
                    ContentUnavailableView(
                        "No Sessions Yet",
                        systemImage: "clock",
                        description: Text("Completed work and break sessions will show up here.")
                    )
                } else {
                    List(entries) { entry in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(entry.mode.displayName)
                                    .font(.headline)
                                Text(entry.completedAt, format: .dateTime.month().day().hour().minute())
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text(durationLabel(for: entry.durationSeconds))
                                .font(.subheadline.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .navigationTitle("History")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") {
                        dismiss()
                    }
                }
            }
        }
    }

    private func durationLabel(for seconds: Int) -> String {
        "\(seconds / 60) min"
    }
}
