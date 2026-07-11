<!-- keywords: swiftdata, on-device persistence ios, @model macro, modelcontainer, @query swiftui, modelcontext insert save, swiftdata relationships delete rule, lightweight migration swiftdata, versionedschema schemamigrationplan, userdefaults appstorage, codable json application support, offline-first ios sync, core data alternative ios 17, modelactor background import, swiftdata #unique #index ios 18, swiftdata history tombstone, fetchdescriptor predicate, externalstorage attribute, cloudkit swiftdata sync, persistentidentifier re-fetch -->

# On-Device Persistence for iOS 17+ (SwiftData, UserDefaults, Files)

Reference for local persistence on iOS 17+/Swift 5.9+ (best practices as of 2026). First-party frameworks only. Every snippet is written to compile and be idiomatic. Where an API is iOS 18+, it is flagged inline.

## Choosing the right store

Pick per **kind of data**, not per convenience.

- **SwiftData** — structured, queryable object graphs with relationships. The default for "app data" (tasks, notes, transactions, cached API entities). Replaces Core Data for new apps. Use it whenever you will ever `filter`, `sort`, or relate the data.
- **UserDefaults / `@AppStorage`** — small, flat, non-secret *settings* and UI flags (KB, not MB). Booleans, enums, last-selected tab, onboarding-seen. Never a data store, never for secrets (it is plaintext in the app container).
- **Codable + JSON to Application Support** — one small self-contained blob you own end-to-end: no queries, no relationships (a settings snapshot, a feature-flag cache, an export/import file). Simpler than SwiftData when you never filter/sort and want a human-readable file.
- **Files (Documents / Application Support / Caches)** — binary blobs: images, audio, PDFs, ML models. Store the *bytes* on disk; store the *reference/metadata* in SwiftData.
- **Keychain** — secrets: tokens, passwords, keys. Via the `Security` framework (`kSecClass…`). Never UserDefaults for these.

Quick heuristic: queryable/related → SwiftData; secret → Keychain; large bytes → a file with only a reference in the DB; one small owned blob → Codable JSON; tiny flag → UserDefaults.

## SwiftData quick start

```swift
import SwiftUI
import SwiftData

@Model
final class Trip {
    var name: String
    var startDate: Date
    var createdAt: Date

    init(name: String, startDate: Date, createdAt: Date = .now) {
        self.name = name
        self.startDate = startDate
        self.createdAt = createdAt
    }
}

@main
struct TravelApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        // Builds the container for the whole app and injects a
        // main-thread ModelContext into the SwiftUI environment.
        .modelContainer(for: Trip.self)
    }
}
```

- `@Model` is a macro: it makes the class persistable, observable (conforms to `Observable`), and adds change tracking. Apply only to a `final class`, never a `struct`.
- `.modelContainer(for:)` builds a `ModelContainer` and puts its `mainContext` in the environment for `@Query` and `@Environment(\.modelContext)`.
- Pass an array for multiple root types: `.modelContainer(for: [Trip.self, LivingAccommodation.self])`. You only need to list types not reachable via relationships from another listed type.

## @Model in depth

```swift
@Model
final class Book {
    // iOS 18+: natural key. On insert-collision SwiftData upserts.
    #Unique<Book>([\.isbn])

    // iOS 18+: indexes for fast filter/sort. Each [...] is one index;
    // a multi-key-path array is a compound index (order matters).
    #Index<Book>([\.title], [\.year], [\.title, \.year])

    var isbn: String
    var title: String
    var year: Int
    var summary: String

    // Store under a stable column name even if you rename the property.
    @Attribute(originalName: "pageCount") var pages: Int

    // Large binary kept in an external file, referenced by the store.
    @Attribute(.externalStorage) var coverImage: Data?

    // iOS 17 single-property uniqueness (upserts on collision).
    @Attribute(.unique) var slug: String

    // Not persisted; must have a default. Reset on each launch.
    @Transient var isSelected: Bool = false

    init(isbn: String, title: String, year: Int,
         summary: String = "", pages: Int = 0, slug: String) {
        self.isbn = isbn
        self.title = title
        self.year = year
        self.summary = summary
        self.pages = pages
        self.slug = slug
    }
}
```

