<!-- keywords: ios human interface guidelines, hig, swiftui navigation, navigationstack type-safe routing, tab bar vs stack vs sheet, empty loading error states, contentunavailableview, sf symbols rendering modes, symbol effects ios 17 vs 18, haptics sensoryfeedback, accessibility voiceover labels, dynamic type anylayout, reduced motion, color contrast wcag, 44pt touch target contentshape, color blind safe ux, presentation detents, swiftui form controls, searchable scopes, accessibility checklist -->

# iOS HIG + Native UX Cheatsheet (iOS 17+ / SwiftUI)

Rules for building iPhone apps that feel native and premium. Target iOS 17+, Swift 5.9+, SwiftUI-first, zero third-party deps. Snippets are idiomatic and compile on an iOS 17 deployment target unless a newer minimum is called out. When SwiftUI can't do it, drop to UIKit via `UIViewRepresentable`/`UIViewControllerRepresentable` — noted where relevant.

## Core principles (apply to every screen)

- **Prefer system components.** Native controls inherit Dynamic Type, VoiceOver, Reduce Motion, RTL, and dark mode for free. A custom control means you re-implement all of that or ship an inaccessible app.
- **Use semantic colors** (`Color.primary`, `.secondary`, `Color(.systemBackground)`, `.tint`), never hardcoded hex, so light/dark/increase-contrast modes work automatically.
- **Respect safe areas.** Let system spacing (`.padding()` with no argument) and `Spacer()` drive layout; avoid magic pixel numbers.
- **Use text styles** (`.font(.body)`, `.headline`, …), which scale with Dynamic Type. Fixed point sizes don't — avoid them for user-facing text.
- **One primary action per screen.** Everything else is secondary/tertiary in visual weight.
- **Latency budget:** any action >100ms needs feedback (spinner, skeleton, or optimistic UI). A measurable task >1s should show determinate progress.

## Navigation: pick the right container

Decision order — do NOT default to a sheet or a tab bar:

- **Tab bar (`TabView`)**: 2–5 top-level, peer sections the user switches between freely (Home, Search, Profile). Never for a linear flow or steps. Each tab persists its own navigation stack. Don't hide the tab bar mid-flow; if a screen needs full focus, present it modally instead.
- **Navigation stack (`NavigationStack`)**: drill-down into hierarchical content (list → detail → sub-detail). Push when the destination is a child of the current context and the user will want the back button + swipe-back.
- **Sheet (`.sheet`)**: a self-contained, interruptive task the user completes and dismisses (compose, filter, quick edit, share). Use detents for lightweight tasks. A sheet keeps the parent visible behind it — signals "you'll come right back."
- **Full-screen cover (`.fullScreenCover`)**: onboarding, media playback, camera, or a task that demands total focus with no parent context. Use sparingly — it breaks the "come right back" mental model.
- **Alert / confirmationDialog**: a decision, not a task. `alert` = 1–2 choices needing acknowledgment. `confirmationDialog` = a short list of actions (esp. destructive) anchored to what the user tapped.

Rule of thumb: if the user can be "in the middle of it" and navigate away, it's a stack. If they must finish or cancel, it's a sheet/cover. If it's a yes/no, it's an alert.

### NavigationStack with type-safe routing

Prefer value-based navigation over `NavigationLink(destination:)` — it enables deep linking, programmatic pop-to-root, and state restoration.

```swift
enum Route: Hashable {
    case detail(itemID: UUID)
    case settings
}

struct RootView: View {
    @State private var path: [Route] = []
    let store: ItemStore

    var body: some View {
        NavigationStack(path: $path) {
            List(store.items) { item in
                NavigationLink(value: Route.detail(itemID: item.id)) {
                    ItemRow(item: item)
                }
            }
            .navigationTitle("Items")
            .navigationDestination(for: Route.self) { route in
                switch route {
                case .detail(let id): DetailView(itemID: id)
                case .settings:       SettingsView()
                }
            }
        }
    }

    func popToRoot() { path.removeAll() }
    func openSettings() { path.append(.settings) }
}
```

