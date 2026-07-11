<!-- keywords: swiftui pitfalls, ios common mistakes, main thread ui update, async await task mainactor, retain cycle weak self, @state source of truth, foreach id identity, list identity pitfall, body recomputation performance, @observable observation framework, swift concurrency sendable, headless xcode build, missing files xcode target pbxproj, wrong bundle identifier, unsigned build codesign simulator, @stateobject vs @observedobject, environment object crash, swiftui performance jank, xcodegen tuist project generation, asyncimage downsample thumbnail, task cancellation debounce, bindable environment value -->

# Common iOS & SwiftUI Pitfalls: Symptoms and Fixes

Reference for building iPhone apps on **iOS 17+ / Swift 5.9+** (validated against Xcode 26 / Swift 6). Each entry is **symptom → fix** with compilable, idiomatic code. First-party frameworks only; zero third-party dependencies.

---

## 0. Fast rules (read first)

- Prefer `@Observable` (Observation framework, iOS 17+) over `ObservableObject`/`@Published` for new view models.
- All UI mutation happens on the main actor. Never call SwiftUI/UIKit off-main.
- `Task {}` inherits the enclosing actor and priority; `Task.detached {}` inherits neither. Know which you want.
- A view's `body` is a pure function of its inputs. It can run many times per second — keep it cheap and side-effect-free.
- Every `ForEach` element needs a **stable** identity. Never key on array index for mutable data.
- `@State` is for view-local, view-owned, transient state — nothing else.
- When generating a project headlessly, the `.pbxproj`/target membership, bundle id, and signing config matter as much as the Swift code. A perfect source file that isn't in the target does not compile.

---

## 1. Main-thread / UI updates

### 1.1 Mutating UI state from a background thread

**Symptom:** Purple runtime warning "Publishing changes from background threads is not allowed"; UI updates late, flickers, or crashes; `_dispatch_assert_queue_fail`.

**Cause:** State that drives the UI is written from a background queue (e.g. a `URLSession` completion handler, a `DispatchQueue.global` block, a C callback).

**Fix — hop to the main actor before touching UI state:**

```swift
// WRONG: completion handler runs on a background thread
URLSession.shared.dataTask(with: url) { data, _, _ in
    self.items = decode(data)   // ❌ background-thread UI mutation
}.resume()

// RIGHT: async/await keeps you on the caller's actor
@MainActor
func load() async throws {
    let (data, _) = try await URLSession.shared.data(from: url)
    items = try JSONDecoder().decode([Item].self, from: data) // ✅ on main
}
```

If you're stuck with a callback API, bridge explicitly:

```swift
someLegacyAPI { result in
    Task { @MainActor in
        self.items = result   // ✅ explicit hop to main
    }
}
```

- Do **not** sprinkle `DispatchQueue.main.async` everywhere as a superstition. Model the actor boundary once (usually `@MainActor` on the view model) and let the compiler enforce it.
- `@Observable` classes are **not** implicitly main-actor. Mark the type `@MainActor` if it owns UI state.

### 1.2 Blocking the main thread with sync work

**Symptom:** Frozen UI, dropped frames, watchdog `0x8badf00d` termination on launch, a spinner that never spins.

**Cause:** File I/O, JSON decoding of large payloads, image resizing, or `Data(contentsOf:)` on the main thread.

**Fix — move CPU/IO work off-main, then return the result. Prefer the async `byPreparingThumbnail` API, which does the decode/resize off the main thread and never blocks:**

```swift
@MainActor
func thumbnail(from data: Data) async -> UIImage? {
    guard let image = UIImage(data: data) else { return nil }
    // Decode + downsample happen off-main; returns on the calling actor.
    return await image.byPreparingThumbnail(of: CGSize(width: 200, height: 200))
}
```

- Never use `Thread.sleep`, semaphores, or `DispatchGroup.wait()` on the main thread.
- Never do synchronous networking (`Data(contentsOf: remoteURL)`) — it blocks and is not cancellable.

---

## 2. async/await, Task, and @MainActor

### 2.1 `Task {}` vs `Task.detached {}`

**Symptom:** Either an unexpected main-thread hop, or a data-race warning, depending on which you picked by accident.

**Rule:**
- `Task {}` **inherits** the current actor, priority, and task-local values. Inside a `@MainActor` type, its body runs on the main actor.
- `Task.detached {}` inherits **nothing** — no actor, no priority, no task-locals. Use it only for genuinely independent background work (and reach for it rarely; a `nonisolated` async function is usually the cleaner way to get off-main).