Property rules:

- Supported types: `Bool/Int/Double/Float/String/Date/Data/UUID/URL`, `Codable` structs/enums, `RawRepresentable` enums, arrays/dictionaries of the above, and relationships to other `@Model`s.
- Every stored property needs a default or must be set in `init`. Give sensible defaults so future lightweight migrations stay automatic.
- Enums persist automatically if they conform to `Codable` (add `: Codable`, or use a `RawRepresentable` enum).
- `#Unique` and `#Index` are **iOS 18+**. On iOS 17, use `@Attribute(.unique)` for single-property uniqueness; there is no index macro (indexing is implicit/unavailable).
- `.externalStorage` keeps large `Data` out of the SQLite row while you still read/write it as a normal property.

## Relationships

```swift
@Model
final class Trip {
    var name: String

    // To-many. Deleting a Trip cascades to its accommodations.
    // Declare the inverse on exactly ONE side (here).
    @Relationship(deleteRule: .cascade, inverse: \LivingAccommodation.trip)
    var accommodations: [LivingAccommodation] = []

    init(name: String) { self.name = name }
}

@Model
final class LivingAccommodation {
    var address: String
    var trip: Trip?          // to-one back-reference; SwiftData wires the inverse

    init(address: String) { self.address = address }
}
```

- Declare `@Relationship(inverse:)` on **one** side only; declaring it on both causes conflicts. SwiftData infers the other direction.
- Delete rules:
  - `.cascade` — delete the related objects too (owner → owned).
  - `.nullify` (**default**) — clear the reference, keep the related object.
  - `.deny` — refuse to delete while relationships exist.
  - `.noAction` — leave the graph untouched (you take responsibility for integrity).
- To-many defaults to `[]`; optional to-one defaults to `nil`. Don't force-unwrap to-one relationships.
- To-many relationships are **unordered**. For a stable order, add your own `sortOrder` field and sort on read. (`@Relationship` has no `ordered:` option.)
- Many-to-many: to-many on both sides, inverse declared on one.

## @Query — reading in SwiftUI

`@Query` is the read path in views. It auto-updates the UI when the store changes.

```swift
struct TripList: View {
    @Query(sort: \Trip.startDate, order: .forward, animation: .default)
    private var trips: [Trip]

    var body: some View {
        List(trips) { trip in
            Text(trip.name)
        }
    }
}
```

Parameterized query with a predicate and multiple sort descriptors — build it in `init`:

```swift
struct UpcomingTrips: View {
    @Query private var trips: [Trip]

    init(after date: Date) {
        _trips = Query(
            filter: #Predicate<Trip> { $0.startDate >= date },
            sort: [SortDescriptor(\.startDate),
                   SortDescriptor(\.name, order: .reverse)]
        )
    }

    var body: some View { List(trips) { Text($0.name) } }
}
```

- `@Query` reads from the **main context** injected via `.modelContainer`. It is a SwiftUI property wrapper — usable only inside a `View`.
- To parameterize, assign the backing store `_trips = Query(...)` in `init`. The default-value form cannot reference `self`'s other properties.
- `#Predicate` supports comparisons, `&&`/`||`/`!`, `contains`, `starts(with:)`, `localizedStandardContains`, optionals, and relationship key paths. It does **not** support arbitrary Swift closures/functions — it compiles to a store query.
- Push filtering/sorting into the predicate/sort so the work runs in the store, not in Swift after fetch.
- To fetch outside a view, use `FetchDescriptor` on a `ModelContext` (below).

## ModelContext — writing

```swift
struct AddTripButton: View {
    @Environment(\.modelContext) private var context

    var body: some View {
        Button("Add") {
            context.insert(Trip(name: "Kyoto", startDate: .now))
            // The main context autosaves on a runloop tick; explicit
            // save() forces a deterministic checkpoint.
            try? context.save()
        }
    }
}
```

Core operations:

```swift
context.insert(model)      // add new (or upsert if a unique key collides)
context.delete(model)      // remove one
try context.save()         // flush pending changes to disk
context.rollback()         // discard unsaved changes since last save
```

Bulk delete by type (the `where` predicate is optional):

