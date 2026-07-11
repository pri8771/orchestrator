<!-- keywords: swiftui, ios 17, ios 18, swift 5.9, app architecture, @observable, observation framework, @state, @bindable, @environment, observableobject legacy, navigationstack, navigationdestination, navigationpath, programmatic navigation, mvvm view model, one-way data flow, unidirectional data flow, view decomposition, coordinator router, @main app scene, scenephase windowgroup, @entry environmentkey, sheet enum routing, @mainactor async model, observationignored, presentationdetents -->

# Modern SwiftUI App Architecture (iOS 17+ / Swift 5.9+)

Reference for building iPhone apps with SwiftUI on iOS 17+. First-party frameworks only, zero third-party dependencies. Snippets typecheck against the iOS 17 SDK unless marked otherwise.

Version terminology (used throughout): the **deployment target** (e.g. iOS 17.0) is what the app runs on; the **toolchain/compiler** (Xcode 16+ / Swift 6, or Xcode 15 / Swift 5.9) is what you build *with*. They are independent. Some *language* features (e.g. the `@Entry` macro) require a newer compiler but still run on iOS 17 because the macro expands to code that back-deploys. Features that need a newer *OS* are flagged "iOS 18+".

Core rules:
- Prefer the `@Observable` macro (Observation framework) over legacy `ObservableObject`/`@Published`/`@StateObject`/`@ObservedObject`/`@EnvironmentObject`. Use the legacy stack only when deploying below iOS 17 or when a third-party API forces it.
- Data flows **down** via values/bindings and **up** via closures or method calls on a model. Never mutate parent state directly from a child except through a `Binding` the parent handed down.
- A view is a value type describing UI for the current state. Keep views small; push logic into `@Observable` models.
- Don't over-engineer. Most screens need a view plus maybe one `@Observable` model. Add routers/coordinators only when navigation state must be shared or driven programmatically.

---

## App entry: `@main`, `App`, scenes

```swift
import SwiftUI

@main
struct FoodlogApp: App {
    // App-lifetime owned state. @State owns it; created once for the app.
    @State private var session = SessionModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(session)   // inject into the whole tree
        }
    }
}
```

- `@main` marks the single entry point. The type conforms to `App` and returns a `Scene` from `body`.
- `WindowGroup` is the standard scene for an iPhone app (one full-screen window). Its content closure is the root view.
- Own app-wide models with `@State` **at the `App` level** (supported since iOS 17). Don't use a global singleton for shared UI state; inject via `.environment(_:)`.
- Other scene types: `DocumentGroup` (document-based apps), `Settings` (macOS). For iPhone, `WindowGroup` is almost always correct.

### App delegate / launch hooks (only if you need UIKit lifecycle)

```swift
@main
struct FoodlogApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    var body: some Scene { WindowGroup { RootView() } }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ app: UIApplication,
                     didFinishLaunchingWithOptions opts: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        // push registration, third-party SDK init, etc.
        true
    }
}
```

Only add a delegate when you genuinely need UIKit callbacks (push tokens, background fetch, `UNUserNotificationCenter` delegate). For URL opening, prefer the `.onOpenURL` modifier instead. Otherwise skip the delegate entirely.

### Reacting to lifecycle with `scenePhase`

```swift
@main
struct FoodlogApp: App {
    @Environment(\.scenePhase) private var scenePhase
    @State private var store = Store()

    var body: some Scene {
        WindowGroup { RootView().environment(store) }
            .onChange(of: scenePhase) { _, phase in
                if phase == .background { store.persist() }
            }
    }
}
```

- Phases: `.active`, `.inactive`, `.background`. Persist on `.background`.
- `onChange(of:)` uses the two-parameter closure `{ oldValue, newValue in }` on iOS 17+. The zero/one-parameter forms are deprecated. If you don't need the old value, use `{ _, newValue in }` (there is also a zero-argument overload `{ }` for "just react", but pass the value you use).

---

## The Observation model (`@Observable`)

