<!-- keywords: ios testing, xctest, swift testing, @test, #expect, #require, unit tests, xcode previews, snapshot testing, ui tests, xcuitest, testable architecture, dependency injection, mvvm testing, @observable testing, async test, confirmation, parameterized tests, test strategy, code coverage, urlprotocol mock, test doubles, minimum viable tests, what to test first, swiftui testing, test plan, dynamictypesize preview -->

# Testing & Quality for a Small iOS App

Target: **iOS 17+ deployment / Swift 5.9+**, built with **Xcode 16 or later** (Swift Testing ships in-box; use `OS=latest` in CI so the simulator version tracks your toolchain). Zero third-party deps — everything here is Apple first-party. This is a pragmatic reference for an AI building a prototype: maximize confidence per line of test code, avoid brittle tests that break on every UI tweak.

---

## TL;DR strategy for a prototype

- **Don't chase coverage.** Test the code that would be embarrassing or expensive if wrong: money math, date logic, parsing, persistence round-trips, state machines, reducers/view models.
- **Skip testing SwiftUI view bodies.** Use **Xcode Previews** as your visual feedback loop instead — they are faster than any UI test.
- **Make logic testable by moving it out of views** into plain `struct`/`class` types with injected dependencies. If it's testable, it's usually also better architected.
- **One happy-path UI test** through the primary flow catches wiring/regression bugs that unit tests can't. More than a handful of UI tests is usually a net negative for a prototype (slow, flaky).
- **Prefer Swift Testing (`@Test`)** for new unit tests. Keep XCTest for **UI tests** (`XCUIApplication`) and **performance tests** (`measure`) — those APIs are XCTest-only.

Priority order to write tests, highest first:
1. Pure logic with branches (pricing, validation, formatting, sorting/filtering).
2. View model / reducer state transitions (given input → expected observable state).
3. Persistence & serialization round-trips (Codable, SwiftData/UserDefaults).
4. Networking mappers (raw JSON → domain model), with a fake session — never hit the real network.
5. One end-to-end UI smoke test of the core flow.

---

## Project setup

- **Unit test target**: File ▸ New ▸ Target ▸ *Unit Testing Bundle*. Choose **Swift Testing** (default in Xcode 16+) or XCTest.
- **UI test target**: separate *UI Testing Bundle* — it launches the app as a black box.
- Add app code to tests with `@testable import YourApp` (unit tests only; gives access to `internal` symbols). UI tests use the accessibility layer, not imports.
- A `.xctestplan` lets you group tests, set env vars, and pick run configurations (e.g. randomized order). One default plan is fine for a prototype.
- Swift Testing and XCTest **coexist in the same target and same run** — migrate incrementally.

---

## Swift Testing (`@Test`) — the modern default

Import `Testing`, not `XCTest`. Tests are global functions or methods annotated `@Test`; assertions use `#expect` (non-fatal, keeps going) and `#require` (fatal, unwraps/stops).

```swift
import Testing
@testable import MyApp

@Test func totalIncludesTax() {
    let cart = Cart(items: [.init(price: 10), .init(price: 5)])
    #expect(cart.total(taxRate: 0.1) == 16.5)
}
```

- `#expect(expr)` records a failure and **continues** — the macro captures sub-expression values, so a failure prints the actual operands (no need for XCTest-style messages).
- `#require(expr)` throws and **halts the test** on failure — use it for preconditions where continuing is pointless. It also unwraps optionals (call in a `throws` test with `try`):

```swift
@Test func parsesUser() throws {
    let user = try #require(User(json: validJSON))  // stops here if nil
    #expect(user.name == "Ada")
}
```

### Suites and shared setup

Group with a `struct` (preferred) or `final class`. A **fresh instance is created per test**, so `init` is your per-test setup — no shared mutable state between tests by default.

```swift
@Suite("Cart pricing")
struct CartTests {
    let cart: Cart              // built fresh for every @Test below

    init() { cart = Cart(items: [.init(price: 10)]) }

    @Test func emptyTaxIsIdentity() {
        #expect(cart.total(taxRate: 0) == 10)
    }
}
```

Teardown: for a `struct` suite there usually is none — value types just drop when the instance goes away. If you genuinely need cleanup, use a `final class` and put it in `deinit`. Note `deinit` **cannot be `async` or `throwing`** and can't be actor-isolated, so for `@MainActor` suites or async cleanup, do the work at the end of each test (or use a custom scoping trait) rather than relying on `deinit`.