- `.navigationTitle` goes on the content, not the stack. Large title for top-level roots (default), `.inline` for pushed detail screens where a large title wastes space:

```swift
.navigationBarTitleDisplayMode(.inline)
```

- Toolbar placement is semantic, not positional — the system positions it correctly for the platform + RTL:

```swift
.toolbar {
    ToolbarItem(placement: .topBarLeading) { Button("Cancel") { dismiss() } }
    ToolbarItem(placement: .primaryAction) { Button("Save", action: save) }
    ToolbarItemGroup(placement: .bottomBar) { EditButton(); Spacer(); addButton }
}
```

(`dismiss` comes from `@Environment(\.dismiss) private var dismiss`.)

### TabView

```swift
struct AppTabs: View {
    @State private var selection: Tab = .home
    enum Tab: Hashable { case home, search, profile }

    var body: some View {
        TabView(selection: $selection) {
            HomeView()
                .tabItem { Label("Home", systemImage: "house") }
                .tag(Tab.home)
            SearchView()
                .tabItem { Label("Search", systemImage: "magnifyingglass") }
                .tag(Tab.search)
            ProfileView()
                .tabItem { Label("Profile", systemImage: "person.crop.circle") }
                .tag(Tab.profile)
        }
    }
}
```

- Give each tab its own `NavigationStack`. Tapping the active tab again pops that tab to root automatically when the tab hosts a `NavigationStack`.
- Badge only actionable counts: `.badge(unreadCount)`. Don't badge vanity metrics.
- Provide an outline SF Symbol for the tab label; the system fills the selected tab automatically, so pick a symbol that has a filled counterpart (`house`/`house.fill`).

### Sheets and detents

```swift
.sheet(isPresented: $showFilters) {
    FilterView()
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
        .presentationBackgroundInteraction(.enabled(upThrough: .medium))
        .presentationContentInteraction(.scrolls)
}
```

- `.medium` for a quick task the user glances at without losing context; `.large` for content-heavy tasks. Offer both when the task can grow.
- `presentationBackgroundInteraction(.enabled(upThrough:))` (iOS 16.4+) lets the user interact with the parent while a small sheet is up (Maps-style) — a premium touch.
- Always provide a Cancel/Done control or a drag indicator so dismissal is discoverable. For a form sheet, put Cancel `.topBarLeading` and the confirming action `.primaryAction`.
- Custom fixed height: `.presentationDetents([.height(320)])`.

## Empty, loading, and error states

Every data-driven screen has **four** states: loading, loaded, empty, and error. Design all four. Never show a blank screen while loading, and never leave the user staring at nothing when a fetch returns zero results.

### Empty state — ContentUnavailableView (iOS 17+)

```swift
ContentUnavailableView {
    Label("No Bookmarks", systemImage: "bookmark")
} description: {
    Text("Bookmarks you save will appear here.")
} actions: {
    Button("Browse Articles") { showBrowse = true }
        .buttonStyle(.borderedProminent)
}
```

- Built-in search empty state:

```swift
ContentUnavailableView.search(text: query)   // "No Results for '…'"
```

- An empty state must (1) explain what goes here and (2) offer the action that fills it. Never just "No data."

### Loading state — skeletons over spinners for content

For a **known layout** (list/grid), show a redacted skeleton so the page doesn't jump when data arrives:

```swift
if isLoading {
    List(0..<8, id: \.self) { _ in ItemRow(item: .placeholder) }
        .redacted(reason: .placeholder)
        .allowsHitTesting(false)
} else {
    List(items) { ItemRow(item: $0) }
}
```

- For an **indeterminate wait** with no layout yet, a centered `ProgressView()` is fine. For a measurable task use determinate: `ProgressView(value: fraction)` (fraction in 0...1).
- Debounce spinners: if the fetch usually finishes <200ms, delay showing the spinner so it doesn't flash. For user-initiated mutations, optimistic UI (show the result immediately, reconcile on failure) beats any spinner.