```swift
@MainActor
final class FeedModel {
    var state: LoadState = .idle
    let api: API

    init(api: API) { self.api = api }

    func refresh() {
        Task {                    // ✅ runs on MainActor; safe to touch state
            state = .loading
            let feed = (try? await api.fetch()) ?? []
            state = .loaded(feed)
        }
    }
}
```

- Prefer structured concurrency (`async let`, `TaskGroup`) over spawning unstructured `Task {}` when results must be awaited.

### 2.2 Fire-and-forget tasks that outlive the view

**Symptom:** "Modifying state during view update" logs; work continues after the view disappears; wasted network calls; crashes touching a deallocated model.

**Fix — bind the task lifetime to the view with `.task`:**

```swift
struct FeedView: View {
    @State private var model = FeedModel(api: .live)

    var body: some View {
        List(model.items) { ItemRow(item: $0) }
            .task { await model.load() }   // ✅ auto-cancelled on disappear
    }
}
```

- `.task(id:)` re-runs when `id` changes and cancels the previous run — the correct way to react to a changing parameter (e.g. a selected user id).
- A bare `Task {}` in `onAppear` is **not** cancelled automatically. Only use it when you truly want a lifetime detached from the view, and store/cancel it yourself.

### 2.3 Ignoring cancellation

**Symptom:** A search-as-you-type view fires a request per keystroke and races; stale results overwrite fresh ones.

**Fix — debounce inside `.task(id:)`, which cancels the previous run on each new keystroke:**

```swift
.task(id: query) {
    do {
        try await Task.sleep(for: .milliseconds(300)) // debounce; throws on cancel
        results = try await search(query)
    } catch {
        // CancellationError from a superseded keystroke, or a search failure.
        // Either way, don't overwrite fresh results with stale/empty data.
    }
}
```

- `Task.sleep` throws `CancellationError` when the enclosing task is cancelled — that cancellation *is* the debounce mechanism.
- In long loops, call `try Task.checkCancellation()` periodically so cancellation actually takes effect.

### 2.4 `@MainActor` misuse

**Symptom:** "Call to main actor-isolated method in a synchronous nonisolated context"; or everything is needlessly serialized on main.

**Rules:**
- Annotate the **view-model type** with `@MainActor` (not individual methods) when it owns UI state.
- Do NOT annotate pure computation / networking types with `@MainActor` — that forces their work onto the main thread.
- From a non-isolated async context, `await` the main-actor write; the `await` is the hop, so no explicit `MainActor.run` is needed:

```swift
@MainActor final class ResultModel {
    var result: Int = 0

    // Runs off-main because it's nonisolated; the state write hops back on `await`.
    nonisolated func process() async {
        let value = await computeExpensively()   // off-main
        await setResult(value)                    // ✅ hop for the UI write
    }

    private func setResult(_ value: Int) { result = value }
}
```

Use `MainActor.run { … }` only when you need an isolated block inside otherwise-nonisolated code and can't restructure into an isolated method.

### 2.5 Swift 6 strict concurrency (Sendable)

**Symptom (building with Swift 6 mode):** "Type 'Foo' does not conform to the 'Sendable' protocol"; "Capture of 'self' with non-sendable type crossing actor boundary."

**Fix:**
- Make value-type models `struct` — they're implicitly `Sendable` when all members are `Sendable`.
- For reference types crossing actors, make them a `final class Foo: Sendable` with only immutable `let` stored properties, or isolate them to an actor / `@MainActor`.
- Prefer `let` over `var` for values captured in concurrent closures.
- If you must opt out during migration, use `@unchecked Sendable` **only** with a documented invariant (e.g. "all access guarded by `lock`") — never as a blanket silencer.

---

## 3. Retain cycles (`[weak self]`)

### 3.1 Closure capturing `self` strongly

**Symptom:** View controllers / models never deallocate; memory climbs; `deinit` never runs; timers keep firing after the screen is gone.

**Cause:** An object stores a closure that captures `self`, and the closure (transitively) is owned by `self` → cycle.

**Fix — capture `[weak self]` for stored/escaping closures:**

```swift
final class LocationTracker {
    private var timer: Timer?

    func start() {
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self else { return }   // ✅ breaks the cycle
            self.tick()
        }
    }

    func tick() { /* … */ }
    deinit { timer?.invalidate() }
}
```

