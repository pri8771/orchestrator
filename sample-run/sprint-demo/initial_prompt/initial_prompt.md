# FocusFlow — a minimalist Pomodoro focus timer (iOS, SwiftUI)

Build a small, polished, LOCAL-ONLY iOS app. No backend, no accounts, no network.
Keep scope tight enough to finish and compile inside a time-boxed sprint.

## Core (must-have)
- A single main timer screen: a large circular countdown, Start / Pause / Reset.
- Work / Short-break / Long-break modes with sensible default durations (25 / 5 / 15 min).
- Auto-advance: after a work session it moves to a break, and back, tracking the cycle count.
- A lightweight session history (date, mode, duration) persisted locally with SwiftData.
- A simple Settings screen to adjust the three durations and how many work sessions precede a long break.

## Feel
- Clean, calm, modern SwiftUI. Dark-mode friendly. Haptic tick on start/complete.
- No third-party packages. iOS 17+.

## Explicitly out of scope (V2)
- iCloud sync, widgets, notifications, sounds library, statistics charts.