```swift
try context.delete(model: Trip.self)                              // all Trips
try context.delete(model: Trip.self,
                   where: #Predicate { $0.name.isEmpty })         // matching only
```

Fetch imperatively (view model, `.task`, actor, etc.):

```swift
func loadTrips(_ context: ModelContext) throws -> [Trip] {
    var descriptor = FetchDescriptor<Trip>(
        predicate: #Predicate { $0.startDate >= Date.now },
        sortBy: [SortDescriptor(\.startDate)]
    )
    descriptor.fetchLimit = 50
    descriptor.fetchOffset = 0
    // Optional perf tuning: pre-load only these attributes / relationships.
    descriptor.propertiesToFetch = [\.name, \.startDate]
    descriptor.relationshipKeyPathsForPrefetching = [\.accommodations]
    return try context.fetch(descriptor)
}

// Cheap count without materializing objects:
let count = try context.fetchCount(FetchDescriptor<Trip>())
```

- The **main context autosaves**; explicit `save()` is for determinism (e.g., before backgrounding, or right after a critical write).
- Disable autosave when you want full control: `container.mainContext.autosaveEnabled = false`. Then you must `save()` yourself.
- A `ModelContext` is **not `Sendable`** and is bound to the actor/thread it was created on. Never pass one across concurrency domains. Pass `PersistentIdentifier`s and re-fetch (see ModelActor).
- Note: use `Date.now` (not bare `.now`) inside a `#Predicate` — the compiler needs the explicit type there.

## Configuring the container explicitly

```swift
let schema = Schema([Trip.self, LivingAccommodation.self])

let config = ModelConfiguration(
    schema: schema,
    isStoredInMemoryOnly: false,          // true = ephemeral (tests/previews)
    allowsSave: true,
    groupContainer: .identifier("group.com.example.app"), // App Group sharing
    cloudKitDatabase: .automatic          // .none to disable CloudKit
)

let container = try ModelContainer(for: schema, configurations: [config])
```

Use it in SwiftUI:

```swift
WindowGroup { ContentView() }
    .modelContainer(container)
```

In-memory container for `#Preview` and unit tests — fast and disposable:

```swift
#Preview {
    TripList()
        .modelContainer(for: Trip.self, inMemory: true)
}
```

- **CloudKit sync** (private DB) is opt-in via `cloudKitDatabase` plus the iCloud + CloudKit entitlement. Requirements: every attribute has a default or is optional, **no uniqueness constraints** (`@Attribute(.unique)` / `#Unique` are unsupported), and every relationship is optional. Design the schema CloudKit-compatible from day one if sync is remotely likely.
- **App Group** container (`groupContainer:`) lets a widget/extension read the same store.

## Migrations

SwiftData performs **lightweight (automatic) migrations** for additive, inferable changes with no code:

- Add a property that has a default or is optional.
- Delete a property.
- Rename a property via `@Attribute(originalName:)` (avoids a destructive drop+add).
- Add a relationship the mapping can infer.

Anything else — splitting/merging fields, changing a type, or adding a uniqueness constraint that needs dedup — requires a **`VersionedSchema` + `SchemaMigrationPlan`**.

```swift
enum SchemaV1: VersionedSchema {
    static var versionIdentifier = Schema.Version(1, 0, 0)
    static var models: [any PersistentModel.Type] { [Trip.self] }

    @Model final class Trip {
        var name: String
        init(name: String) { self.name = name }
    }
}

enum SchemaV2: VersionedSchema {
    static var versionIdentifier = Schema.Version(2, 0, 0)
    static var models: [any PersistentModel.Type] { [Trip.self] }

    @Model final class Trip {
        @Attribute(.unique) var name: String   // now unique
        var color: String                       // new; defaulted, so additive
        init(name: String, color: String = "blue") {
            self.name = name
            self.color = color
        }
    }
}

enum TripMigrationPlan: SchemaMigrationPlan {
    static var schemas: [any VersionedSchema.Type] {
        [SchemaV1.self, SchemaV2.self]
    }

    static var stages: [MigrationStage] { [migrateV1toV2] }

    // Custom stage: dedupe names in willMigrate, BEFORE the .unique
    // constraint is applied in the new schema.
    static let migrateV1toV2 = MigrationStage.custom(
        fromVersion: SchemaV1.self,
        toVersion: SchemaV2.self,
        willMigrate: { context in
            let trips = try context.fetch(FetchDescriptor<SchemaV1.Trip>())
            var seen = Set<String>()
            for trip in trips where !seen.insert(trip.name).inserted {
                context.delete(trip)   // drop duplicates
            }
            try context.save()
        },
        didMigrate: nil
    )
}
```