### Parameterized tests — one test, many cases

Replaces copy-pasted test bodies. Each argument runs as a **separate, independently-reported case**. A single array of tuples destructures positionally into multiple parameters:

```swift
@Test(arguments: [
    ("", false),
    ("a@b.co", true),
    ("no-at", false),
])
func validatesEmail(_ input: String, _ expected: Bool) {
    #expect(EmailValidator.isValid(input) == expected)
}
```

Destructuring is positional — element labels in the tuples don't have to match parameter names and are best omitted to avoid confusion. Cross-product of two sequences (every combination):

```swift
@Test(arguments: [1, 2, 3], ["x", "y"])   // 6 cases
func combos(n: Int, s: String) { #expect(!s.isEmpty && n > 0) }
```

Wrap in `zip(a, b)` when you want **paired** (not cross-product) cases:

```swift
@Test(arguments: zip([1, 2, 3], ["a", "b", "c"]))   // 3 cases
func paired(n: Int, s: String) { #expect(n > 0 && !s.isEmpty) }
```

### Traits: naming, tagging, skipping, known bugs

```swift
@Test("Discount never goes negative")                       // display name
@Test(.disabled("flaky until CI fix"))                      // skip, stays visible
@Test(.enabled(if: FeatureFlags.checkoutV2))                // conditional run
@Test(.bug("https://example.com/issue/42", "rounding"))     // url first, then title
@Test(.tags(.fast))                                         // group across files
```

Define tags once (do **not** add `@Test` — a tag is not a test):

```swift
extension Tag { @Tag static var fast: Self }
```

Run a subset from the CLI with `swift test --filter` / `xcodebuild ... -only-testing:`, or filter by tag in Xcode's Test navigator.

### Errors

```swift
@Test func throwsSpecificError() {
    #expect(throws: ValidationError.empty) {   // matches an exact value
        try Validator.check("")
    }
}

@Test func throwsAnything() {
    #expect(throws: (any Error).self) { try risky() }   // any error
}

// Assert NO throw by just calling `try` in a `throws` test — no wrapper needed:
@Test func doesNotThrow() throws { try safe() }
```

To inspect a thrown error's payload, capture it — `#expect(throws:)` / `#require(throws:)` **return the caught error** (Swift 6.1 / Xcode 16.3+):

```swift
@Test func reportsBadField() throws {
    let err = try #require(throws: ValidationError.self) { try parse(bad) }
    #expect(err.field == "email")
}
```

### Async & confirmations (callbacks / notifications)

`@Test` functions can be `async` directly — just `await`. For callback- or delegate-style APIs that fire N times, use `confirmation`:

```swift
@Test func loadsRemoteData() async throws {
    let sut = Loader(session: .stubbed(sampleJSON))
    let items = try await sut.fetchItems()
    #expect(items.count == 3)
}

@Test func emitsThreeEvents() async {
    await confirmation("fires 3x", expectedCount: 3) { confirm in
        let ticker = Ticker(count: 3)
        for await _ in ticker.events { confirm() }   // must all fire before closure returns
    }
}
```

`confirmation` **fails** if the expected count isn't reached **by the time the closure returns** — it does not block and wait like an `XCTestExpectation`. So drive the event source with `await` *inside* the closure (as above). For a plain completion-handler API that returns immediately, prefer `withCheckedContinuation` to bridge it to `async` rather than `confirmation`.

### Time budget

Guard slow/hung async tests with a time-limit trait (minute granularity):

```swift
@Test(.timeLimit(.minutes(1)))
func doesNotHang() async throws { try await work() }
```

---

## XCTest — still required for some things

Use XCTest when you need **UI tests**, **performance/`measure`**, or you're maintaining an existing suite. Same target as Swift Testing is fine.

```swift
import XCTest
@testable import MyApp

final class CartTests: XCTestCase {
    var cart: Cart!

    override func setUp() { super.setUp(); cart = Cart(items: []) }
    override func tearDown() { cart = nil; super.tearDown() }

    func testEmptyTotalIsZero() {
        XCTAssertEqual(cart.total(taxRate: 0), 0, accuracy: 0.0001)
    }
}
```

Assertion cheatsheet (each takes an optional trailing message):

