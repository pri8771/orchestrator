<!-- keywords: storekit 2, in-app purchase, iap, subscriptions, auto-renewable, gamekit, game center, leaderboard, achievements, core haptics, chhapticengine, haptic feedback, avfoundation, avaudioengine, avaudioplayer, avaudioapplication record permission, audio playback, mapkit, map annotations, widgetkit, home screen widget, app intents, live activity, control widget, usernotifications, local notification, push notification, healthkit, hkhealthstore, health data, first-party frameworks, zero dependencies, dependency-free ios, swiftui frameworks, ios 17, ios 18 -->

# First-Party Apple Frameworks Cheatsheet (iOS 17+ / Swift 5.9+)

Build capable iPhone apps with **zero third-party dependencies**. For each framework: when to reach for it, the entry-point type, and a compilable, idiomatic snippet or the gotcha that bites people. All examples target **iOS 17 minimum** unless a call-out says otherwise; Swift Concurrency (`async`/`await`) is preferred over completion handlers everywhere Apple offers it.

## General rules

- **Prefer these over SPM packages.** RevenueCat, Firebase Analytics, Lottie, Alamofire, etc. all have first-party equivalents good enough for most apps. Fewer deps = smaller binary, faster builds, no supply-chain risk, no privacy-manifest surprises.
- **Every framework that touches user data needs an `Info.plist` usage-description string** (or an entitlement). Missing it is a runtime crash the moment the API is used — not a compile error. The specific keys are listed per section.
- **Import the framework and its SwiftUI companion when one exists** (e.g. `import MapKit` gives you `Map`; `import StoreKit` gives you `SubscriptionStoreView`/`StoreView`).
- **Do capability checks before hardware-dependent APIs** (`CHHapticEngine.capabilitiesForHardware().supportsHaptics`, `HKHealthStore.isHealthDataAvailable()`, `GKLocalPlayer.local.isAuthenticated`). Simulators and older/cheaper devices lie.
- **Never block the main actor.** Long queries, audio-graph setup, and StoreKit calls belong in `Task {}` or `async` functions.
- **Verify APIs against the deployment target.** Several SwiftUI conveniences (e.g. `GameCenterView`, Control widgets) shipped *after* iOS 17. Availability call-outs below flag these.

---

## StoreKit 2 — In-App Purchase & Subscriptions

**Use it when:** you sell anything — consumables, non-consumables, or auto-renewable subscriptions. Replaces callback-based StoreKit 1 entirely.

**Start from:** `Product` (fetch + purchase) and `Transaction` (entitlements + history). No server strictly required — signatures are verified on-device via `VerificationResult`.

### The three things you must do

1. **Load products by ID** (IDs come from App Store Connect):

```swift
import StoreKit

let productIDs = ["com.app.pro.monthly", "com.app.pro.yearly", "com.app.coins.100"]
let products = try await Product.products(for: productIDs)   // async throws
```

2. **Purchase, then verify and finish the transaction:**

```swift
func purchase(_ product: Product) async throws -> Transaction? {
    let result = try await product.purchase()
    switch result {
    case .success(let verification):
        let transaction = try checkVerified(verification)
        await transaction.finish()          // REQUIRED — unfinished txns replay forever
        return transaction
    case .userCancelled, .pending:
        return nil
    @unknown default:
        return nil
    }
}

func checkVerified<T>(_ result: VerificationResult<T>) throws -> T {
    switch result {
    case .unverified(_, let error): throw error   // signature failed — do NOT grant
    case .verified(let safe):       return safe
    }
}
```

3. **Listen for `Transaction.updates` from app launch** — the single most-missed requirement. It catches renewals, Ask-to-Buy approvals, refunds, and purchases made on other devices.

```swift
// Start this in your App init / a top-level task and keep it alive for the app's lifetime.
func observeTransactions() -> Task<Void, Never> {
    Task.detached {
        for await update in Transaction.updates {
            guard case .verified(let transaction) = update else { continue }
            await grantEntitlement(for: transaction)   // your unlock logic
            await transaction.finish()
        }
    }
}
```

### Checking what the user currently owns

Use `Transaction.currentEntitlements` (an async sequence). **iOS 18.4+ note:** the per-product `Transaction.currentEntitlement(for:)` is deprecated in favor of `Transaction.currentEntitlements(for:)`, because a user can hold multiple entitling transactions (e.g. own it *and* have Family Sharing access).

