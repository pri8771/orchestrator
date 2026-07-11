<!-- keywords: xcode project structure, project.pbxproj, code signing, provisioning profile, entitlements, info.plist, usage description strings, development team, code_sign_style automatic, bundle identifier reverse dns, xcodebuild cli, physical iphone device build, real device install, capabilities entitlements, targets and schemes, simulator vs device signing, code_signing_allowed, product_bundle_identifier, xcconfig build settings, exportarchive ipa, devicectl install app, generate_infoplist_file, nscalendarsfullaccessusagedescription, developer mode ios 17, allowprovisioningupdates -->

# Xcode Project Structure & Shipping to a Real iPhone

Dense reference for building an iOS 17+ / Swift 5.9+ app that installs and runs on a **physical iPhone**, not just the Simulator. Covers `project.pbxproj`, targets/schemes, `Info.plist`, entitlements, and correct code signing. First-party only, zero third-party dependencies. Written against Xcode 16–26; the concepts are stable back to Xcode 15.

> The #1 reason an AI-generated app "builds" but won't run on a real phone: it was configured Simulator-only with signing disabled. A real device **refuses to launch unsigned code**. Get bundle id + team + automatic signing right and everything else follows.

---

## TL;DR — the golden config for a device build

Set these on the **app target** (in project settings or, better, an `.xcconfig`):

```
PRODUCT_BUNDLE_IDENTIFIER  = com.yourcompany.appname   // real reverse-DNS, globally unique
DEVELOPMENT_TEAM           = ABCDE12345                // your 10-char Apple Team ID
CODE_SIGN_STYLE            = Automatic                 // let Xcode manage profiles
CODE_SIGNING_ALLOWED       = YES                       // MUST be YES for device
CODE_SIGNING_REQUIRED      = YES                       // MUST be YES for device
CODE_SIGN_IDENTITY         = Apple Development         // signing cert type (dev builds)
IPHONEOS_DEPLOYMENT_TARGET = 17.0
SWIFT_VERSION              = 5.9                        // or 6.0
```

Build & install from the CLI (device plugged in, unlocked, trusted):

```bash
xcodebuild \
  -scheme "MyApp" \
  -configuration Debug \
  -destination 'platform=iOS,name=Priyansh’s iPhone' \
  -allowProvisioningUpdates \
  build
```

`-allowProvisioningUpdates` lets Xcode create/refresh the signing certificate and provisioning profile non-interactively. Without it, a fresh machine fails signing on the CLI.

---

## Why Simulator-only builds get rejected by a real device

The Simulator and a physical iPhone are **fundamentally different runtimes**, and this trips up generated projects constantly:

- **Architecture / SDK.** The Simulator runs `arm64` (Apple silicon) / `x86_64` (Intel) slices against the `iphonesimulator` SDK. A device is `arm64` against the `iphoneos` SDK. A binary built only for the Simulator SDK **cannot execute on device** — wrong Mach-O load commands, wrong platform.
- **Code signing.** The Simulator runs code **unsigned** — that's why people get away with `CODE_SIGNING_ALLOWED=NO`. iOS on real hardware enforces a hardware root of trust: **every executable page must be covered by a valid signature** whose entitlements are authorized by an embedded provisioning profile. Unsigned or ad-hoc-signed apps are killed by AMFI at launch ("Unable to install" / a code-signature error in the device log).
- **Provisioning.** The device must contain a provisioning profile that (a) is signed by Apple, (b) lists the device UDID (for Development profiles), (c) matches the app's bundle id, and (d) grants the entitlements the binary declares. The Simulator needs none of this.

**Net effect:** a project with `CODE_SIGNING_ALLOWED=NO`, no `DEVELOPMENT_TEAM`, and a placeholder bundle id like `com.example.MyApp` compiles and runs in the Simulator, then fails on device with *"Signing for 'MyApp' requires a development team"* or *"Unable to install — the code signature is invalid."*

**Fix:** never disable signing for a shipping app. Simulator builds don't *need* signing, but leaving signing enabled with a valid team works for **both** Simulator and device, so keep it on always.

---

## Anatomy of an Xcode project on disk