**When you do NOT need `[weak self]`:**
- Non-escaping closures (`map`, `filter`, `forEach`, most higher-order fns) — no capture beyond the call.
- Structured `async`/`await` bodies and `.task {}` — SwiftUI owns and cancels these; they don't create a persistent strong reference held *by* `self`.
- One-shot `Task {}` that finishes and releases — capturing `self` strongly is usually fine and avoids the object dying mid-flight.

**Rule of thumb:** reach for `[weak self]` when the closure is **stored** and **outlives** the current scope (Combine `sink`, `NotificationCenter` observer blocks, `Timer`, delegate callbacks, long-lived completion handlers).

### 3.2 Combine subscriptions

**Symptom:** Model leaks because a `sink` closure captures `self` and the resulting `AnyCancellable` is stored on `self`.

**Fix:**

```swift
publisher
    .sink { [weak self] value in self?.handle(value) }
    .store(in: &cancellables)   // cancellables is on self → cycle without [weak self]
```

- For new code, prefer `.values` async sequences over Combine where practical.

### 3.3 Delegates

**Symptom:** Two objects keep each other alive.

**Fix:** `AnyObject`-constrain the protocol and make the delegate property `weak`:

```swift
protocol RowDelegate: AnyObject { func didTap() }
weak var delegate: RowDelegate?   // ✅ requires the AnyObject constraint
```

---

## 4. @State vs source of truth

### 4.1 Duplicating a passed-in value into `@State`

**Symptom:** The view shows stale data; parent updates don't propagate; two "copies" of the truth drift apart.

**Cause:** `@State` initializes **once** and is owned by the view. Seeding it from a parameter captures only the first value.

```swift
// WRONG: freezes the first value forever
struct Badge: View {
    @State private var count: Int
    init(count: Int) { _count = State(initialValue: count) } // ❌ stale after updates
    var body: some View { Text("\(count)") }
}

// RIGHT: read the value directly; the source of truth lives upstream
struct Badge: View {
    let count: Int                    // ✅ re-renders when the parent changes
    var body: some View { Text("\(count)") }
}
```