`@Observable` (from the `Observation` framework, auto-imported with SwiftUI) replaces `ObservableObject`. SwiftUI tracks **only the stored properties a view actually reads** during `body`, so re-renders are more precise than `@Published`.

```swift
import SwiftUI

@Observable
final class ProfileModel {
    var name: String = ""
    var isLoading = false

    // Not tracked: caches, derived scratch state, injected deps you never render.
    @ObservationIgnored private var cache: [String: Int] = [:]

    func load() { /* ... */ }
}
```

Rules:
- Use a `final class` (reference type; identity matters for shared state).
- Every non-ignored stored `var` is observable automatically. No `@Published`.
- `@ObservationIgnored` opts a property out of tracking. Use it for caches, tokens, and injected dependencies you never render. It's also the escape hatch for storing non-`Sendable` deps under `@MainActor`.
- Computed properties are tracked transitively — reading one registers the stored properties it touches.
- No protocol conformance is required; `@Observable` is a macro. Do **not** also conform to `ObservableObject`.

### Property-wrapper decision table

| Wrapper | Use when | Owns the value? |
|---|---|---|
| `@State private var x = Model()` | View (or `App`) creates & owns an `@Observable` instance or a value type | Yes |
| `@Bindable var model: Model` | You received an `@Observable` and need `$` bindings to its properties | No |
| `@Environment(Model.self) var model` | Read an `@Observable` injected upstream | No |
| `@Environment(\.dismiss) var dismiss` | Read a system environment value | No |
| `let item: Item` (plain) | Child just needs to display data, no mutation | No |
| `@Binding var text: String` | Child mutates a value the parent owns | No (parent does) |

Legacy equivalents you should **not** reach for on iOS 17+: `@StateObject` → `@State`, `@ObservedObject` → plain `let` or `@Bindable`, `@EnvironmentObject` → `@Environment(Type.self)`.

---

## `@State` — view-owned truth

```swift
struct CounterView: View {
    @State private var count = 0                 // value type
    @State private var model = ProfileModel()    // @Observable reference type

    var body: some View {
        VStack {
            Text("\(count)")
            Button("Inc") { count += 1 }
            TextField("Name", text: $model.name)  // $ projects a Binding
        }
    }
}
```