```
MyApp/
├─ MyApp.xcodeproj/
│  ├─ project.pbxproj                 # the project graph (targets, files, build settings)
│  ├─ project.xcworkspace/            # implicit workspace + SwiftPM resolution
│  │  └─ xcshareddata/swiftpm/Package.resolved
│  └─ xcshareddata/xcschemes/         # SHARED schemes (commit these)
│     └─ MyApp.xcscheme
├─ MyApp/
│  ├─ MyAppApp.swift                  # @main App entry
│  ├─ ContentView.swift
│  ├─ Info.plist                      # optional in modern projects (see below)
│  ├─ MyApp.entitlements              # capabilities (created when you add one)
│  └─ Assets.xcassets/                # AppIcon, colors, images
└─ MyAppTests/ , MyAppUITests/        # test targets (optional)
```

- **`.xcodeproj` is a bundle (folder)**, not a file. The real data is `project.pbxproj`.
- **`.xcworkspace`** appears even without a manual workspace; Xcode uses it for SwiftPM. When SwiftPM packages exist, open the `.xcodeproj` (or a `.xcworkspace` if you made one) — never build a bare target that ignores package resolution.
- **Shared schemes** live in `xcshareddata/xcschemes/`. Only shared schemes are visible to `xcodebuild` and CI. User-specific schemes hide under `xcuserdata/` (gitignored).

---

## project.pbxproj basics

`project.pbxproj` is an **OpenStep/NeXT-style plist** (old-style, brace-and-semicolon syntax — *not* XML). It's a flat dictionary of objects keyed by 24-char hex UUIDs, forming a graph.

**Top-level shape:**

```
// !$*UTF8*$!
{
  archiveVersion = 1;
  objectVersion = 77;              // see version table below
  classes = { };
  objects = {
    /* ... every project object, keyed by UUID ... */
  };
  rootObject = 1A2B... /* Project object */;
}
```