```swift
XCTAssertEqual(a, b, accuracy: 0.001)   // use accuracy for Double/Float
XCTAssertTrue(x); XCTAssertFalse(x)
XCTAssertNil(x); XCTAssertNotNil(x)
let v = try XCTUnwrap(optional)         // unwrap-or-fail (throws)
XCTAssertThrowsError(try f()) { error in XCTAssertEqual(error as? E, .bad) }
XCTAssertNoThrow(try g())
XCTFail("unreachable")
```

### Async in XCTest

```swift
func testFetch() async throws {
    let items = try await loader.fetchItems()
    XCTAssertEqual(items.count, 3)
}
```

Callback style (avoid when you can use `async`):

```swift
func testCallback() {
    let exp = expectation(description: "done")
    service.load { result in
        XCTAssertNoThrow(try result.get())
        exp.fulfill()
    }
    wait(for: [exp], timeout: 2)
}
```

### API translation table

| Concept              | XCTest                        | Swift Testing                          |
|----------------------|-------------------------------|----------------------------------------|
| Test case            | `func testX()` in `XCTestCase`| `@Test func x()`                       |
| Group                | subclass of `XCTestCase`      | `@Suite struct`                        |
| Equal                | `XCTAssertEqual(a, b)`        | `#expect(a == b)`                      |
| Unwrap               | `try XCTUnwrap(x)`            | `try #require(x)`                      |
| True                 | `XCTAssertTrue(x)`            | `#expect(x)`                           |
| Throws               | `XCTAssertThrowsError`        | `#expect(throws:)`                     |
| Setup / teardown     | `setUp` / `tearDown`          | `init` / (`deinit`, non-async only)    |
| Skip                 | `throw XCTSkip(...)`          | `.disabled()` / `.enabled(if:)` trait  |
| Parameterize         | manual loop / data-driven     | `@Test(arguments:)`                    |
| Async wait           | `XCTestExpectation`           | `confirmation` / plain `await`         |

---

## Testable architecture (the real unlock)

Untestable code is almost always code that reaches out to the world (network, clock, disk, `Date()`, `UUID()`, singletons) from inside itself. Fix it by **injecting** those dependencies.

### 1. Protocol-based seams for I/O

```swift
protocol ItemService {
    func fetchItems() async throws -> [Item]
}

struct LiveItemService: ItemService {
    var session: URLSession = .shared
    func fetchItems() async throws -> [Item] {
        let (data, _) = try await session.data(from: .itemsEndpoint)
        return try JSONDecoder().decode([Item].self, from: data)
    }
}

struct StubItemService: ItemService {           // test double
    var result: Result<[Item], Error>
    func fetchItems() async throws -> [Item] { try result.get() }
}
```

### 2. View model owns logic; view stays dumb

Test the view model, not the view. On iOS 17+ prefer `@Observable`.

```swift
import Observation

@Observable @MainActor
final class ItemListModel {
    private(set) var items: [Item] = []
    private(set) var isLoading = false
    var errorMessage: String?
    private let service: ItemService

    init(service: ItemService) { self.service = service }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        do { items = try await service.fetchItems() }
        catch { errorMessage = "Couldn't load items." }
    }
}
```

```swift
@Test @MainActor func loadPopulatesItems() async {
    let model = ItemListModel(service: StubItemService(result: .success([.sample])))
    await model.load()
    #expect(model.items.count == 1)
    #expect(model.errorMessage == nil)
}

@Test @MainActor func loadSurfacesError() async {
    let model = ItemListModel(service: StubItemService(result: .failure(URLError(.timedOut))))
    await model.load()
    #expect(model.items.isEmpty)
    #expect(model.errorMessage != nil)
}
```

Mark the test (or whole suite) `@MainActor` to match the model's isolation — then property access is synchronous and no actor hops are needed.

### 3. Inject non-determinism

Never call `Date()`, `UUID()`, or `Task.sleep` deep inside logic. Pass them in:

```swift
struct SessionTimer {
    var now: () -> Date = Date.init            // default = real clock
    func hasExpired(started: Date, ttl: TimeInterval) -> Bool {
        now().timeIntervalSince(started) > ttl
    }
}

@Test func expiresAfterTTL() {
    let fixed = Date(timeIntervalSince1970: 1_000)
    let timer = SessionTimer(now: { fixed })   // frozen clock
    #expect(timer.hasExpired(started: fixed.addingTimeInterval(-120), ttl: 60))
}
```

### 4. Fake `URLSession` without a live server