```swift
func hasActiveSubscription() async -> Bool {
    for await result in Transaction.currentEntitlements {
        guard case .verified(let transaction) = result else { continue }
        if transaction.productType == .autoRenewable && transaction.revocationDate == nil {
            return true
        }
    }
    return false
}
```

### Subscription status & renewal info

```swift
// groupID = the subscription group configured in App Store Connect
let statuses = try await Product.SubscriptionInfo.status(for: subscriptionGroupID)
for status in statuses {
    guard case .verified(let renewalInfo) = status.renewalInfo else { continue }
    let willAutoRenew = renewalInfo.willAutoRenew   // false => user cancelled, still in period
    _ = (status.state, willAutoRenew)               // .subscribed, .expired, .inGracePeriod, ...
}
```

### Drop-in SwiftUI storefront (iOS 17+)

Skip building a custom paywall — these render Apple's native UI:

```swift
import SwiftUI
import StoreKit

// Whole subscription group with a marketing header:
SubscriptionStoreView(groupID: subscriptionGroupID) {
    VStack { Text("Unlock Pro").font(.largeTitle.bold()) }
}
.subscriptionStoreControlStyle(.prominentPicker)

// Non-subscription products:
StoreView(ids: ["com.app.coins.100", "com.app.coins.500"])
```

### Gotchas

- **Test locally with a `.storekit` configuration file** (Xcode → File → New → File → StoreKit Configuration). Lets you buy, refund, and fast-forward renewals in the simulator with no sandbox account.
- `await transaction.finish()` is mandatory — forgetting it makes the transaction reappear in `Transaction.updates` every launch.
- **Always ship a "Restore Purchases" button** for non-consumables/subscriptions (App Review requirement). Call `try await AppStore.sync()`, then re-scan `currentEntitlements`.
- **Consumables are not in `currentEntitlements`** — grant them at purchase time and persist the count yourself.
- For a stable per-Apple-Account identifier, read `AppTransaction.shared`. The `originalAppVersion`/`bundleID` fields are available from iOS 16; the dedicated `appTransactionID` property is **iOS 18.4+** only — don't assume it on older targets.

---

## GameKit — Game Center (leaderboards, achievements, matchmaking)

**Use it when:** you want leaderboards, achievements, or player-vs-player without running your own backend or auth.

**Start from:** `GKLocalPlayer.local` — authenticate first; nothing else works until you do.

### Authenticate (do this early, once)

```swift
import GameKit
import UIKit

func authenticatePlayer() {
    GKLocalPlayer.local.authenticateHandler = { viewController, error in
        if let vc = viewController {
            // Present Apple's sign-in UI from the active window scene's root VC.
            let root = UIApplication.shared.connectedScenes
                .compactMap { $0 as? UIWindowScene }
                .flatMap { $0.windows }
                .first { $0.isKeyWindow }?.rootViewController
            root?.present(vc, animated: true)
        } else if GKLocalPlayer.local.isAuthenticated {
            // Ready: submit scores, load achievements, matchmake.
        } else {
            // User declined or is not signed in — degrade gracefully, do NOT block the app.
        }
    }
}
```

### Submit a score (modern async API, iOS 14+)

```swift
try await GKLeaderboard.submitScore(
    5000, context: 0, player: GKLocalPlayer.local,
    leaderboardIDs: ["com.app.highscores"]        // IDs from App Store Connect
)
```

### Report achievement progress

```swift
let achievement = GKAchievement(identifier: "com.app.first_win")
achievement.percentComplete = 100
achievement.showsCompletionBanner = true
try await GKAchievement.report([achievement])
```

### Show the built-in Game Center dashboard

The pure-SwiftUI `GameCenterView` is **iOS 26+ only**. For an iOS 17 deployment target, wrap `GKGameCenterViewController`:

```swift
import SwiftUI
import GameKit

struct GameCenterDashboard: UIViewControllerRepresentable {
    let leaderboardID: String

    func makeUIViewController(context: Context) -> GKGameCenterViewController {
        let vc = GKGameCenterViewController(leaderboardID: leaderboardID,
                                            playerScope: .global,
                                            timeScope: .allTime)
        vc.gameCenterDelegate = context.coordinator
        return vc
    }
    func updateUIViewController(_ vc: GKGameCenterViewController, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator() }
    final class Coordinator: NSObject, GKGameCenterControllerDelegate {
        func gameCenterViewControllerDidFinish(_ vc: GKGameCenterViewController) {
            vc.dismiss(animated: true)
        }
    }
}

// Usage: .sheet(isPresented: $show) { GameCenterDashboard(leaderboardID: "com.app.highscores") }
```