`objectVersion` tracks the format the generating Xcode wrote (higher = newer). Rough map: `54`≈Xcode 13, `56`≈Xcode 14/15, `60`≈Xcode 15.3, `70`≈Xcode 16.0, `77`≈Xcode 16.1+/26. Exact numbers matter mainly to third-party parsers (e.g. CocoaPods' `Xcodeproj`) that reject versions they don't recognize — don't hand-bump it.

**Key object types (`isa`) you'll see:**

| `isa` | Role |
|---|---|
| `PBXProject` | Root. Points at targets, main group, build config list. |
| `PBXNativeTarget` | A buildable target (the app). Has build phases + config list. |
| `XCConfigurationList` | Ordered set of build configs (Debug/Release) for a project or target. |
| `XCBuildConfiguration` | One config (e.g. Debug) + its `buildSettings` dict + optional `baseConfigurationReference` (an `.xcconfig`). |
| `PBXGroup` / `PBXFileSystemSynchronizedRootGroup` | Folder structure. Xcode 16+ uses **synchronized groups** that mirror the filesystem — you no longer list every file. |
| `PBXFileReference` | A file on disk (source, plist, asset catalog). |
| `PBXBuildFile` | Links a file reference into a build phase. |
| `PBXSourcesBuildPhase` / `PBXResourcesBuildPhase` / `PBXFrameworksBuildPhase` | Compile / copy-resources / link phases. |
| `PBXShellScriptBuildPhase` | Run-script phase (custom build steps). |

**Rules for editing pbxproj:**

- **Prefer not to hand-edit.** Change build settings in Xcode's Build Settings UI, via `.xcconfig` files, or scripted with a library. Hand edits are merge-conflict magnets and easy to corrupt (a dropped semicolon breaks the whole project).
- If you must automate, prefer `.xcconfig` files (plain text, diffable) over touching `objects`.
- **UUIDs must stay unique and referenced.** Orphaned or duplicate UUIDs cause "The project is damaged."
- Xcode 16+ **synchronized folders** mean new `.swift` files under a target's synchronized group are auto-included — you don't add `PBXBuildFile` entries. Great for agents: create the file in the target's folder and it's picked up on next build.

---

## Targets & schemes

**Target** = a product to build (app, extension, framework, test bundle). Each target owns:
- Build phases (Compile Sources, Copy Bundle Resources, Link Binary, Run Scripts).
- A build-config list (Debug/Release) with a `buildSettings` dict.
- Dependencies on other targets.

**Scheme** = *how* to build/run a set of targets. Defines the Run/Test/Profile/Archive actions, which configuration each action uses, environment variables, and launch args.

- Run (debug) defaults to the **Debug** configuration; Archive defaults to **Release**.
- **Share the scheme** (checkbox in *Product → Scheme → Manage Schemes*, or place the `.xcscheme` in `xcshareddata`) so `xcodebuild -scheme` and CI can see it.
- List what a project exposes:

```bash
xcodebuild -list -project MyApp.xcodeproj
# or, with a workspace:
xcodebuild -list -workspace MyApp.xcworkspace
```

---

## Bundle identifier — use a real reverse-DNS id

The bundle id is the app's globally unique identity. It ties together the App ID, provisioning profile, entitlements, keychain, and App Store record.

- **Format:** reverse-DNS, segments from `A–Z a–z 0–9` and hyphens: `com.yourcompany.myapp`.
- **Use a domain you control** (or a plausible personal one like `com.pchordia.todos`). **Never ship** `com.example.*`, `com.apple.*`, `org.reactjs.*`, or a template default — Apple reserves some prefixes and collisions block registration.
- Set by `PRODUCT_BUNDLE_IDENTIFIER`; surfaced to the OS via `CFBundleIdentifier = $(PRODUCT_BUNDLE_IDENTIFIER)`.
- With **Automatic signing**, Xcode auto-creates a matching **App ID** in your account the first time you build to device. Changing the bundle id later creates a new App ID and a new provisioning profile.
- Keep it stable — it *is* the app's identity on device and in the Store.

---

## Info.plist — structure & essential keys

Modern Xcode-generated apps often have **no `Info.plist` file**: keys live under **Build Settings → Info.plist Values** (`INFOPLIST_KEY_*`) with `GENERATE_INFOPLIST_FILE = YES`, and Xcode synthesizes the plist at build time. You add a physical `Info.plist` only when you need keys with no `INFOPLIST_KEY_*` equivalent (e.g. `CFBundleURLTypes`, `UIBackgroundModes`, `NSAppTransportSecurity`, `CFBundleDocumentTypes`).

Point the target at a file with `INFOPLIST_FILE = MyApp/Info.plist`. A physical `Info.plist` (XML plist) looks like:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>               <string>$(PRODUCT_NAME)</string>
    <key>CFBundleDisplayName</key>        <string>My App</string>
    <key>CFBundleIdentifier</key>         <string>$(PRODUCT_BUNDLE_IDENTIFIER)</string>
    <key>CFBundleShortVersionString</key> <string>$(MARKETING_VERSION)</string>
    <key>CFBundleVersion</key>            <string>$(CURRENT_PROJECT_VERSION)</string>
    <key>LSRequiresIPhoneOS</key>         <true/>
    <key>UILaunchScreen</key>             <dict/>
</dict>
</plist>
```

**Version keys — get these right, they're checked at install/upload:**
- `CFBundleShortVersionString` (`MARKETING_VERSION`) — user-facing, e.g. `1.2.0`.
- `CFBundleVersion` (`CURRENT_PROJECT_VERSION`) — build number, must increase per App Store Connect upload within a marketing version, e.g. `42`.

**Common structural keys:**
- `LSRequiresIPhoneOS` → `true` for iOS apps.
- `UILaunchScreen` → empty `<dict/>` is a valid modern launch screen (blank). Without a launch screen (storyboard or this key) the app may run in a compatibility letterbox at a smaller size.
- `UISupportedInterfaceOrientations` → array of allowed orientations.
- `UIApplicationSceneManifest` → only needed for the UIKit scene lifecycle. **A pure SwiftUI `App` does not need it** — SwiftUI manages scenes for you. Omit it unless you have a UIKit `UISceneDelegate`.
- `ITSAppUsesNonExemptEncryption` → set `<false/>` if you use only exempt crypto (e.g. HTTPS); skips the export-compliance prompt on every TestFlight/Store upload.

> With `GENERATE_INFOPLIST_FILE = YES`, all of the above have `INFOPLIST_KEY_*` build-setting equivalents (e.g. `INFOPLIST_KEY_LSRequiresIPhoneOS`, `INFOPLIST_KEY_UIApplicationSceneManifest_Generation`), so an agent can set them without authoring a plist file at all.

---

## Usage-description strings (the ones that crash you if missing)

If your app calls an API that touches protected data or hardware, iOS shows a permission prompt — and **requires a purpose string** in the app's `Info.plist`. If the key is **absent, iOS terminates the app** (a hard crash, not a silent denial) the instant you request access. This is a top cause of "works in review, then crashes."

Add them as `Info.plist` keys, or as `INFOPLIST_KEY_<Key> = "..."` build settings.

| Key | Triggered by | Example string |
|---|---|---|
| `NSCameraUsageDescription` | `AVCaptureDevice`, camera capture | "Take photos to attach to your notes." |
| `NSMicrophoneUsageDescription` | audio recording | "Record voice memos." |
| `NSPhotoLibraryUsageDescription` | read Photos via `PHPhotoLibrary` (only needed for the classic picker; `PhotosPicker`/`PHPickerViewController` need **no** string) | "Choose photos to import." |
| `NSPhotoLibraryAddUsageDescription` | save-only to Photos | "Save exported images to your library." |
| `NSLocationWhenInUseUsageDescription` | `CLLocationManager` foreground | "Show nearby places." |
| `NSLocationAlwaysAndWhenInUseUsageDescription` | background location | "Alert you when you arrive." |
| `NSContactsUsageDescription` | `CNContactStore` | "Find friends already using the app." |
| `NSCalendarsFullAccessUsageDescription` | EventKit read+write (iOS 17+) | "Add and view events on your calendar." |
| `NSCalendarsWriteOnlyAccessUsageDescription` | EventKit add-only (iOS 17+) | "Add events to your calendar." |
| `NSFaceIDUsageDescription` | `LAContext` Face ID | "Unlock with Face ID." |
| `NSBluetoothAlwaysUsageDescription` | Core Bluetooth | "Connect to your device." |
| `NSLocalNetworkUsageDescription` | LAN discovery / Bonjour | "Discover devices on your network." |
| `NSUserTrackingUsageDescription` | App Tracking Transparency (`ATTrackingManager`) | "Personalize the ads you see." |
| `NSMotionUsageDescription` | Core Motion | "Count your steps." |
| `NSSpeechRecognitionUsageDescription` | `SFSpeechRecognizer` | "Transcribe your dictation." |

> The old `NSCalendarsUsageDescription` still works for the legacy `requestAccess(to:)` API, but on iOS 17+ prefer the split full-access / write-only keys with `requestFullAccessToEvents()` / `requestWriteOnlyAccessToEvents()`.

**Rules:**
- The string must be a **real, specific human sentence**. App Review rejects generic/empty strings like "Required." or "$(PRODUCT_NAME) needs access."
- The prompt fires the **first time** you request access. Request permission lazily, near the feature, not at launch.
- Declaring the key is separate from requesting access — you need both.

```swift
import AVFoundation

func requestCameraAccess() async -> Bool {
    // Requires NSCameraUsageDescription in Info.plist, or the app crashes here.
    await AVCaptureDevice.requestAccess(for: .video)
}
```

```swift
import EventKit

func requestCalendarWriteAccess() async throws -> Bool {
    // iOS 17+: needs NSCalendarsWriteOnlyAccessUsageDescription.
    try await EKEventStore().requestWriteOnlyAccessToEvents()
}
```

```swift
import CoreLocation

@MainActor
final class LocationProvider: NSObject, CLLocationManagerDelegate {
    private let manager = CLLocationManager()

    override init() {
        super.init()
        manager.delegate = self
    }

    func start() {
        // Needs NSLocationWhenInUseUsageDescription.
        manager.requestWhenInUseAuthorization()
    }

    // Modern (iOS 14+) authorization callback — reads status off the manager.
    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        switch manager.authorizationStatus {
        case .authorizedWhenInUse, .authorizedAlways:
            manager.startUpdatingLocation()
        default:
            break
        }
    }
}
```

---

## Capabilities & entitlements

**Entitlements** are signed key/value permissions baked into the app's code signature. They grant access to system services (App Groups, Keychain sharing, Push, iCloud, HealthKit, associated domains). They live in a `.entitlements` plist and are **enforced by the OS on device** — an entitlement the provisioning profile doesn't authorize causes install/launch failure.

**How it fits together:** add a Capability in Xcode's *Signing & Capabilities* tab → Xcode (a) writes the entitlement into `MyApp.entitlements`, (b) sets `CODE_SIGN_ENTITLEMENTS = MyApp/MyApp.entitlements`, and (c) with Automatic signing, enables the matching service on your App ID + regenerates the provisioning profile. Skip any of these and you get *"Provisioning profile doesn't include the … entitlement."*

Example `MyApp.entitlements`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.application-groups</key>
    <array>
        <string>group.com.yourcompany.myapp</string>
    </array>
    <key>aps-environment</key>
    <string>development</string>
    <key>keychain-access-groups</key>
    <array>
        <string>$(AppIdentifierPrefix)com.yourcompany.myapp</string>
    </array>
</dict>
</plist>
```