### Error state — recoverable and specific

```swift
ContentUnavailableView {
    Label("Couldn’t Load", systemImage: "wifi.slash")
} description: {
    Text("Check your connection and try again.")
} actions: {
    Button("Retry") { Task { await load() } }
        .buttonStyle(.borderedProminent)
}
```

- Errors must be actionable: say what happened in plain language and give a Retry (or a path forward). Never surface raw error codes or stack traces to users.
- Transient/inline errors (a failed row action) → a brief non-blocking banner or an alert with Retry. Fatal errors that block the whole screen → full `ContentUnavailableView`.
- Validation errors belong next to the field, shown on submit or on blur — not in an alert.

### State machine pattern

```swift
enum LoadState<T> { case loading, loaded(T), empty, failed(Error) }

@ViewBuilder
func content(for state: LoadState<[Item]>) -> some View {
    switch state {
    case .loading:        LoadingList()
    case .loaded(let x):  ItemList(items: x)
    case .empty:          EmptyItemsView()
    case .failed(let e):  ErrorView(error: e, retry: { Task { await load() } })
    }
}
```

## Standard controls — use these, styled, before building custom

- **Button roles** carry meaning and system styling. Destructive is red; cancel is emphasized in dialogs:

```swift
Button("Delete", role: .destructive) { delete() }
Button("Cancel", role: .cancel) { }
```

- **Button styles**, in descending emphasis: `.borderedProminent` (the one primary action), `.bordered` (secondary), `.plain`/`.borderless` (tertiary). Recolor with `.tint(_:)`, not a custom background.
- **Toggle** for a binary on/off setting (renders as a switch). Use `.toggleStyle(.button)` for a selectable chip.
- **Picker**: `.menu` for a compact inline choice, `.segmented` for 2–5 mutually exclusive options visible at once, `.navigationLink` inside a `Form` to push a selection list, `.wheel` only for continuous ranges (dates/times).
- **Stepper** for small discrete numeric adjustments; **Slider** for continuous ranges (give it min/max labels).
- **Menu** for a compact set of secondary actions on a control; **contextMenu** for actions on an item via long-press. Always mirror context-menu actions as swipe actions or a visible button — context menus are undiscoverable alone.
- **Forms and Settings**: use `Form { Section { … } }`. It provides grouped-inset styling, correct row insets, and header/footer text for free.

```swift
enum Theme: String, CaseIterable, Identifiable {
    case system, light, dark
    var id: Self { self }
    var title: String { rawValue.capitalized }
}

Form {
    Section("Account") {
        TextField("Name", text: $name)
        Toggle("Notifications", isOn: $notify)
    }
    Section {
        Picker("Theme", selection: $theme) {
            ForEach(Theme.allCases) { Text($0.title).tag($0) }
        }
    } footer: {
        Text("Applies across all your devices.")
    }
}
```

- **Search**: `.searchable(text:)` on the content inside a `NavigationStack`. Add `.searchScopes` for filters and `.searchSuggestions` for autocomplete. Don't hand-roll a search bar.

```swift
.searchable(text: $query, prompt: "Search items")
.searchScopes($scope) {
    Text("All").tag(Scope.all)
    Text("Favorites").tag(Scope.favorites)
}
```

- **Swipe actions** for list-row actions; keep leading for a positive/frequent action and trailing for destructive:

```swift
.swipeActions(edge: .trailing, allowsFullSwipe: true) {
    Button(role: .destructive) { delete(item) } label: { Label("Delete", systemImage: "trash") }
}
.swipeActions(edge: .leading) {
    Button { archive(item) } label: { Label("Archive", systemImage: "archivebox") }.tint(.orange)
}
```

- **Text input hygiene** — set keyboard, content type (enables autofill), autocorrect, and capitalization. This is a premium/accessibility signal:

```swift
TextField("Email", text: $email)
    .keyboardType(.emailAddress)
    .textContentType(.emailAddress)
    .textInputAutocapitalization(.never)
    .autocorrectionDisabled()
    .submitLabel(.next)
```