- `@State` **creates and owns**. It survives view re-instantiation (SwiftUI keeps the storage). Initialize inline.
- Always `private`. State is an implementation detail of the view.
- `$state` yields a `Binding`. For an `@Observable` held in `@State`, `$model.name` binds a property directly — no local `@Bindable` needed (that's only for environment reads; see below).
- With `@Observable`, use `@State` (not `@StateObject`) to own the model. `@StateObject` is for legacy `ObservableObject` only.

---

## `@Bindable` — bindings to an object you don't own

Use when a view **receives** an `@Observable` and needs to produce bindings (e.g. into a `TextField`, `Toggle`, `Picker`).

```swift
struct EditProfileView: View {
    @Bindable var model: ProfileModel   // passed in by the parent

    var body: some View {
        Form {
            TextField("Name", text: $model.name)
            Toggle("Loading", isOn: $model.isLoading)
        }
    }
}
```

- The parent owns the instance (via `@State` or environment). `@Bindable` just unlocks the `$` syntax on a passed-in reference.
- If you only **read** (no bindings), take a plain `let model: ProfileModel` instead.
- To bind a property of an `@Observable` read from `@Environment`, make a **local** `@Bindable var x = x` at the top of `body` (see the routing section).

---

## `@Environment` — dependency injection down the tree

Inject an `@Observable` once; read it anywhere below without threading it through every initializer.

```swift
// Inject (usually at App or a subtree root):
RootView().environment(session)          // session: @Observable

// Read:
struct HeaderView: View {
    @Environment(SessionModel.self) private var session
    var body: some View { Text(session.displayName) }
}
```

- `@Environment(Type.self)` reads by **type**. One instance per type in a given subtree wins (nearest ancestor).
- A non-optional read with no injected value is a **programmer error and causes a runtime failure** (purple runtime issue). If injection might be absent, read it as optional and handle `nil`:

```swift
@Environment(SessionModel.self) private var session: SessionModel?
```

- Use environment for genuinely cross-cutting dependencies (auth session, router, theme, a data store). Don't dump every model into the environment; pass narrow inputs explicitly where ownership is local.

### Custom environment values

**`@Entry` macro** (requires Xcode 16+ / Swift 6 compiler; **back-deploys to iOS 17** — it is a compile-time macro, not an OS feature):

```swift
struct Theme { var accent: Color = .blue }

extension EnvironmentValues {
    @Entry var theme: Theme = Theme()
}

// set:  ContentView().environment(\.theme, Theme(accent: .pink))
// read: @Environment(\.theme) private var theme
```

If you're on the Xcode 15 / Swift 5.9 toolchain, write the key manually (same runtime behavior):

```swift
private struct ThemeKey: EnvironmentKey {
    static let defaultValue = Theme()
}
extension EnvironmentValues {
    var theme: Theme {
        get { self[ThemeKey.self] }
        set { self[ThemeKey.self] = newValue }
    }
}
```

Useful built-in environment values: `\.dismiss`, `\.scenePhase`, `\.colorScheme`, `\.dynamicTypeSize`, `\.openURL`, `\.horizontalSizeClass`, `\.locale`.

```swift
struct SheetContent: View {
    @Environment(\.dismiss) private var dismiss
    var body: some View { Button("Done") { dismiss() } }
}
```

---

## One-way data flow

State lives in exactly one place; everything else derives from it.

- **Down:** pass immutable values (`let`), a `Binding` (child may write, parent owns), or an `@Observable` reference.
- **Up:** child calls a closure or a method on a model; it never reaches around the binding to mutate parent internals.

```swift
struct Todo: Identifiable, Hashable { let id = UUID(); var title: String; var done = false }

// Parent owns the list; child edits one row's value via a binding.
struct TodoList: View {
    @State private var model = TodoModel()
    var body: some View {
        List($model.items) { $item in       // binding to each element
            TodoRow(item: $item, onDelete: { model.delete(item) })
        }
    }
}

struct TodoRow: View {
    @Binding var item: Todo
    let onDelete: () -> Void                 // "up" via closure
    var body: some View {
        HStack {
            Toggle(item.title, isOn: $item.done)
            Button(role: .destructive, action: onDelete) { Image(systemName: "trash") }
        }
    }
}
```

- `List($collection)` / `ForEach($collection)` gives element bindings (`$item`). The collection must be a `Binding` to a `RandomAccessCollection` of `Identifiable` elements whose element you can write back (e.g. an array).
- Prefer callbacks (`onDelete`) over handing a child write access to state it shouldn't understand.

---

## Navigation: `NavigationStack` + `navigationDestination`

`NavigationStack` replaces `NavigationView` for push/pop stacks. Two ergonomic modes: value-driven links, and a programmatic path.

### Value-driven (simple, no shared path)

```swift
struct Item: Identifiable, Hashable { let id: Int; let title: String }

struct ItemsView: View {
    let items: [Item]
    var body: some View {
        NavigationStack {
            List(items) { item in
                NavigationLink(item.title, value: item)     // value, not a destination view
            }
            .navigationDestination(for: Item.self) { item in // one place maps type -> view
                ItemDetailView(item: item)
            }
            .navigationTitle("Items")
        }
    }
}
```

- `NavigationLink(_, value:)` pushes a **value**; `.navigationDestination(for:)` maps a type to its destination view. This decouples the link site from the destination and enables deep-linking / state restoration.
- Destination values must be `Hashable`. Register multiple `.navigationDestination(for:)` modifiers for different types.
- **Do not** attach `.navigationDestination` inside a lazy container's row (per-row in a `List`/`LazyVStack`) — attach it once to the stack's content. Per-row placement means it isn't registered when rows are offscreen.

### Programmatic path (recommended for anything non-trivial)

```swift
@Observable
final class ItemsModel {
    var path: [Item] = []                    // typed path; or NavigationPath for mixed types
    func open(_ item: Item) { path.append(item) }
    func popToRoot() { path.removeAll() }
}

struct ItemsView: View {
    @State private var model = ItemsModel()
    let items: [Item]
    var body: some View {
        NavigationStack(path: $model.path) {  // $ works on @State directly
            List(items) { item in
                Button(item.title) { model.open(item) }
            }
            .navigationDestination(for: Item.self) { ItemDetailView(item: $0) }
        }
    }
}
```

- `NavigationStack(path:)` binds a `[SomeHashable]` or `NavigationPath`. Mutating it navigates: `append` pushes, `removeLast` pops, `removeAll`/reassign pops to root.
- Use a **typed array** (`[Item]`) when every screen pushes the same type — you get direct, inspectable access. Use `NavigationPath` for heterogeneous destinations.

### `NavigationPath` for mixed destination types

```swift
@Observable
final class Router {
    var path = NavigationPath()
    func push<V: Hashable>(_ v: V) { path.append(v) }
    func pop()        { if !path.isEmpty { path.removeLast() } }
    func popToRoot()  { path = NavigationPath() }
}
```

- `NavigationPath` is a type-erased stack that holds any `Hashable`. Register one `.navigationDestination(for:)` per type you push.
- `path.count`, `path.isEmpty`, `path.removeLast(k)` are available. Elements are not directly readable (type-erased) — that's the tradeoff for mixing types.

### Sharing the router via environment

Programmatic navigation shines when a deep child needs to navigate. Inject the router and bind to its `path`:

```swift
@main
struct MyApp: App {
    @State private var router = Router()
    var body: some Scene {
        WindowGroup { RootView().environment(router) }
    }
}

struct RootView: View {
    @Environment(Router.self) private var router
    var body: some View {
        @Bindable var router = router                    // needed for $router.path
        NavigationStack(path: $router.path) {
            HomeView()
                .navigationDestination(for: Item.self) { ItemDetailView(item: $0) }
                .navigationDestination(for: SettingsRoute.self) { SettingsView(route: $0) }
        }
    }
}

struct DeepChild: View {
    @Environment(Router.self) private var router
    var body: some View {
        Button("Go home") { router.popToRoot() }         // navigate from anywhere
    }
}
```

- **Key pattern:** to bind to a property of an `@Environment`-read `@Observable`, declare a local `@Bindable var x = x` at the top of `body`, then use `$x.path`. You can't write `$router.path` directly on the environment value. (This step is *not* needed for `@State`-held models — those project bindings directly.)

---

## Modal presentation: sheets, full-screen covers, alerts

Enum-driven modals keep presentation state in one optional and avoid a swarm of booleans.

```swift
enum Sheet: Identifiable {
    case settings
    case editItem(Item)
    var id: String {
        switch self {
        case .settings: "settings"
        case .editItem(let i): "edit-\(i.id)"
        }
    }
}

struct HomeView: View {
    @State private var sheet: Sheet?
    var body: some View {
        List { /* ... */ }
            .toolbar {
                Button("Settings") { sheet = .settings }
            }
            .sheet(item: $sheet) { sheet in
                switch sheet {
                case .settings:           SettingsView()
                case .editItem(let item): EditItemView(item: item)
                }
            }
    }
}
```

- `.sheet(item:)` presents when the optional is non-`nil`, dismisses when set back to `nil` (or the user swipes). The item must be `Identifiable`.
- `.sheet(isPresented:)` for a single boolean-driven sheet; `.fullScreenCover(...)` for modals that shouldn't be swipe-dismissed (onboarding, camera).
- Sheet sizing: `.presentationDetents([.medium, .large])`, `.presentationDragIndicator(.visible)`.
- Alerts/confirmations: `.alert(_, isPresented:)`, `.confirmationDialog(_, isPresented:)`; both also have `presenting:`-item overloads. Drive with a bool or optional item, same as sheets.

---

## Lightweight MVVM (without over-engineering)

Not every view needs a view model. Use one when a screen has real logic: async loading, validation, non-trivial derived state, or side effects. For a static list or a form bound straight to a model, skip the VM.

```swift
@Observable
@MainActor
final class FeedModel {
    private(set) var items: [Post] = []
    private(set) var phase: Phase = .idle

    enum Phase: Equatable { case idle, loading, loaded, failed(String) }

    private let service: FeedService
    init(service: FeedService = .live) { self.service = service }

    func load() async {
        phase = .loading
        do {
            items = try await service.fetch()
            phase = .loaded
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }
}

struct FeedView: View {
    @State private var model = FeedModel()
    var body: some View {
        List(model.items) { PostRow(post: $0) }
            .overlay { if model.phase == .loading { ProgressView() } }
            .task { await model.load() }                     // runs on appear; auto-cancelled on disappear
            .refreshable { await model.load() }
    }
}
```

Conventions:
- Annotate the model `@MainActor` so all state mutation is on the main thread. `@Observable @MainActor final class ...` is the idiomatic combo for a UI model.
- Expose read-only state with `private(set)`; mutate only through methods. This enforces one-way flow.
- Model a screen's state as an **enum phase** (`idle/loading/loaded/failed`) rather than parallel `isLoading`/`error`/`data` booleans that can contradict.
- Inject dependencies through `init` (default to a `.live` value) so the model is testable and previewable with a fake service.
- `.task { }` ties async work to view lifetime — cancelled automatically on disappear. Prefer it over `onAppear { Task { ... } }`. Use `.task(id:)` to restart when an input changes.
- Keep networking/persistence in a service/repository type, not in the view model. The VM orchestrates; it doesn't run `URLSession` inline.

When the VM would be trivial, put state directly in the view with `@State` — that *is* idiomatic SwiftUI, not a shortcut to apologize for.

---

## View decomposition

- Split when `body` gets long, when a chunk repeats, or when a subtree has its own state. Small views also give SwiftUI finer invalidation.
- Extract into a **new `View` struct** (preferred) rather than a `@ViewBuilder` computed property when the piece takes inputs or holds state. A computed `var someSection: some View` is fine for a private, input-free slice.
- Pass the **minimum** each child needs: a plain `let` for display, a `@Binding` for editable fields, an `@Observable` only if the child owns interaction with it.
- Avoid "massive body" and avoid passing whole models down when a child needs one field.

```swift
struct ProfileView: View {
    @Bindable var model: ProfileModel
    var body: some View {
        Form {
            AvatarHeader(name: model.name, imageURL: model.avatarURL)  // display-only inputs
            Section("Details") {
                TextField("Name", text: $model.name)                   // binding where edited
            }
        }
    }
}

struct AvatarHeader: View {
    let name: String
    let imageURL: URL?
    var body: some View {
        HStack {
            AsyncImage(url: imageURL) { $0.resizable() } placeholder: { Color.gray }
                .frame(width: 44, height: 44)
                .clipShape(.circle)
            Text(name).font(.headline)
        }
    }
}
```

Performance notes:
- Reading fewer properties of an `@Observable` in a view = fewer re-renders. Decomposition naturally narrows what each view observes.
- Use `LazyVStack`/`LazyHStack`/`List` for large collections; `List` recycles rows. Prefer `List` over `ScrollView { LazyVStack }` unless you need custom layout.
- Give `ForEach` stable `Identifiable` IDs; avoid `id: \.self` on mutable value types.

---

## When to reach for a Router / Coordinator

Start simple. Escalate only when the pain is real.

1. **Local `NavigationLink(value:)` + `navigationDestination`** — a screen pushes a couple of detail views, nobody else drives it. Most screens. No router.
2. **A per-feature `@Observable` model holding `path`** — the screen navigates programmatically (after a save, from a menu action) but navigation stays contained to that feature. Put `var path` on that feature's model.
3. **A shared `Router` in the environment** — reach for this when:
   - Deep children must trigger navigation (a cell three levels down opens a detail, or "go to root/checkout").
   - Deep links / notifications must jump to an arbitrary screen (mutate `router.path` from the URL handler).
   - Tabs need coordinated or restorable navigation state.
   - You want to unit-test navigation logic in isolation.

```swift
// Deep link -> programmatic navigation via the shared router.
.onOpenURL { url in
    if let route = Route(url: url) { router.path.append(route) }
}
```

- A "coordinator" in SwiftUI is just this: an `@Observable` router owning path(s) and exposing intent methods (`showCheckout()`, `popToRoot()`), injected via environment. You do **not** need UIKit-style coordinator-with-delegate protocols — that's over-engineering here.
- One router per `NavigationStack`. With a `TabView`, give each tab its own path (a router with `var paths: [Tab: NavigationPath]`, or a small router per tab) so switching tabs doesn't clobber another tab's stack.
- Don't introduce a router "for cleanliness" on a two-screen app. It adds indirection with no payoff. Add it the first time a child needs to navigate something it doesn't own.

---

## Quick anti-patterns → fixes

- `@StateObject var vm = VM()` where `VM` is `@Observable` → `@State var vm = VM()`.
- `@ObservedObject`/`@EnvironmentObject` on iOS 17+ → `@Bindable`/`let` and `@Environment(Type.self)`.
- Singletons for shared UI state → own at `App` with `@State`, inject via `.environment(_:)`.
- `NavigationView` → `NavigationStack` (and `NavigationSplitView` for iPad/multi-column).
- Parallel `isLoading`/`error`/`data` flags → a single `enum Phase`.
- `onAppear { Task { await load() } }` → `.task { await load() }` (structured cancellation).
- `onChange(of:) { newValue in }` (deprecated arity) → `onChange(of:) { _, newValue in }`.
- Business logic / `URLSession` inside `body` → move to an `@Observable @MainActor` model + a service type.
- Passing a whole model to a child that reads one field → pass the field (`let`) or a `@Binding`.
- Marking `@Entry` / custom `EnvironmentKey` as needing iOS 18 → `@Entry` is a compiler feature that back-deploys to iOS 17.

---

## Minimal end-to-end skeleton

```swift
import SwiftUI

// MARK: Domain
struct Item: Identifiable, Hashable { let id: UUID; var title: String }

// MARK: Router (shared)
@Observable @MainActor
final class Router {
    var path = NavigationPath()
    func open(_ item: Item) { path.append(item) }
    func popToRoot() { path = NavigationPath() }
}

// MARK: Feature model
@Observable @MainActor
final class ItemsModel {
    private(set) var items: [Item] = []
    func load() async { items = [Item(id: UUID(), title: "First")] }
}

// MARK: App
@main
struct DemoApp: App {
    @State private var router = Router()
    var body: some Scene {
        WindowGroup {
            RootView().environment(router)
        }
    }
}

// MARK: Root + stack
struct RootView: View {
    @Environment(Router.self) private var router
    var body: some View {
        @Bindable var router = router
        NavigationStack(path: $router.path) {
            ItemsView()
                .navigationDestination(for: Item.self) { DetailView(item: $0) }
        }
    }
}

struct ItemsView: View {
    @State private var model = ItemsModel()
    @Environment(Router.self) private var router
    var body: some View {
        List(model.items) { item in
            Button(item.title) { router.open(item) }
        }
        .navigationTitle("Items")
        .task { await model.load() }
    }
}

struct DetailView: View {
    let item: Item
    @Environment(Router.self) private var router
    var body: some View {
        VStack(spacing: 16) {
            Text(item.title).font(.title)
            Button("Back to root") { router.popToRoot() }
        }
    }
}
```

This is a complete, idiomatic iOS 17+ app skeleton: `@main` app owning a shared `@Observable` router, a `NavigationStack` bound (via a local `@Bindable`) to the router's path, a `@MainActor` feature model loaded via `.task`, value-based navigation, and a deep child navigating through the injected router. Verified to typecheck against `-target arm64-apple-ios17.0` on the Swift 6 toolchain.