**Common entitlement keys:**
- `com.apple.developer.icloud-container-identifiers` + `com.apple.developer.icloud-services` — iCloud/CloudKit.
- `com.apple.security.application-groups` — share data (`UserDefaults(suiteName:)`, shared container) between the app and its extensions.
- `aps-environment` = `development` / `production` — Push Notifications.
- `com.apple.developer.associated-domains` — Universal Links / web credentials.
- `com.apple.developer.healthkit`, `.homekit`, `.networking.wifi-info`, `.family-controls`.

**Free (personal) Apple ID limits:** signing with a free account works on device but **cannot** use Push, App Groups, associated domains, iCloud, etc.; profiles expire after ~7 days; and you're capped at ~3 apps and a few registered devices. For real capabilities you need a **paid Apple Developer Program** membership.

---

## Code signing — the correct setup for a physical iPhone

### Automatic signing (recommended, and what agents should generate)

```
CODE_SIGN_STYLE                = Automatic
DEVELOPMENT_TEAM               = ABCDE12345      // 10-char Team ID (developer.apple.com → Membership)
CODE_SIGN_IDENTITY             = Apple Development
CODE_SIGNING_ALLOWED           = YES
CODE_SIGNING_REQUIRED          = YES
PROVISIONING_PROFILE_SPECIFIER =                 // leave EMPTY for automatic
```