- **Pull-to-refresh**: `.refreshable { await reload() }` on a `List`/`ScrollView`. The async closure holds the spinner until it returns.
- **Confirm destructive actions** with `confirmationDialog` anchored to the trigger, not a generic alert:

```swift
.confirmationDialog("Delete this item?", isPresented: $confirmDelete, titleVisibility: .visible) {
    Button("Delete", role: .destructive) { delete() }
    Button("Cancel", role: .cancel) { }
}
```

## SF Symbols — the icon system

- Use SF Symbols for all glyphs. They align to text baselines, scale with Dynamic Type, adapt weight to the surrounding font, and mirror in RTL when appropriate. Never ship a bitmap where a symbol exists.
- Size symbols by attaching them to text or via `.imageScale` / `.font`, so they track Dynamic Type:

```swift
Label("Favorites", systemImage: "star.fill")
Image(systemName: "gear").imageScale(.large)
Image(systemName: "bolt.fill").font(.title2)   // scales with type
```

- Use `.fill` for selected/active, outline for inactive:

```swift
Image(systemName: "heart").symbolVariant(isLiked ? .fill : .none)
```

- **Rendering modes** — pick deliberately:
  - `.monochrome` (default): single tint.
  - `.hierarchical`: one color, multiple opacities — adds depth cheaply. `Image(...).symbolRenderingMode(.hierarchical).foregroundStyle(.blue)`.
  - `.palette`: you supply 2–3 colors. `.symbolRenderingMode(.palette).foregroundStyle(.pink, .gray)`.
  - `.multicolor`: uses the symbol's intrinsic colors. `Image(systemName: "flame.fill").symbolRenderingMode(.multicolor)`.
- **Symbol animations** make interactions feel alive — use on state change, not decoratively:

```swift
// One-shot on value change (iOS 17+)
Image(systemName: "bell.fill")
    .symbolEffect(.bounce, value: notificationCount)

// Continuous while active — .pulse/.variableColor are the iOS 17 indefinite effects
Image(systemName: "arrow.triangle.2.circlepath")
    .symbolEffect(.variableColor.iterative, isActive: isSyncing)

// Morph between two symbols (iOS 17+)
Image(systemName: isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill")
    .contentTransition(.symbolEffect(.replace))
```

> Availability note: `.pulse`, `.variableColor`, `.bounce`, `.scale`, and `.replace` are iOS 17+. `.rotate`, `.wiggle`, and `.breathe` are **iOS 18+** — gate them with `if #available(iOS 18, *)` or don't use them on an iOS 17 target.

- Symbol names are versioned — an unknown name renders blank. Prefer symbols available since iOS 15–16 for broad support and gate newer ones. Provide an `accessibilityLabel` when a symbol conveys meaning with no adjacent text.

## Haptics — feedback, not decoration

- SwiftUI-native (iOS 17+): `.sensoryFeedback` fires when a trigger value changes. Prefer this over `UIFeedbackGenerator` in SwiftUI.

```swift
.sensoryFeedback(.success, trigger: didSaveSucceed)
.sensoryFeedback(.impact(weight: .light), trigger: selectedTab)
.sensoryFeedback(.selection, trigger: pickerValue)
```

- Semantic feedback (map intent → feedback), fired the moment the outcome is known:
  - `.success` — a task completed (payment sent, item saved).
  - `.warning` — recoverable problem the user should notice.
  - `.error` — action failed.
  - `.selection` — moving through discrete values (picker, segmented control).
  - `.impact(weight:)` — a UI element hit a boundary, snapped, or toggled. `.light` for subtle, `.heavy` for consequential.
- UIKit fallback when you need imperative control:

```swift
let generator = UINotificationFeedbackGenerator()
generator.prepare()               // call ~before the event to reduce latency
generator.notificationOccurred(.success)
```