- Use `@State` only for state the view **creates and owns** (a toggle, a text field's draft, sheet presentation).
- To let a child mutate parent-owned state, pass a `@Binding`, not a copied `@State`.

### 4.2 Reference-type model ownership (iOS 17+)

**Symptom:** Model recreated on every render; observation doesn't fire; or (pre-Observation) the wrong wrapper.

**With the Observation framework (`@Observable`, iOS 17+):**

```swift
@Observable
final class CartModel { var items: [Item] = [] }

struct CartView: View {
    @State private var cart = CartModel()   // ✅ view OWNS the model → @State
    var body: some View { Text("\(cart.items.count)") }
}

struct CartBadge: View {
    let cart: CartModel                      // ✅ passed in, not owned → plain let
    var body: some View { Text("\(cart.items.count)") } // still observes changes
}
```

- **Owner uses `@State`.** A view that merely receives the model takes it as a `let` — Observation tracks the property reads automatically.
- Use `@Bindable` when a child needs two-way bindings into an `@Observable`: `@Bindable var cart: CartModel`, then `$cart.items`.

**Legacy `ObservableObject` mapping (only when targeting pre-17 or maintaining old code):**
- Owner: `@StateObject` (created once).
- Passed in: `@ObservedObject` (do **not** initialize it here — it would be recreated every render, losing state).
- Injected via environment: `@EnvironmentObject`.

### 4.3 `@State` that should be derived

**Symptom:** A stored `@State` drifts out of sync with the data it summarizes (e.g. `@State var total`).

**Fix — compute it; don't store it:**

```swift
// WRONG
@State private var total = 0   // must be manually kept in sync ❌

// RIGHT
var total: Int { cart.items.reduce(0) { $0 + $1.price } } // ✅ always correct
```

Store state only when it's genuinely independent input; derive everything else.

### 4.4 Missing environment value / `@EnvironmentObject`

**Symptom:** Runtime crash. For `@Observable`: "No Observable object of type X found." For legacy: "No ObservableObject of type X found. A View.environmentObject(_:) for X may be missing."

**Cause & fix:** Sheets and `NavigationStack` destinations can be presented into a different environment branch, so they don't always inherit what the presenting view had. Inject at or above the point of use.

```swift
// Modern: @Observable via .environment(_:)
.sheet(isPresented: $showSettings) {
    SettingsView()
        .environment(session)   // read with @Environment(SessionModel.self) var session
}

// Legacy: ObservableObject via .environmentObject(_:)
.sheet(isPresented: $showSettings) {
    SettingsView()
        .environmentObject(session)   // read with @EnvironmentObject var session: SessionModel
}
```

---

## 5. List identity & ForEach id pitfalls

### 5.1 Keying `ForEach` on array index

**Symptom:** Wrong rows animate; deleting row 2 visually removes row 3; text fields keep the wrong text after a reorder; per-row state attaches to the wrong item.

**Cause:** `ForEach(items.indices, id: \.self)` or `ForEach(0..<items.count)` ties identity to **position**, not to the item. When the array mutates, positions shift and SwiftUI reuses the wrong view state.

```swift
// WRONG for mutable data
ForEach(items.indices, id: \.self) { i in
    ItemRow(item: items[i])   // ❌ identity = index
}

// RIGHT: identity = the item itself
ForEach(items) { item in      // requires Item: Identifiable
    ItemRow(item: item)
}
```

- `ForEach(0..<n)` is fine **only** for a constant range (static layout). SwiftUI treats the range as constant and warns if it changes at runtime.
- Never index into an array to derive a `Binding` by position across mutations. Use `ForEach($items)` for stable element bindings:

```swift
ForEach($items) { $item in
    TextField("Name", text: $item.name)   // ✅ stable per-element binding
}
```

### 5.2 Unstable or non-unique ids

**Symptom:** Duplicate-id runtime warning ("ForEach … ID … occurs multiple times"); rows vanish or animations glitch; per-row state resets constantly.

**Causes & fixes:**
- `id: \.self` on a `String`/`Int` array with duplicates → collisions. Give each element a real unique id.
- `Identifiable` conformance returning `UUID()` **computed fresh on each access** → identity churns every render. The id must be **stored**:

```swift
// WRONG: new UUID every read → identity churns, state resets every frame
struct Item: Identifiable { var id: UUID { UUID() } }  // ❌

// RIGHT: id assigned once at creation
struct Item: Identifiable { let id = UUID() }          // ✅
```

- Don't derive `id` from a mutable field (e.g. `id: \.name`) if it can change — editing the name destroys and recreates the row, losing its state.

### 5.3 `List` diffing with value churn

**Symptom:** The whole list rebuilds and scroll position jumps on any change.

**Fix:** Keep stable ids so SwiftUI can diff minimally, and conform elements to `Equatable`/`Hashable` on stable fields. Avoid regenerating the entire array identity unnecessarily (e.g. `items = items.map { $0 }`).

---

## 6. Performance (expensive body recomputation)

### 6.1 Doing work inside `body`

**Symptom:** Scroll jank, high CPU, warm device; `body` called far more often than expected.

**Cause:** `body` runs on every dependency change (potentially every frame during animation). Sorting, filtering, date formatting, regex, or heavy allocation inside `body` repeats that work constantly.

```swift
// WRONG: sorts on every render
var body: some View {
    List(items.sorted { $0.date > $1.date }) { ItemRow(item: $0) }  // ❌
}

// RIGHT: compute once, upstream; body just reads
@Observable final class Model {
    private(set) var sorted: [Item] = []
    func setItems(_ new: [Item]) { sorted = new.sorted { $0.date > $1.date } }
}
```

- Don't allocate `DateFormatter`/`NumberFormatter` in `body` — they're expensive to create. Prefer `Date.FormatStyle` / `.formatted(_:)`, which are optimized and cached by the system:

```swift
Text(item.date, format: .dateTime.day().month().year())   // ✅ no manual formatter
Text(price, format: .currency(code: "USD"))
```

### 6.2 Over-broad observation → whole-tree invalidation

**Symptom:** A tiny change (one counter) redraws large, unrelated portions of the screen.

**Fixes:**
- Split large views into small subviews. With `@Observable`, a subview re-renders only if **it** reads the specific property that changed — invalidation is at the granularity of the view that read the property.
- Push state down to the smallest view that needs it.
- Don't pass a giant model into a leaf that needs one field — pass the field.

```swift
// A row that reads only `title` won't re-render when `subtitle` changes,
// as long as the row is its own view reading only `title`.
struct TitleRow: View {
    let title: String
    var body: some View { Text(title) }
}
```

### 6.3 `AnyView` and type erasure

**Symptom:** Lost diffing efficiency, extra allocations, a harder-to-optimize view tree.

**Fix:** Prefer `@ViewBuilder`, `if`/`switch` inside the builder, or generic `some View` over wrapping branches in `AnyView`. Reserve `AnyView` for genuinely heterogeneous collections.

### 6.4 Cascading `.onChange` / `.onReceive`

**Symptom:** One change triggers many; feedback loops.

**Fix:** Debounce (see 2.3), and prefer deriving values over reacting-and-storing. Use the iOS 17 signature `.onChange(of:initial:) { old, new in … }` deliberately — the zero-parameter and single-parameter closures behave differently.

### 6.5 Images

**Symptom:** Memory spikes, slow scrolling with photos.

**Fixes:**
- Use `AsyncImage` for remote images, but constrain size explicitly to avoid layout thrash:

```swift
AsyncImage(url: url) { image in
    image.resizable().scaledToFill()
} placeholder: {
    Color.secondary.opacity(0.2)
}
.frame(width: 100, height: 100)
.clipped()
```

- Downsample large images before display (`byPreparingThumbnail(of:)`); don't hand a 4000px image to a 100pt view.
- For big local libraries, load lazily and cache decoded thumbnails off-main.

### 6.6 `LazyVStack`/`List` vs `VStack`

**Symptom:** Hundreds of rows in a `ScrollView { VStack { ForEach … } }` build all at once — slow appearance, high memory.

**Fix:** Use `List` or `LazyVStack`/`LazyVGrid` so rows are realized on demand.

---

## 7. Headless / AI-generated build gotchas

These are the failures that make an otherwise-correct codebase fail to build or install when generated without a human clicking around Xcode.

### 7.1 Source files not added to the Xcode target

**Symptom:** "Cannot find 'X' in scope" even though the file exists on disk; or the file simply isn't compiled.

**Cause:** Writing a `.swift` file into the folder does **not** add it to `project.pbxproj`'s `PBXBuildFile` / `PBXSourcesBuildPhase`. Xcode compiles only files referenced by the target.

**Fixes (pick one and be consistent):**
- **Swift Package Manager layout** — SPM compiles every `.swift` under `Sources/<Target>/` automatically. No `.pbxproj` bookkeeping. Strongly preferred for headless generation.
- **Synchronized groups (Xcode 16+)** — a `PBXFileSystemSynchronizedRootGroup` auto-includes files in a folder, so new files are picked up without editing `pbxproj`. Confirm the generated project actually uses one.
- **Declarative generators** — describe the project with XcodeGen (`project.yml`) or Tuist and regenerate the `.xcodeproj`, so target membership is derived, never hand-patched.
- If you must hand-edit `project.pbxproj`, add matching entries in **all** of: `PBXFileReference`, `PBXBuildFile`, the group's `children`, and the target's `PBXSourcesBuildPhase.files`. Missing one yields "file not found" or "not in target."

**Verify by doing a real build:**

```bash
xcodebuild -scheme MyApp -destination 'generic/platform=iOS' \
  -configuration Debug build 2>&1 | tail -40
```

### 7.2 Wrong / malformed bundle identifier

**Symptom:** "Failed to register bundle identifier"; install fails; provisioning can't match; the app installs over an unrelated app.

**Rules:**
- Reverse-DNS, e.g. `com.acme.todo`. Allowed characters: `A–Z a–z 0–9 . -`. No underscores, spaces, or emoji.
- Keep `PRODUCT_BUNDLE_IDENTIFIER` (build settings) authoritative; reference it via `$(PRODUCT_BUNDLE_IDENTIFIER)` in `Info.plist` rather than hardcoding a divergent value.
- Extensions must use the app id as a prefix: app `com.acme.todo`, widget `com.acme.todo.widget`.

```bash
xcodebuild -scheme MyApp -showBuildSettings 2>/dev/null \
  | grep PRODUCT_BUNDLE_IDENTIFIER
```

### 7.3 Unsigned / mis-signed builds

**Symptom:** "Signing for 'MyApp' requires a development team"; "No profiles for 'X' were found"; device install fails; `errSecInternalComponent`.

**Fixes by target:**
- **Simulator builds don't need signing.** For CI/headless verification, build and run in the simulator — no team, no profile:

```bash
xcodebuild -scheme MyApp \
  -destination 'platform=iOS Simulator,name=iPhone 16' \
  -configuration Debug \
  CODE_SIGNING_ALLOWED=NO \
  build
```

- **Device builds** need a team + signing. Enable automatic signing in build settings:

```
CODE_SIGN_STYLE = Automatic
DEVELOPMENT_TEAM = ABCDE12345   // your 10-char Team ID
```

- Never commit certificates/profiles or a fake `DEVELOPMENT_TEAM`. Leave it empty and document that a real team must be set, rather than shipping a value that fails everywhere but one machine.

### 7.4 Missing / inconsistent Info.plist keys

**Symptom:** Immediate crash on first use of a capability; App Store rejection.

**Fixes:**
- Every privacy-sensitive API requires a **usage-description** string, or the app crashes when the permission prompt would show: `NSCameraUsageDescription`, `NSPhotoLibraryUsageDescription`, `NSLocationWhenInUseUsageDescription`, `NSMicrophoneUsageDescription`, `NSContactsUsageDescription`, etc.
- Modern Xcode projects use an **auto-generated Info.plist** with keys set via `INFOPLIST_KEY_*` build settings (e.g. `INFOPLIST_KEY_NSCameraUsageDescription`). Don't add a stray second `Info.plist` that conflicts with the generated one.
- Set a deployment target you actually support (`IPHONEOS_DEPLOYMENT_TARGET = 17.0`) so `@available` assumptions hold.

### 7.5 `@main` entry point issues

**Symptom:** "'main' attribute cannot be used in a module that contains top-level code"; or two `@main` types; or none.

**Fix:** Exactly one `@main` App struct, and no top-level executable statements in the app module:

```swift
@main
struct MyApp: App {
    var body: some Scene { WindowGroup { ContentView() } }
}
```

### 7.6 Assets not in an asset catalog

**Symptom:** `Image("x")` / `UIImage(named:)` returns nil at runtime; app icon missing; blank launch screen.

**Fixes:**
- Put images and colors in `Assets.xcassets` and reference them by asset name. A loose PNG in the bundle won't resolve by name unless added as a resource.
- The **app icon** must be a complete `AppIcon` set; a missing/incorrectly-sized icon fails App Store validation and can render blank on device.
- SPM resources need explicit declaration (`.process("Resources")` in the target) and access via `Bundle.module`, not `Bundle.main`.

### 7.7 Non-deterministic project churn

**Symptom:** Every generation shuffles `project.pbxproj` UUIDs / ordering, producing noisy diffs and occasional corruption.

**Fixes:**
- Prefer SPM or a declarative generator (XcodeGen / Tuist) so the `.xcodeproj` is a build artifact, not hand-edited source.
- If editing `pbxproj`, keep object ordering stable and IDs deterministic; validate with `plutil -lint MyApp.xcodeproj/project.pbxproj`.

### 7.8 Always verify with a real build + boot

**Symptom:** "It compiles in my head" — but a target-membership or signing error only surfaces at `xcodebuild`/install time.

**Minimal headless smoke test:**

```bash
# 1) Build for simulator (no signing needed)
xcodebuild -scheme MyApp \
  -destination 'platform=iOS Simulator,name=iPhone 16' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO build

# 2) Boot a simulator and install/launch to catch runtime crashes
xcrun simctl boot "iPhone 16" 2>/dev/null || true
APP=$(find ~/Library/Developer/Xcode/DerivedData -name 'MyApp.app' -path '*Debug-iphonesimulator*' | head -1)
xcrun simctl install booted "$APP"
xcrun simctl launch --console booted com.acme.todo
```

A build that passes step 1 but crashes in step 2 usually means a missing Info.plist usage-description key, a nil forced-unwrap, or an off-main UI mutation — all covered above.

---

## 8. Quick symptom → section index

| Symptom | Section |
|---|---|
| "Publishing changes from background threads" | 1.1 |
| Frozen UI / watchdog kill on launch | 1.2 |
| Unexpected thread / data race in a `Task` | 2.1, 2.5 |
| Work keeps running after view disappears | 2.2 |
| Search races / stale results overwrite fresh | 2.3 |
| Object never deallocates, `deinit` never runs | 3.x |
| Child view shows stale value from parent | 4.1 |
| Model recreated every render / state resets | 4.2, 5.2 |
| "No Observable object of type X found" | 4.4 |
| Deleting a row removes the wrong one | 5.1 |
| "ID occurs multiple times" | 5.2 |
| Scroll jank / high CPU during animation | 6.1, 6.6 |
| Tiny change redraws the whole screen | 6.2 |
| "Cannot find 'X' in scope" but file exists | 7.1 |
| "requires a development team" | 7.3 |
| Crash the instant a permission would prompt | 7.4 |
| `Image("x")` is nil at runtime | 7.6 |