With Automatic, Xcode manages the signing certificate and an "Xcode Managed Profile" for you. Requirements on device:
1. `DEVELOPMENT_TEAM` set to a real Team ID.
2. That team's account added in **Xcode → Settings → Accounts** (for GUI) or credentials available to `xcodebuild -allowProvisioningUpdates`.
3. The iPhone **plugged in, unlocked, and "Trust This Computer" accepted**, so Xcode can register its UDID.
4. Bundle id matches an App ID the account can create or use.

**Do not set `PROVISIONING_PROFILE_SPECIFIER` (or the deprecated `PROVISIONING_PROFILE`) when using Automatic** — a stale specifier is a classic signing failure. Leave them empty.

### Manual signing (CI / enterprise / reproducible release)

```
CODE_SIGN_STYLE                = Manual
CODE_SIGN_IDENTITY             = Apple Distribution: Your Co (ABCDE12345)
PROVISIONING_PROFILE_SPECIFIER = MyApp App Store Profile
DEVELOPMENT_TEAM               = ABCDE12345
```

Manual requires you to install the cert (`.p12`) and profile (`.mobileprovision`) yourself. Prefer Automatic for development; use Manual only when you need deterministic, checked-in profiles.

### The settings that must be YES

- `CODE_SIGNING_ALLOWED = YES` — if `NO`, Xcode produces an **unsigned** binary; the device won't install it. Templates sometimes force `NO` for Simulator convenience — never do this for the app target.
- `CODE_SIGNING_REQUIRED = YES` — enforces that signing actually happens. Setting it `NO` masks misconfiguration and yields an unsignable/uninstallable build.