- Rules: haptics **confirm**, they don't announce. One haptic per discrete event — never on continuous scroll or every keystroke. Don't fire on view appear. Always pair with a visual change; a haptic alone is inaccessible. The system suppresses haptics in some states (e.g. Low Power Mode) — never try to force them.

## Layout, motion, and polish that reads as "premium"

- **Consistent spacing scale**: stick to multiples of 4/8pt. Use default `.padding()` for edge insets; set `VStack(spacing:)` deliberately.
- **Animate state, not layout thrash** — wrap the state change; use a spring for interactive UI:

```swift
withAnimation(.snappy) { isExpanded.toggle() }   // .snappy / .smooth / .bouncy (iOS 17)
.animation(.default, value: items.count)          // implicit, scoped to a value
```

- **`matchedGeometryEffect`** for hero transitions between two views in the same namespace — the "expand card into detail" effect.
- **Content transitions** for text/number changes: `.contentTransition(.numericText())` on a counter animates digit rolls.
- **Materials** for depth: `.background(.regularMaterial)` / `.thinMaterial` for translucent bars and cards. Don't stack opaque cards on opaque backgrounds.
- **Respect the keyboard**: SwiftUI auto-avoids it. For a Done button, add a keyboard toolbar:

```swift
.toolbar { ToolbarItemGroup(placement: .keyboard) { Spacer(); Button("Done") { hideKeyboard() } } }
```

- **Onboarding/permissions**: request each system permission (notifications, location, camera) *in context*, right when the feature needs it, after a one-line rationale — never a wall of prompts on launch. Set the matching `NS…UsageDescription` key in Info.plist; a missing string crashes the app when the API is called.

## Accessibility checklist (ship-blocking — verify every item)

Accessibility is not optional polish; a failing item here is a bug. Test with VoiceOver on, Dynamic Type at max, and the Accessibility Inspector.

### VoiceOver labels

- Every actionable element needs a clear label. System controls with text get one free. Icon-only buttons do NOT — add one:

```swift
Button(action: share) { Image(systemName: "square.and.arrow.up") }
    .accessibilityLabel("Share")
```

- Label describes *what it does*, not the icon: "Share", not "Square with arrow." No "button" in the label — the trait already says that.
- Add a hint only when the result isn't obvious from the label: `.accessibilityHint("Opens the share sheet")`.
- Combine a composite row into one announcement so VoiceOver doesn't read 5 fragments:

```swift
HStack { Image(systemName: "star.fill"); Text(title); Spacer(); Text(subtitle) }
    .accessibilityElement(children: .combine)
    .accessibilityLabel("\(title), \(subtitle)")
```

- Expose state via traits and value, not by baking it into the label:

```swift
.accessibilityAddTraits(isSelected ? .isSelected : [])
.accessibilityValue(isOn ? "On" : "Off")
```

- Decorative images: hide them — `Image("divider").accessibilityHidden(true)`.
- Announce important async changes: `AccessibilityNotification.Announcement("3 new messages").post()`.
- Group and order: `.accessibilityElement(children: .contain)` on containers; fix reading order with `.accessibilitySortPriority` only when the visual order misleads.

### Dynamic Type

- Use text styles (`.body`, `.headline`, `.caption`, …), never fixed `.font(.system(size: 17))`, for user-facing text.
- Support the accessibility (AX) sizes; test at `AX5`. If a fixed HStack of label+control overflows at large sizes, switch to a vertical layout dynamically:

```swift
@Environment(\.dynamicTypeSize) private var typeSize

var body: some View {
    let layout = typeSize.isAccessibilitySize
        ? AnyLayout(VStackLayout(alignment: .leading))
        : AnyLayout(HStackLayout())
    layout { Text("Label"); Spacer(); control }
}
```

- Never truncate essential text at large sizes; allow wrapping (`.fixedSize(horizontal: false, vertical: true)` where needed). Only cap type scaling for genuinely space-critical UI, and then set a *ceiling* (e.g. `.dynamicTypeSize(...DynamicTypeSize.accessibility3)`), never a small cap like `.large`.
- Custom fonts must scale: `.font(.custom("Inter", size: 17, relativeTo: .body))`.