Use `URLProtocol` to intercept requests — no third-party mock library needed.

```swift
final class StubURLProtocol: URLProtocol {
    // Set once before creating the session; single-threaded test setup.
    nonisolated(unsafe) static var stub: (data: Data, response: HTTPURLResponse)?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for r: URLRequest) -> URLRequest { r }

    override func startLoading() {
        if let stub = Self.stub {
            client?.urlProtocol(self, didReceive: stub.response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: stub.data)
        }
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

extension URLSession {
    static func stubbed(_ data: Data, status: Int = 200) -> URLSession {
        StubURLProtocol.stub = (
            data,
            HTTPURLResponse(url: URL(string: "https://stub.local")!, statusCode: status,
                            httpVersion: nil, headerFields: nil)!
        )
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        return URLSession(configuration: config)
    }
}
```

Prefer stubbing at the **protocol/service seam** (option 1) when you can — it's simpler. Reach for `URLProtocol` when you specifically want to exercise the real `URLSession` + decoding path.

---

## Xcode Previews — your primary UI feedback loop

Previews are the fastest way to iterate on SwiftUI and to eyeball states that would be tedious to reach at runtime. Treat them as **executable design documentation**, not tests — but they catch crashes, layout breakage, and bad data handling instantly.

Modern `#Preview` macro (iOS 17+):

```swift
#Preview("Loaded") {
    ItemListView(model: ItemListModel(service: StubItemService(result: .success(Item.samples))))
}

#Preview("Empty") {
    ItemListView(model: ItemListModel(service: StubItemService(result: .success([]))))
}

#Preview("Error", traits: .sizeThatFitsLayout) {   // first trait is required in this overload
    ItemListView(model: ItemListModel(service: StubItemService(result: .failure(URLError(.timedOut)))))
}
```

Rules that make previews pay off:
- **Preview every meaningful state**: empty, loading, error, one-item, many-items, long-text, largest Dynamic Type, dark mode.
- Feed views the **same stub doubles** you use in unit tests — one fixtures file (`Item.samples`) serves both.
- Force environment variants right in the preview. Use `\.dynamicTypeSize` (the `\.sizeCategory` / `ContentSizeCategory` API is deprecated):

```swift
#Preview("Dark · XXL") {
    ContentView()
        .environment(\.colorScheme, .dark)
        .environment(\.dynamicTypeSize, .accessibility3)
}
```

- Keep views **preview-able by injecting data** — a view that constructs its own network client can't be previewed offline. Same discipline that makes it testable.
- Use `@Previewable @State` when a preview needs local mutable state to drive a binding:

```swift
#Preview {
    @Previewable @State var text = "Hello"
    TextField("Name", text: $text)
}
```

---

## Snapshot testing basics (no third-party lib)

A snapshot test renders a view to an image and diffs it against a stored reference — good for catching unintended visual regressions. There's no first-party snapshot *assertion* API, but you can render deterministically with **`ImageRenderer`** (iOS 16+) and diff yourself. `ImageRenderer` is main-actor-only.

```swift
import SwiftUI
import Testing
@testable import MyApp

@MainActor
func pngData<V: View>(_ view: V, size: CGSize) -> Data? {
    let renderer = ImageRenderer(content: view.frame(width: size.width, height: size.height))
    renderer.scale = 2                    // pin scale so bytes are deterministic
    return renderer.uiImage?.pngData()
}

@Test @MainActor func badgeMatchesReference() throws {
    let data = try #require(pngData(Badge(count: 3), size: .init(width: 60, height: 60)))
    let ref  = try #require(referencePNG(named: "badge_3"))   // your fixtures loader
    #expect(data == ref)                  // exact-match; see caveats
}
```

Pragmatic guidance for a prototype:
- Snapshot tests are **high-maintenance and environment-sensitive** (OS version, font metrics, scale). For a prototype, **Previews usually give better ROI** than a snapshot suite.
- If you do snapshot: **pin `scale`**, a fixed frame, and a fixed color scheme; commit reference images; regenerate deliberately when the design intentionally changes.
- Exact byte-equality is brittle across OS updates. If you need tolerance, compare pixel buffers with a small per-pixel threshold rather than raw `Data ==`.
- Good targets: small self-contained visual components (badges, chips, chart cells). Bad targets: whole screens with system chrome or animation.

---

## UI tests (XCUITest) — one smoke test, not fifty