Xcode automatically **relaxes signing for the `iphonesimulator` SDK** (the Simulator doesn't verify signatures), so keeping both `YES` still lets Simulator builds succeed. Keep them `YES` everywhere.

### First device build checklist

1. Real reverse-DNS `PRODUCT_BUNDLE_IDENTIFIER`.
2. `DEVELOPMENT_TEAM` = your Team ID.
3. `CODE_SIGN_STYLE = Automatic`, profile specifier empty.
4. Both signing flags `YES`.
5. Add usage-description strings for every permission you'll request.
6. Add Capabilities in the *Signing & Capabilities* tab (not by hand-editing entitlements).
7. iPhone connected, unlocked, trusted; **Developer Mode enabled** (Settings → Privacy & Security → Developer Mode, iOS 16+; requires a reboot).
8. Build with `-allowProvisioningUpdates`.

---

## Building from the command line with xcodebuild

**Discover schemes / destinations:**

```bash
xcodebuild -list -project MyApp.xcodeproj
xcodebuild -showdestinations -scheme MyApp        # list attached devices/simulators
```

**Build & test in the Simulator (no signing needed):**

```bash
xcodebuild \
  -scheme MyApp \
  -destination 'platform=iOS Simulator,name=iPhone 16,OS=latest' \
  build
```

**Build for a physical device (signing enforced):**

```bash
xcodebuild \
  -scheme MyApp \
  -configuration Debug \
  -destination 'platform=iOS,name=Priyansh’s iPhone' \
  -allowProvisioningUpdates \
  build
```

Address a device by name, or by exact `id` (UDID) for scripts:

```bash
-destination 'platform=iOS,id=00008120-000A1B2C3D4E002E'
-destination 'generic/platform=iOS'   # any arm64 device; used for archiving
```

**Inspect resolved signing settings (great for debugging):**

```bash
xcodebuild -showBuildSettings -scheme MyApp \
  | grep -E 'CODE_SIGN|DEVELOPMENT_TEAM|PRODUCT_BUNDLE_IDENTIFIER|PROVISIONING'
```

**Override settings on the command line** (last resort; prefer project/xcconfig):

```bash
xcodebuild -scheme MyApp -destination 'platform=iOS,name=iPhone' \
  DEVELOPMENT_TEAM=ABCDE12345 CODE_SIGN_STYLE=Automatic \
  -allowProvisioningUpdates build
```

### Archive & export an `.ipa` (distribution)

```bash
# 1) Archive (Release, generic device destination)
xcodebuild \
  -scheme MyApp \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath build/MyApp.xcarchive \
  -allowProvisioningUpdates \
  archive

# 2) Export a signed .ipa
xcodebuild \
  -exportArchive \
  -archivePath build/MyApp.xcarchive \
  -exportPath build/export \
  -exportOptionsPlist ExportOptions.plist \
  -allowProvisioningUpdates
```

`-exportArchive` **requires** both `-archivePath` and `-exportOptionsPlist`. Minimal `ExportOptions.plist` for App Store / TestFlight:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>method</key>        <string>app-store-connect</string>
    <key>teamID</key>        <string>ABCDE12345</string>
    <key>signingStyle</key>  <string>automatic</string>
    <key>destination</key>   <string>export</string>
    <key>uploadSymbols</key> <true/>
</dict>
</plist>
```

- `method` values (Xcode 15.3+): `app-store-connect` (Store/TestFlight), `release-testing` (ad-hoc), `debugging` (development), `enterprise`. Older Xcode used `app-store`, `ad-hoc`, `development`, still accepted for back-compat.
- Set `destination` to `upload` to send straight to App Store Connect, or `export` to write the `.ipa` to `-exportPath`.
- To install on a device without the Store, use `debugging` / `release-testing` and side-load the `.ipa` with `devicectl` (below).

### Install / launch on a connected device (Xcode 15+, iOS 17+)

```bash
xcrun devicectl list devices                                            # find the device UDID
xcrun devicectl device install app --device <UDID> build/export/MyApp.ipa
xcrun devicectl device process launch --console --terminate-existing \
  --device <UDID> com.yourcompany.myapp
```

`--console` streams the app's stdout/stderr to your terminal; `--terminate-existing` kills a running instance first. `devicectl` replaces the deprecated `instruments`/`ios-deploy` paths and is the first-party way to install and launch on hardware from the CLI (`ios-deploy` no longer works on iOS 17+).

---

## Recommended: drive settings from an .xcconfig

`.xcconfig` files are plain text, diffable, and keep signing config out of the noisy pbxproj. Attach one via each `XCBuildConfiguration`'s `baseConfigurationReference` (Xcode: *Project → Info → Configurations*).

`Config/App.xcconfig`:

```
// App.xcconfig — shared build settings
PRODUCT_NAME = MyApp
PRODUCT_BUNDLE_IDENTIFIER = com.yourcompany.myapp
MARKETING_VERSION = 1.0.0
CURRENT_PROJECT_VERSION = 1

IPHONEOS_DEPLOYMENT_TARGET = 17.0
SWIFT_VERSION = 5.9
TARGETED_DEVICE_FAMILY = 1,2          // 1=iPhone, 2=iPad

DEVELOPMENT_TEAM = ABCDE12345
CODE_SIGN_STYLE = Automatic
CODE_SIGN_IDENTITY = Apple Development
CODE_SIGNING_ALLOWED = YES
CODE_SIGNING_REQUIRED = YES

// Info.plist synthesized from build settings (no physical file needed):
GENERATE_INFOPLIST_FILE = YES
INFOPLIST_KEY_UILaunchScreen_Generation = YES
INFOPLIST_KEY_NSCameraUsageDescription = Take photos to attach to your notes.
INFOPLIST_KEY_CFBundleDisplayName = My App
```

`GENERATE_INFOPLIST_FILE = YES` + `INFOPLIST_KEY_*` is the modern default and lets an agent set usage strings **without** authoring a plist file. Note: `.xcconfig` values are literal — no surrounding quotes needed, and a value runs to end of line.

---

## Minimal, compilable SwiftUI app entry

```swift
import SwiftUI

@main
struct MyAppApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

struct ContentView: View {
    @State private var count = 0
    var body: some View {
        VStack(spacing: 16) {
            Text("Taps: \(count)")
                .font(.largeTitle.monospacedDigit())
            Button("Tap me") { count += 1 }
                .buttonStyle(.borderedProminent)
        }
        .padding()
    }
}

#Preview {
    ContentView()
}
```

A deployment target of 17.0 lets you use `@Observable`, `ContentUnavailableView`, `.scrollTargetBehavior`, `SwiftData`, and other iOS 17 APIs directly.

---

## Failure → cause → fix

| Symptom | Cause | Fix |
|---|---|---|
| *"Signing for 'MyApp' requires a development team."* | `DEVELOPMENT_TEAM` unset. | Set the Team ID; select the team in Signing & Capabilities. |
| *"Unable to install… code signature invalid"* | Unsigned/ad-hoc binary (`CODE_SIGNING_ALLOWED=NO`) or Simulator SDK slice. | Enable signing; build for `iphoneos`, not the Simulator. |
| *"No profiles for 'com.x.y' were found."* | Bundle id has no App ID / profile. | Use Automatic + `-allowProvisioningUpdates`; use a real bundle id. |
| *"…doesn't include the '\<entitlement\>' entitlement."* | Capability in entitlements but not enabled on the App ID/profile. | Add the Capability via Xcode's UI so the profile regenerates; rebuild. |
| App **crashes immediately** on tapping a permission feature | Missing `NS…UsageDescription`. | Add the usage-description key. |
| *"This app cannot be installed because its integrity could not be verified."* | Profile expired (free account, ~7 days) or device UDID not in profile. | Re-sign; register the device; consider paid membership. |
| `xcodebuild` can't find the scheme | Scheme not shared. | Share the scheme (`xcshareddata/xcschemes`). |
| Device not offered as a destination | iPhone locked, untrusted, or Developer Mode off. | Unlock, Trust, enable Developer Mode (iOS 16+), reboot. |

---

## Rules of thumb for an AI build agent

- Always set a **real reverse-DNS bundle id** and a **`DEVELOPMENT_TEAM`**; never leave `com.example.*`.
- Keep **`CODE_SIGN_STYLE=Automatic`**, **`CODE_SIGNING_ALLOWED=YES`**, **`CODE_SIGNING_REQUIRED=YES`** — do **not** disable signing to "make it build."
- Add a **usage-description string** for *every* permission API the code calls, before the code runs.
- Add capabilities through the **Signing & Capabilities** flow so the App ID + profile stay in sync; don't hand-edit entitlements in isolation.
- Prefer **`GENERATE_INFOPLIST_FILE=YES` + `INFOPLIST_KEY_*`** and **`.xcconfig`** files over hand-editing `project.pbxproj`.
- Build device targets with **`-allowProvisioningUpdates`**; verify with `xcodebuild -showBuildSettings | grep CODE_SIGN`.
- Set `IPHONEOS_DEPLOYMENT_TARGET = 17.0` and `SWIFT_VERSION = 5.9` (or 6.0) explicitly.