### Gotchas

- **Guard every call with `GKLocalPlayer.local.isAuthenticated`.** Auth can fail silently (no Game Center account, restricted device). Never crash or lock the player out.
- Leaderboard/achievement IDs are configured in App Store Connect and must match exactly.
- Achievements only reach 100% once; report incremental `percentComplete` for progress.

---

## Core Haptics — Custom tactile feedback

**Use it when:** you need richer feedback than the standard generators — game hits, textures, sync'd-to-audio rumbles, custom curves.
> For simple UI taps (button press, selection, success/error), use SwiftUI's `.sensoryFeedback(_:trigger:)` (iOS 17+) or `UIImpactFeedbackGenerator`/`UINotificationFeedbackGenerator` — don't reach for Core Haptics.

**Start from:** `CHHapticEngine`. Capability-check first — iPad and older/low-end devices return `false`.

```swift
import CoreHaptics

final class Haptics {
    private var engine: CHHapticEngine?

    func prepare() {
        guard CHHapticEngine.capabilitiesForHardware().supportsHaptics else { return }
        do {
            let engine = try CHHapticEngine()
            // The system stops the engine on interruptions; restart on demand.
            engine.resetHandler = { [weak engine] in try? engine?.start() }
            try engine.start()
            self.engine = engine
        } catch { engine = nil }
    }

    /// A sharp tap. `.hapticTransient` = instant; `.hapticContinuous` = sustained rumble (≤30s).
    func tap(intensity: Float = 1, sharpness: Float = 1) {
        guard let engine else { return }
        let event = CHHapticEvent(
            eventType: .hapticTransient,
            parameters: [
                CHHapticEventParameter(parameterID: .hapticIntensity, value: intensity),
                CHHapticEventParameter(parameterID: .hapticSharpness, value: sharpness)
            ],
            relativeTime: 0
        )
        do {
            let pattern = try CHHapticPattern(events: [event], parameters: [])
            let player = try engine.makePlayer(with: pattern)
            try player.start(atTime: CHHapticTimeImmediate)
        } catch { /* haptics are non-critical — swallow */ }
    }
}
```

### Gotchas

- **Restart the engine after interruptions.** The system stops it when the app backgrounds or a call comes in; use `resetHandler` (and optionally `stoppedHandler`) to re-`start()`. A "haptics stopped working" bug is almost always a dead engine.
- `intensity` and `sharpness` are `Float`s in `0...1`.
- **Ship `.ahap` files for complex patterns** — author them as JSON and load with `try engine.playPattern(from: url)` instead of hand-building events in code.
- Continuous events cap at 30 seconds.

---

## AVFoundation — Audio playback & recording

**Use it when:** you play sounds/music, record audio, or need mixing/looping/spatial audio. For a single sound file, this is all you need — no packages.

**Start from:**
- `AVAudioPlayer` — simplest: play a bundled file, loop, set volume.
- `AVAudioEngine` — a node graph for mixing, effects, real-time, and precise scheduling.
- `AVAudioSession` — **configure this first** or playback behaves wrong (silent switch, ducking, background).

### Configure the session (once, at launch)

```swift
import AVFoundation

let session = AVAudioSession.sharedInstance()
// .playback => plays even when the ring/silent switch is on; ideal for media & games.
try session.setCategory(.playback, mode: .default)
try session.setActive(true)
```

### Play a bundled sound

```swift
final class SoundPlayer {
    private var player: AVAudioPlayer?          // MUST be a property — see gotchas
    func play(_ name: String, ext: String = "caf", loops: Int = 0) {
        guard let url = Bundle.main.url(forResource: name, withExtension: ext) else { return }
        player = try? AVAudioPlayer(contentsOf: url)
        player?.numberOfLoops = loops     // -1 = loop forever
        player?.prepareToPlay()
        player?.play()
    }
}
```

### Record (needs mic permission — request it first)