UI tests drive the app as a user via the **accessibility tree**. They're the slowest, flakiest layer — keep them few and focused on critical end-to-end flows.

```swift
import XCTest

final class CheckoutUITests: XCTestCase {
    override func setUp() { continueAfterFailure = false }

    func testAddItemAndCheckout() {
        let app = XCUIApplication()
        app.launchArguments = ["-uitesting"]     // app reads this to load stub data
        app.launch()

        app.buttons["addItemButton"].tap()
        XCTAssertTrue(app.staticTexts["cartCountLabel"].waitForExistence(timeout: 2))

        app.buttons["checkoutButton"].tap()
        XCTAssertTrue(app.staticTexts["orderConfirmedLabel"].waitForExistence(timeout: 5))
    }
}
```

Make UI tests reliable:
- **Query by accessibility identifier**, never by localized display text: `.accessibilityIdentifier("checkoutButton")` in the view, `app.buttons["checkoutButton"]` in the test. Identifiers don't change with copy or locale.
- **Always `waitForExistence(timeout:)`** before asserting/tapping — never assume synchronous appearance. Avoid `sleep()`.
- **Inject deterministic state** via `launchArguments` / `launchEnvironment`; branch in the app to load stubs and bypass login/network. UI tests can't `@testable import`, so this is your only seam.
- Set `continueAfterFailure = false` so a failed step doesn't cascade misleading failures.
- One golden-path test + maybe one critical error path is plenty for a prototype.

---

## Running, coverage, and CI

Command line (simulator; `OS=latest` avoids pinning a version that drifts with your toolchain):

```bash
xcodebuild test \
  -scheme MyApp \
  -destination 'platform=iOS Simulator,name=iPhone 16,OS=latest' \
  -enableCodeCoverage YES \
  -resultBundlePath TestResults.xcresult
```

- Run a subset: `-only-testing:MyAppTests/CartTests/emptyTaxIsIdentity`.
- Coverage is a **guide, not a goal.** 100% coverage of getters proves nothing; one good test of the pricing branch proves a lot. Watch coverage on *logic* files, ignore it on views.
- Randomize test order in the test plan to surface hidden inter-test state coupling.
- SwiftPM package? `swift test` runs Swift Testing and XCTest together; filter with `swift test --filter CartTests`.

---

## Common pitfalls

- **Testing views instead of view models.** SwiftUI `body` is hard to assert on; the logic behind it isn't. Move logic out, test that.
- **Hitting the real network/clock/disk.** Flaky, slow, offline-hostile. Inject a seam.
- **Shared mutable state between tests.** In Swift Testing each `@Test` gets a fresh suite instance — don't reintroduce `static var`s. In XCTest, reset in `tearDown`.
- **`Date()`/`UUID()` inside logic** → non-deterministic tests. Inject them.
- **Asserting on localized strings** in UI tests → breaks on translation. Use accessibility identifiers.
- **Force-unwrapping in tests** → an unclear crash instead of a readable failure. Use `try #require` / `XCTUnwrap`.
- **Comparing `Double` with `==`.** Use `accuracy:` (XCTest) or compare within an epsilon (`abs(a - b) < 1e-9`) in `#expect`.
- **`confirmation` for immediate completion handlers.** It doesn't wait; if `confirm()` fires after the closure returns, the test fails. Bridge with `withCheckedContinuation` instead.
- **Over-investing in snapshot/UI tests on a prototype.** They rot fastest. Lean on Previews + view-model unit tests.

---

## Minimum-viable suite for a new prototype (copy this shape)

1. `ModelTests.swift` — Swift Testing suite over your pure logic (pricing, validation, formatting) with `@Test(arguments:)` for edge cases.
2. `ViewModelTests.swift` — `@MainActor` suite feeding stub services into each `@Observable` model; assert `items`, `isLoading`, `errorMessage` for success + failure.
3. `PersistenceTests.swift` — encode → decode / save → load round-trips.
4. `Fixtures.swift` — `Item.samples`, stub services, sample JSON. **Shared by tests and Previews.**
5. `CoreFlowUITests.swift` — one XCUITest walking the primary user journey with `-uitesting` stub data.
6. Rich **`#Preview`s** on every screen covering empty/loading/error/populated/dark/largest-Dynamic-Type.

That's roughly 150–300 lines of test code that catches the large majority of regressions in a small app — without the flakiness tax of an over-built suite.