Wire the plan into the container:

```swift
let container = try ModelContainer(
    for: SchemaV2.Trip.self,
    migrationPlan: TripMigrationPlan.self
)
```

- Use `MigrationStage.lightweight(fromVersion:toVersion:)` for stages that need no code; `.custom(...)` when you must transform data.
- Bump `versionIdentifier` per schema. Keep old `VersionedSchema` enums around so the chain runs on devices that skipped versions.
- Reference models through the schema namespace (`SchemaV1.Trip`) inside migration code to avoid ambiguity.
- Test migrations against a real seeded store copied from a prior build, not just an empty one.

## Background work with @ModelActor

`ModelContext` is not `Sendable`. For imports, batch inserts, or heavy fetches off the main thread, use a `@ModelActor`.

```swift
struct TripPayload { let name: String; let date: Date }

@ModelActor
actor DataImporter {
    func importTrips(_ payloads: [TripPayload]) throws {
        for (i, p) in payloads.enumerated() {
            modelContext.insert(Trip(name: p.name, startDate: p.date))
            if i % 500 == 0 { try modelContext.save() }   // bound memory
        }
        try modelContext.save()
    }

    // Return identifiers, not models — models can't cross the actor boundary.
    func tripIDs() throws -> [PersistentIdentifier] {
        try modelContext.fetch(FetchDescriptor<Trip>()).map(\.persistentModelID)
    }
}
```

Usage:

```swift
let importer = DataImporter(modelContainer: container)   // synthesized init
try await importer.importTrips(payloads)

// Back on the main context, re-fetch by ID to display:
if let id = try await importer.tripIDs().first,
   let trip = container.mainContext.model(for: id) as? Trip {
    // use `trip` on the main thread
}
```

- `@ModelActor` synthesizes `init(modelContainer:)` and an actor-isolated `modelContext` running on the actor's own executor.
- **Never** return `@Model` instances across the actor boundary — they're bound to their context and not `Sendable`. Return `PersistentIdentifier` (`model.persistentModelID`) and re-fetch with `context.model(for:)` or `context.fetch(...)`.
- `ModelContainer` **is `Sendable`**; share it freely. `ModelContext` is not.

## Change tracking (SwiftData History, iOS 18+)

Process store changes in order — for server sync, undo, or reacting to writes from a widget/extension. Deletes leave **tombstones** so you can propagate deletions.

```swift
// `lastToken: DefaultHistoryToken?` persisted between passes.
var descriptor = HistoryDescriptor<DefaultHistoryTransaction>()
if let lastToken {
    descriptor.predicate = #Predicate { $0.token > lastToken }
}
let transactions = try context.fetchHistory(descriptor)

for tx in transactions {
    for change in tx.changes {
        switch change {
        case .insert(let insert):
            handleInsert(insert.changedPersistentIdentifier)
        case .update(let update):
            handleUpdate(update.changedPersistentIdentifier)
        case .delete(let delete):
            // The row is gone; the tombstone carries its identifier/values.
            handleDelete(delete.changedPersistentIdentifier, delete.tombstone)
        @unknown default:
            break
        }
    }
    lastToken = tx.token   // advance the cursor
}
```

- History requires the store to have change tracking enabled (default for the standard SwiftData store). Deletes are recoverable only via their tombstone.
- Persist the last `DefaultHistoryToken` (e.g., in UserDefaults, or a small file) so each pass is incremental.
- The change cases carry typed values (`DefaultHistoryInsert`/`Update`/`Delete`); the common metadata (`changedPersistentIdentifier`) is available on each.

## UserDefaults & @AppStorage

For small, non-secret settings. `@AppStorage` is the SwiftUI-bound view over `UserDefaults`.