The iOS 17+ permission API lives on `AVAudioApplication`, not `AVAudioSession` (the latter's `requestRecordPermission` is deprecated):

```swift
import AVFoundation

func startRecording() async throws -> AVAudioRecorder? {
    guard await AVAudioApplication.requestRecordPermission() else { return nil }

    let session = AVAudioSession.sharedInstance()
    try session.setCategory(.playAndRecord, mode: .default)
    try session.setActive(true)

    let url = FileManager.default.temporaryDirectory.appendingPathComponent("rec.m4a")
    let settings: [String: Any] = [
        AVFormatIDKey: kAudioFormatMPEG4AAC,
        AVSampleRateKey: 44_100,
        AVNumberOfChannelsKey: 1,
        AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue
    ]
    let recorder = try AVAudioRecorder(url: url, settings: settings)
    recorder.record()
    return recorder     // retain this too
}
```

### Gotchas

- **`Info.plist` `NSMicrophoneUsageDescription` is mandatory to record** — omit it and the app is terminated the moment recording begins.
- **Retain the player/recorder** in a property. A local `AVAudioPlayer` deallocates the instant the function returns and you hear nothing — the #1 "no sound" bug.
- Use category `.playback` to keep sound on with the silent switch; `.ambient` to respect it and mix with other apps.
- **Prefer `.caf`/`.m4a` over `.mp3`** for low-latency game SFX. Preload with `prepareToPlay()`.
- A single `AVAudioPlayer` can't overlap itself. For dozens of overlapping short sounds, use `AVAudioEngine` + `AVAudioPlayerNode`s, or a pool of players.

---

## MapKit — Maps, annotations, search

**Use it when:** you show a map, place markers, draw routes, or do place search / geocoding. The SwiftUI `Map` (iOS 17 API) is dramatically simpler than the old UIKit one — use it.

**Start from:** `Map { }` with `MapContentBuilder` content (`Marker`, `Annotation`, `MapPolyline`).

```swift
import SwiftUI
import MapKit

struct MapScreen: View {
    @State private var camera: MapCameraPosition = .region(
        MKCoordinateRegion(
            center: CLLocationCoordinate2D(latitude: 37.3349, longitude: -122.0090),
            span: MKCoordinateSpan(latitudeDelta: 0.02, longitudeDelta: 0.02)
        )
    )
    let places: [Place]

    var body: some View {
        Map(position: $camera) {
            UserAnnotation()                                     // blue dot (needs location perm)
            ForEach(places) { place in
                Marker(place.name, coordinate: place.coordinate) // system pin with label
                Annotation(place.name, coordinate: place.coordinate) { // custom view
                    Image(systemName: "star.fill").padding(6)
                        .background(.yellow, in: Circle())
                }
            }
        }
        .mapControls { MapUserLocationButton(); MapCompass() }
    }
}
```

### Search for places (async, iOS 17+)

```swift
let request = MKLocalSearch.Request()
request.naturalLanguageQuery = "coffee"
if let region = camera.region { request.region = region }   // camera.region is optional
let response = try await MKLocalSearch(request: request).start()
let items = response.mapItems      // each has .placemark.coordinate, .name, .url
```

### Gotchas

- **Showing the user's location needs Core Location.** Add `NSLocationWhenInUseUsageDescription` and request authorization via `CLLocationManager`; `UserAnnotation()` is blank otherwise.
- `Marker` = quick system pin; `Annotation` = your own SwiftUI view. Don't hand-roll pins.
- `MapCameraPosition.region` is optional — it's `nil` until the map has rendered / after non-region positions like `.userLocation`. Guard it; don't fall back to a zero region.
- For directions use `MKDirections`; draw the result with `MapPolyline(route.polyline)`.
- Map tiles need network. There's no offline map — cache your own overlays if you need offline.

---

## WidgetKit — Home Screen / Lock Screen widgets, Live Activities, Controls

**Use it when:** you want glanceable content on the Home/Lock Screen, a Dynamic Island / Live Activity, or (iOS 18) a Control Center toggle.

**Start from:** `Widget` (+ a `TimelineProvider`), in a **Widget Extension target** (File → New → Target → Widget Extension). Widgets are separate processes — they can't run arbitrary code, only render timelines you supply.

### Minimal static widget

```swift
import WidgetKit
import SwiftUI

struct SimpleEntry: TimelineEntry { let date: Date; let count: Int }

struct Provider: TimelineProvider {
    func placeholder(in context: Context) -> SimpleEntry { .init(date: .now, count: 0) }
    func getSnapshot(in context: Context, completion: @escaping (SimpleEntry) -> Void) {
        completion(.init(date: .now, count: 3))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<SimpleEntry>) -> Void) {
        let entry = SimpleEntry(date: .now, count: readSharedCount())
        // Ask for a refresh in ~15 min; the system rate-limits actual refreshes.
        completion(Timeline(entries: [entry], policy: .after(.now.addingTimeInterval(900))))
    }
}

struct CounterWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "CounterWidget", provider: Provider()) { entry in
            Text("\(entry.count)")
                .containerBackground(.fill.tertiary, for: .widget)   // REQUIRED iOS 17+
        }
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryCircular])
    }
}
```

### Interactive widgets — Button/Toggle with `AppIntent` (iOS 17+)

Widgets can mutate state *without launching the app* by wiring a `Button`/`Toggle` to an `AppIntent`. The intent runs in the extension; the system reloads the timeline automatically when it returns.

```swift
import AppIntents
import WidgetKit

struct IncrementIntent: AppIntent {
    static var title: LocalizedStringResource = "Increment"
    func perform() async throws -> some IntentResult {
        incrementSharedCount()                       // write to App Group / shared store
        return .result()
    }
}

// In the widget's view:
Button(intent: IncrementIntent()) { Image(systemName: "plus") }
```

### Control widgets (iOS 18+) — Control Center / Lock Screen toggles

```swift
import WidgetKit
import AppIntents
import SwiftUI

struct FlashlightControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "com.app.flashlight") {
            ControlWidgetToggle("Flashlight",
                                isOn: isFlashlightOn(),
                                action: ToggleFlashlightIntent()) { isOn in
                Image(systemName: isOn ? "flashlight.on.fill" : "flashlight.off.fill")
            }
        }
    }
}
```

### Gotchas

- **`.containerBackground(for: .widget)` is required on iOS 17+.** Without it the widget renders with a broken background and may be rejected in review.
- **Share data via an App Group** (`group.com.yourcompany.app`) — the extension can't see the app's `UserDefaults.standard` or sandbox. Use `UserDefaults(suiteName:)` or a shared file container.
- The system **rate-limits refreshes** (roughly a few dozen per day). Don't design for second-by-second updates — use a Live Activity or push-updated widget instead.
- For an `AppIntent` shared by app and widget, set its **Target Membership to both targets**.
- Live Activities use `ActivityKit` (`Activity<Attributes>.request(...)`) + a `.dynamicIsland` widget; requires `NSSupportsLiveActivities = YES` in `Info.plist`.

---

## UserNotifications — Local & push notifications

**Use it when:** you schedule reminders, alarms, or geofenced alerts (local), or deliver server-driven pushes. This one framework covers both.

**Start from:** `UNUserNotificationCenter.current()`.

### Request permission (async)

```swift
import UserNotifications

let granted = try await UNUserNotificationCenter.current()
    .requestAuthorization(options: [.alert, .sound, .badge])
```

### Schedule a local notification

```swift
let content = UNMutableNotificationContent()
content.title = "Time to stretch"
content.body  = "You've been sitting for an hour."
content.sound = .default

// Fire in 60s. Use UNCalendarNotificationTrigger for a specific time (repeating optional).
let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 60, repeats: false)
let request = UNNotificationRequest(identifier: UUID().uuidString,
                                    content: content, trigger: trigger)
try await UNUserNotificationCenter.current().add(request)
```

### Show notifications while the app is foregrounded

By default nothing appears in-foreground. Implement the delegate:

```swift
final class NotifDelegate: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification) async
        -> UNNotificationPresentationOptions {
        [.banner, .sound]      // opt into showing it while active
    }
}
// UNUserNotificationCenter.current().delegate = notifDelegate  (set BEFORE app finishes launching)
```

### Remote push (device token)

```swift
// In your App/AppDelegate after authorization:
UIApplication.shared.registerForRemoteNotifications()
// AppDelegate.application(_:didRegisterForRemoteNotificationsWithDeviceToken:) -> send token to server.
```

### Gotchas

- **Set the delegate before the app finishes launching** (in `application(_:didFinishLaunchingWithOptions:)`), or you miss the launch notification and lose foreground presentation.
- **Local notifications don't need a server or entitlement.** Remote push needs the **Push Notifications capability** + APNs (auth key). No third-party push SDK required.
- iOS caps pending local notifications at **64** — extras are silently dropped.
- Add actionable buttons via `UNNotificationCategory` + `UNNotificationAction`; handle taps in the delegate's `didReceive response` method.
- **Time-sensitive / critical alerts** need extra entitlements from Apple — don't assume you can bypass Focus/DND.

---

## HealthKit — Read/write health & fitness data

**Use it when:** you read or contribute steps, workouts, heart rate, sleep, weight, etc. Health data only comes through HealthKit — there is no alternative.

**Start from:** `HKHealthStore`. Requires the **HealthKit capability** and `Info.plist` `NSHealthShareUsageDescription` (read) plus, if writing, `NSHealthUpdateUsageDescription`.

### Availability + authorization (async)

```swift
import HealthKit

let store = HKHealthStore()

func requestAuth() async throws {
    guard HKHealthStore.isHealthDataAvailable() else { return }   // false on iPad
    let stepType = HKQuantityType(.stepCount)
    let read: Set<HKObjectType> = [stepType, HKQuantityType(.heartRate)]
    let write: Set<HKSampleType> = [stepType]
    try await store.requestAuthorization(toShare: write, read: read)
}
```

### Read today's step total (`HKStatisticsQuery`)

`HKStatisticsQuery` has no async variant — bridge it with a continuation:

```swift
func stepsToday() async throws -> Double {
    let type = HKQuantityType(.stepCount)
    let start = Calendar.current.startOfDay(for: .now)
    let predicate = HKQuery.predicateForSamples(withStart: start, end: .now)

    return try await withCheckedThrowingContinuation { continuation in
        let query = HKStatisticsQuery(quantityType: type,
                                      quantitySamplePredicate: predicate,
                                      options: .cumulativeSum) { _, stats, error in
            if let error { continuation.resume(throwing: error); return }
            let steps = stats?.sumQuantity()?.doubleValue(for: .count()) ?? 0
            continuation.resume(returning: steps)
        }
        store.execute(query)
    }
}
```

### Write a sample

```swift
let sample = HKQuantitySample(
    type: HKQuantityType(.bodyMass),
    quantity: HKQuantity(unit: .gramUnit(with: .kilo), doubleValue: 72.5),
    start: .now, end: .now
)
try await store.save(sample)
```

### Gotchas

- **You cannot detect read-denial.** For privacy, HealthKit reports read authorization as granted even when the user said no — a denied read just returns empty results. Never gate features on `authorizationStatus(for:)` for read types; handle "no data" gracefully.
- **`isHealthDataAvailable()` is `false` on iPad** — don't assume iPhone.
- Use `HKStatisticsCollectionQuery` for bucketed data (steps per day over a week); `HKAnchoredObjectQuery` for incremental sync / long-lived observation.
- **Live/background delivery** (`enableBackgroundDelivery`, `HKObserverQuery`) needs the **Background Modes** capability.
- Quantity types are strongly typed — pass the matching `HKUnit` to `doubleValue(for:)` or it traps at runtime.

---

## Quick decision table

| Need | Framework | Entry type | Don't reach for a package |
|------|-----------|-----------|---------------------------|
| Sell / subscribe | StoreKit 2 | `Product`, `Transaction` | RevenueCat (only if you need cross-platform receipts) |
| Leaderboards / achievements | GameKit | `GKLocalPlayer.local` | Any BaaS game backend |
| Simple UI tap feedback | SwiftUI / UIKit | `.sensoryFeedback` / `UIImpactFeedbackGenerator` | — |
| Rich custom haptics | Core Haptics | `CHHapticEngine` | — |
| Play a sound / music | AVFoundation | `AVAudioPlayer` | — |
| Mixing / effects / real-time audio | AVFoundation | `AVAudioEngine` | AudioKit |
| Map + pins + search | MapKit | `Map { }` | Google Maps SDK |
| Home/Lock Screen glance | WidgetKit | `Widget` + `TimelineProvider` | — |
| Control Center toggle (iOS 18) | WidgetKit | `ControlWidget` | — |
| Reminders / scheduled alerts | UserNotifications | `UNUserNotificationCenter` | — |
| Server push | UserNotifications + APNs | `UNUserNotificationCenter` | Firebase Cloud Messaging / OneSignal |
| Steps / workouts / vitals | HealthKit | `HKHealthStore` | — |

## Info.plist / capability quick reference

- **StoreKit 2:** In-App Purchase capability. (Local `.storekit` file for testing.)
- **GameKit:** Game Center capability.
- **AVFoundation record:** `NSMicrophoneUsageDescription` + `AVAudioApplication.requestRecordPermission()`.
- **MapKit user location:** `NSLocationWhenInUseUsageDescription` + Core Location.
- **WidgetKit:** Widget Extension target; **App Group** for shared data; `NSSupportsLiveActivities` for Live Activities.
- **UserNotifications (push):** Push Notifications capability + APNs key.
- **HealthKit:** HealthKit capability + `NSHealthShareUsageDescription` (+ `NSHealthUpdateUsageDescription` to write); Background Modes for observers.
