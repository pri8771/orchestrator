import SwiftUI

struct SettingsView: View {
    @Environment(TimerEngine.self) private var engine
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("Durations") {
                    Stepper("Work: \(engine.workDurationMinutes) min", value: Binding(
                        get: { engine.workDurationMinutes },
                        set: { engine.updateConfiguration(workDurationMinutes: $0, shortBreakDurationMinutes: engine.shortBreakDurationMinutes, longBreakDurationMinutes: engine.longBreakDurationMinutes, sessionsBeforeLongBreak: engine.sessionsBeforeLongBreak) }
                    ), in: 1...60)
                    
                    Stepper("Short Break: \(engine.shortBreakDurationMinutes) min", value: Binding(
                        get: { engine.shortBreakDurationMinutes },
                        set: { engine.updateConfiguration(workDurationMinutes: engine.workDurationMinutes, shortBreakDurationMinutes: $0, longBreakDurationMinutes: engine.longBreakDurationMinutes, sessionsBeforeLongBreak: engine.sessionsBeforeLongBreak) }
                    ), in: 1...30)
                    
                    Stepper("Long Break: \(engine.longBreakDurationMinutes) min", value: Binding(
                        get: { engine.longBreakDurationMinutes },
                        set: { engine.updateConfiguration(workDurationMinutes: engine.workDurationMinutes, shortBreakDurationMinutes: engine.shortBreakDurationMinutes, longBreakDurationMinutes: $0, sessionsBeforeLongBreak: engine.sessionsBeforeLongBreak) }
                    ), in: 1...60)
                }

                Section("Settings") {
                    Stepper("Sessions before long break: \(engine.sessionsBeforeLongBreak)", value: Binding(
                        get: { engine.sessionsBeforeLongBreak },
                        set: { engine.updateConfiguration(workDurationMinutes: engine.workDurationMinutes, shortBreakDurationMinutes: engine.shortBreakDurationMinutes, longBreakDurationMinutes: engine.longBreakDurationMinutes, sessionsBeforeLongBreak: $0) }
                    ), in: 1...10)
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") {
                        dismiss()
                    }
                }
            }
        }
    }
}