```swift
enum Theme: String, CaseIterable { case system, light, dark
    var label: String { rawValue.capitalized }
}

struct SettingsView: View {
    @AppStorage("hasSeenOnboarding") private var seen = false
    @AppStorage("fontScale") private var fontScale = 1.0
    @AppStorage("theme") private var theme: Theme = .system   // RawRepresentable

    var body: some View {
        Form {
            Toggle("Onboarding done", isOn: $seen)
            Slider(value: $fontScale, in: 0.8...1.5)
            Picker("Theme", selection: $theme) {
                ForEach(Theme.allCases, id: \.self) { Text($0.label).tag($0) }
            }
        }
    }
}
```

Shared defaults across an App Group (widgets/extensions):

```swift
@AppStorage("badgeCount", store: UserDefaults(suiteName: "group.com.example.app"))
private var badgeCount = 0
```

Non-SwiftUI access:

```swift
let defaults = UserDefaults.standard
defaults.set(true, forKey: "hasSeenOnboarding")
let seen = defaults.bool(forKey: "hasSeenOnboarding")
```

- Supported types: `Bool`, `Int`, `Double`, `String`, `Data`, `URL`, `Date`, arrays/dicts of these, and `RawRepresentable` whose raw value is `String`/`Int`.
- **Not for**: secrets (Keychain), large data (files), or anything queryable (SwiftData). Everything here is plaintext in the app container.
- Register launch defaults so the first read isn't the zero value: `defaults.register(defaults: ["fontScale": 1.0])`.

## Codable + JSON to Application Support

When you own one small blob end-to-end and never need queries. Application Support is backed up and hidden from the user; Documents is user-visible (Files app); Caches is purgeable by the system.

```swift
struct AppSettings: Codable {
    var lastSyncDate: Date?
    var favoriteTags: [String] = []
    var featureFlags: [String: Bool] = [:]
}

enum SettingsStore {
    private static let filename = "settings.json"

    private static func fileURL() throws -> URL {
        let dir = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true            // creates Application Support if missing
        )
        return dir.appendingPathComponent(filename)
    }

    static func load() throws -> AppSettings {
        let url = try fileURL()
        guard FileManager.default.fileExists(atPath: url.path) else {
            return AppSettings()
        }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(AppSettings.self, from: try Data(contentsOf: url))
    }

    static func save(_ settings: AppSettings) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(settings)
        // Atomic write: temp file + rename, so a crash never truncates the file.
        try data.write(to: try fileURL(), options: [.atomic])
    }
}
```

- Always write with `.atomic` to avoid half-written files.
- `.applicationSupportDirectory` for app-managed data, `.documentDirectory` only if the user should see/manage the file, `.cachesDirectory` for regenerable data.
- Exclude large regenerable files from iCloud backup:

```swift
var url = fileURL
var values = URLResourceValues()
values.isExcludedFromBackup = true
try url.setResourceValues(values)   // `url` must be a var
```

- Prefer this over SwiftData when it is a single blob, no relationships, no filtering, and you want a human-readable file you can diff/version.

## Storing binaries (files + a DB reference)

Keep bytes on disk; keep only the reference in SwiftData.

```swift
@Model
final class Attachment {
    var id: UUID
    var relativePath: String     // filename relative to a known directory
    var addedAt: Date
    init(id: UUID = UUID(), relativePath: String, addedAt: Date = .now) {
        self.id = id
        self.relativePath = relativePath
        self.addedAt = addedAt
    }
}

func saveImage(_ data: Data, context: ModelContext) throws {
    let dir = try FileManager.default.url(
        for: .applicationSupportDirectory, in: .userDomainMask,
        appropriateFor: nil, create: true
    ).appendingPathComponent("attachments", isDirectory: true)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

    let name = "\(UUID().uuidString).jpg"
    try data.write(to: dir.appendingPathComponent(name), options: .atomic)
    context.insert(Attachment(relativePath: name))
}
```

- Store a **relative** filename, not an absolute URL: the app-container path changes across installs/updates. Reconstruct the absolute URL at read time from the directory + filename.
- On delete, remove **both** the model and the file — SwiftData won't touch the file for you. (`.externalStorage` binaries, by contrast, are managed by the store.)