### Reduce Motion

- Check the environment and swap large motion (parallax, spins, big scale/hero transitions) for a cross-fade:

```swift
@Environment(\.accessibilityReduceMotion) private var reduceMotion

withAnimation(reduceMotion ? nil : .bouncy) { expand() }
```

- `.transition(reduceMotion ? .opacity : .scale.combined(with: .opacity))`.
- Autoplaying/looping animations must stop or become static under Reduce Motion. Gate continuous symbol effects on `!reduceMotion`.

### Color contrast + don't rely on color alone (color-blind safe)

- Text contrast ≥ **4.5:1** (normal), ≥ **3:1** (large ≥17pt bold / ≥20pt). Interactive and meaningful graphical elements ≥ **3:1**. Verify with the Accessibility Inspector's contrast checker.
- Use semantic colors so **Increase Contrast** and dark mode adjust automatically. Test with Settings → Accessibility → Display & Text Size → Increase Contrast on.
- **Never encode meaning in color alone** (red = error, green = success). Pair color with an icon, text, or shape — the single most common color-blind failure:

```swift
Label(status.title, systemImage: status.symbol)   // icon + text carry the meaning
    .foregroundStyle(status.color)                 // color is redundant reinforcement
// status.symbol: .success → "checkmark.circle.fill", .error → "xmark.octagon.fill"
```

- Charts: differentiate series by shape, pattern, direct labels, or position — not hue alone. In Swift Charts, add `.symbol(by:)` alongside `.foregroundStyle(by:)`.
- Don't override the user's Bold Text or accent-color settings.

### Touch targets ≥ 44×44pt

- Every tappable element must present a **≥44×44pt** hit area, even if the glyph is smaller. Enlarge the hit area without enlarging the visual:

```swift
Button(action: close) { Image(systemName: "xmark") }
    .frame(minWidth: 44, minHeight: 44)
    .contentShape(Rectangle())      // whole frame is tappable, not just the glyph
```

- `.contentShape(Rectangle())` is required whenever padding or empty space inside a tappable view should register taps.
- Keep ~8pt between adjacent targets so fingers don't overlap them. List rows are ≥44pt tall by default — don't shrink them.

### Additional a11y wins

- **Focus / Full Keyboard Access**: ensure a logical focus order; use `@FocusState` to move between fields on submit.
- **Voice Control**: labels double as spoken command names — clear labels serve VoiceOver and Voice Control at once.
- **Localization / RTL**: use `Text` with string keys, `.multilineTextAlignment(.leading)` (not `.left`), and leading/trailing (not left/right) so RTL mirrors correctly. Semantic symbols and chevrons mirror automatically.
- **Expose swipe/context actions to VoiceOver** so they aren't invisible to it:

```swift
.accessibilityAction(named: "Delete") { delete(item) }
```

- **Reduce Transparency**: system materials fall back to solid automatically — one more reason to use `.regularMaterial` over a custom blur.

## Fast pre-ship audit (run through this list)

- [ ] All four states (loading/empty/error/loaded) designed for every data screen.
- [ ] Navigation container matches the task (stack vs sheet vs tab vs alert).
- [ ] Every icon-only control has an `accessibilityLabel`.
- [ ] Screen is usable at Dynamic Type `AX5` — no clipped or overlapping text.
- [ ] VoiceOver reads each screen in a sensible order; composite rows are combined.
- [ ] No meaning conveyed by color alone; text contrast ≥ 4.5:1.
- [ ] Every tap target ≥ 44×44pt with correct `contentShape`.
- [ ] Motion degrades gracefully under Reduce Motion; symbol effects gated by availability and `reduceMotion`.
- [ ] Haptics only on discrete, meaningful events; paired with visuals.
- [ ] Semantic colors throughout; works in light, dark, and Increase Contrast.
- [ ] Destructive actions use `role: .destructive` and confirm before acting.
- [ ] Permissions requested in context with a rationale; `NS…UsageDescription` strings present.