## Offline-first patterns

Treat the local store as the source of truth; the network is a sync input.

- **Local store is source of truth.** UI reads only from SwiftData via `@Query`. Never block rendering on the network.
- **Sync state per entity.** Model the lifecycle explicitly:

```swift
enum SyncState: Int, Codable { case synced, pendingCreate, pendingUpdate, pendingDelete }

@Model
final class Note {
    #Unique<Note>([\.remoteID])     // iOS 18+; enables upsert-on-insert
    var remoteID: String
    var body: String
    var updatedAt: Date
    var syncState: SyncState

    init(remoteID: String, body: String,
         updatedAt: Date = .now, syncState: SyncState = .pendingCreate) {
        self.remoteID = remoteID
        self.body = body
        self.updatedAt = updatedAt
        self.syncState = syncState
    }
}
```

- **Write path:** mutate locally, mark `pending*`, enqueue a sync. A background `@ModelActor` pushes pending rows, then flips them to `.synced`.
- **Pull path:** fetch remote deltas and **upsert** by natural key. With a uniqueness constraint (`#Unique` on iOS 18+, `@Attribute(.unique)` on iOS 17), re-inserting a row that shares the key updates the existing row instead of duplicating:

```swift
context.insert(Note(remoteID: dto.id, body: dto.body,
                    updatedAt: dto.updatedAt, syncState: .synced))
try context.save()
```

- **Conflict resolution:** last-write-wins via `updatedAt` is the simplest correct default; add field-level merge only if the domain demands it.
- **Deletes:** soft-delete locally (`pendingDelete`), confirm with the server, then hard-delete. On iOS 18+, use SwiftData History tombstones to propagate remote deletions into the store.
- **Identity:** generate a local `UUID` immediately so offline-created objects have a stable id before the server assigns one. Natural keys + upsert make repeated syncs idempotent after flaky connectivity.

## Testing & previews

```swift
@MainActor
func makeTestContext() throws -> ModelContext {
    let config = ModelConfiguration(isStoredInMemoryOnly: true)
    let container = try ModelContainer(for: Trip.self, configurations: config)
    return container.mainContext
}
```

- `isStoredInMemoryOnly: true` gives a clean store per test with no disk teardown.
- Seed preview data by inserting into the in-memory `mainContext` before returning the view.
- Because `@Model` types are `Observable`, assert on model state directly after `save()`.

## Pitfalls checklist

- `@Model` on a `struct` — must be a `final class`.
- Passing a `ModelContext` or `@Model` across actors/threads — pass `PersistentIdentifier` and re-fetch.
- Declaring `@Relationship(inverse:)` on both sides — declare on exactly one.
- Uniqueness constraint + CloudKit — `@Attribute(.unique)`/`#Unique` are unsupported with sync; remove them or drop sync.
- Non-optional relationship or non-defaulted attribute + CloudKit — CloudKit needs all relationships optional and all attributes defaulted/optional.
- `#Unique` / `#Index` / History on iOS 17 — iOS 18+ only; gate with `if #available` or fall back to `@Attribute(.unique)`.
- Storing large `Data`/images inline in a row — use `.externalStorage` or a file + reference.
- Expecting a to-many relationship to preserve insertion order — it won't; add an explicit sort key.
- Bare `.now` inside a `#Predicate` — write `Date.now` so the type resolves.
- Forgetting `try context.save()` before termination when autosave is disabled.
- Secrets in UserDefaults — use Keychain.

## Sources

- [SwiftData — Apple Developer Documentation](https://developer.apple.com/documentation/swiftdata)
- [What's new in SwiftData — WWDC24](https://developer.apple.com/videos/play/wwdc2024/10137/)
- [Track model changes with SwiftData history — WWDC24](https://developer.apple.com/videos/play/wwdc2024/10075/)
- [DefaultHistoryTransaction — Apple Developer Documentation](https://developer.apple.com/documentation/swiftdata/defaulthistorytransaction)
- [FetchDescriptor — Apple Developer Documentation](https://developer.apple.com/documentation/swiftdata/fetchdescriptor)
- [SwiftData Indexes — Use Your Loaf](https://useyourloaf.com/blog/swiftdata-indexes/)
