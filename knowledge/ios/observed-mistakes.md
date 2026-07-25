<!-- keywords: observed mistakes, real build defects, overlapping text, layout collision, text overlaps values, labels over numbers, cluttered layout, data-positioned layout collision, timeline label collision, screenshot defects, fake completeness, interface lies, sample data as real, generic anti-patterns from shipped builds -->

# Observed Mistakes — Real Defects Caught in Shipped Builds

This is a curated, growing log of **actual defects observed in apps this factory
built**, each generalized into a rule so the same class of mistake is not
reproduced in a future round. These are not hypothetical — every entry was seen
in a real generated app, screenshotted or reported. Read this before building
UI: if your screen could exhibit any of these, fix it before you finish.

Each entry is: **what was seen → the generic rule → how to avoid it.**

---

## M-001 · Overlapping text (labels and values drawn on top of each other)

**What was seen (Gloam, a solar "day ribbon" app):** the screen positioned each
event's label + time by the sun's actual time-of-day along a vertical gradient.
At a high-latitude location (Reykjavík) where sunrise (3:55 AM) and golden hour
(5:18 AM) fall close together, the two blocks were placed so close that
"GOLDEN HOUR" rendered directly ON TOP of the "3:55 AM" numerals, and at the
bottom three time cards ("4:44 PM", "19h 15m of daylight", "9:47 PM") collided
into an unreadable stack. The app compiled and ran; it was simply broken to look
at.

**Generic rule:** **Text and controls must never overlap, collide, or clip.** A
label must never sit on top of a value; two values must never stack into each
other.

**How to avoid it — especially for DATA-POSITIONED layouts** (anything placed by
a data value: event times on a timeline, points on an axis/chart, markers on a
gradient or map):

- Never place two elements at raw computed positions without a **collision
  check**. Compute positions, then enforce a **minimum gap** between adjacent
  elements.
- When two elements would collide (values close together; a dense/edge-case
  dataset like a high-latitude day, a busy hour, many records at once),
  **de-clutter deterministically**: offset one, group them into a single
  combined chip, collapse to a count ("+2"), or drop the lower-priority label —
  but NEVER draw one over another.
- Test the layout with the **worst realistic data**, not the tidy demo case:
  events bunched together, the longest label, the largest Dynamic Type size,
  the smallest supported window. "Looks right in the mock" is not the bar.
- Prefer a real layout container (VStack/HStack/Grid/Layout with spacing) over
  absolute `.position`/`.offset` for anything that carries text. If you must
  position by value, wrap it in logic that guarantees separation.

**A screen where text overlaps is a broken screen — it fails visual QA and must
not ship.**

---

## M-002 · Claiming an outcome that did not happen / sample data shown as real

**Rule (from the quality rulebook, reinforced here because it is easy to fake):**
show "Saved" / "Deleted" / "Sent" / "Synced" / "Unlocked" only AFTER the real
operation is confirmed — never unconditionally, never a success animation played
regardless of the result. When there is no real user data yet, show an honest
empty state ("No sessions yet — complete your first to see your history"), never
fabricated stats presented as the user's own. A control that does nothing, or a
toggle that only flips its own appearance, is a defect, not a placeholder —
implement it or visibly disable it with a reason.

---

## M-003 · A label and a nearby control/button overlap at a screen edge

**What was seen (Gloam, rebuilt):** the ribbon's *time anchors* were correctly
de-cluttered (M-001's fix held — sunrise/golden-hour no longer collide), but at
the bottom of the screen the "SUNSET" label and the "SHARE" button were drawn on
top of each other ("SUN̶S̶E̶T̶/SHARE"). The collision-avoidance had been applied to
the one region it was designed for (the 1-D ribbon axis) and NOT to the
separate label-plus-control cluster at the screen edge.

**Generic rule:** overlap avoidance is not "solve it once for one component."
EVERY cluster of text + controls must be checked for collisions — a label and
its adjacent button, a caption under a value, a footer that meets a floating
action control. Fixing overlap in the hero component does not fix it elsewhere
on the same screen.

**How to avoid it:**
- After building a screen, audit EACH region independently for overlap, not just
  the one you know is dense. A label sitting beneath/over a button at the safe-
  area edge is a common blind spot.
- Give paired label+control elements a real layout container with explicit
  spacing (VStack/HStack with spacing, or a grid), never absolute positions that
  can coincide.
- Reserve space for a floating/pinned control (e.g. a Share button) so
  scrolling or edge content can't slide under it.

---

## M-004 · Missing launch-screen config letterboxes the whole app

**What was seen (steep, 2026-07-18):** the generated app target's Info.plist
had no `UILaunchScreen` key (and no LaunchScreen storyboard, and no
`INFOPLIST_KEY_UILaunchScreen_Generation` build setting). iOS treats such an
app as legacy-sized and LETTERBOXES it: on a 912pt-tall device the app got a
630pt-tall window with black bands above and below. The timer screen's layout
overflowed the shrunken window and its Start button rendered ~32pt BELOW the
window's bottom edge — visible in screenshots as a cropped, unfinished screen,
and untappable by any user or UI test. Every declared flow through the timer
failed while the Swift layout code itself was "correct".

**Generic rule:** an iOS app target MUST declare launch-screen configuration —
`UILaunchScreen` (an empty dict is enough) in the app's Info.plist, or a
LaunchScreen storyboard, or `INFOPLIST_KEY_UILaunchScreen_Generation` when the
Info.plist is generated. Without it, nothing about the layout can be trusted:
the app runs in a smaller window than the device on modern iPhones.

**How to avoid it:**
- Always include `<key>UILaunchScreen</key><dict/>` (at minimum) in the app
  target's Info.plist, or the equivalent generated-plist build setting.
- Never treat the letterboxed window as a layout constraint to design around —
  fix the plist, don't shrink the design.
- The `designlint` gate now hard-errors (`missing_launch_screen`) when an
  .xcodeproj app has no launch-screen configuration anywhere.

---

## M-00N · `.accessibilityIdentifier` placed on a TabView's content instead of its tab button

An app with a 3-tab `TabView` set each tab's identifier like this:

```swift
NavigationStack(path: $statsPath) { StatsRootView() }
    .tabItem { Label("Stats", systemImage: "chart.bar") }
    .tag(Tab.stats)
    .accessibilityIdentifier("tabBar.statsTab")   // WRONG: attaches to the content view
```

The UI crawl could not find `tabBar.statsTab` as a tappable element and
failed 4 of 13 declared user flows — every flow whose first step was
switching to the Stats or Settings tab. Two separate repair rounds "fixed"
unrelated things and left this misplacement untouched because the identifier
genuinely *was* set somewhere in the file; it was just attached to the wrong
view. `xcodebuild` and Swift's type checker have no way to catch this — the
code compiles and runs fine, it just isn't testable/tappable-by-identifier.

**Generic rule:** in a `TabView`, `.accessibilityIdentifier` chained after
`.tabItem { ... }` attaches to the tab's CONTENT (what shows when the tab is
selected), not to the tab bar button. The tab bar button is a separate,
UIKit-bridged control synthesized from the `.tabItem` closure. To make a tab
bar button itself discoverable (by XCUITest, an accessibility inspector, or
this factory's UI crawl), the identifier must be INSIDE the `.tabItem`
closure, on the `Label` (or icon/text) itself:

```swift
.tabItem {
    Label("Stats", systemImage: "chart.bar")
        .accessibilityIdentifier("tabBar.statsTab")   // RIGHT
}
.tag(Tab.stats)
```

**How to avoid it:** any accessibilityIdentifier meant to make a *tab bar
button* tappable-by-ID must be set on the Label inside `.tabItem { }`, never
chained onto the tab's content view/NavigationStack. This is easy to miss
because both placements compile and both "look" like they're identifying the
tab — only one is discoverable as the tab bar control.

**UPDATE (2026-07-24, two more apps, same night):** the "move it inside
`.tabItem`" fix above is NOT reliable — it is genuinely inconsistent across
builds. Two separate apps (Fieldnotes, Formcheck) had the identifier
correctly placed INSIDE `.tabItem { Label(...).accessibilityIdentifier(...) }`
exactly as prescribed above, verified via fresh `xcodebuild` installs and the
UI crawl's own runner tool directly — and `app.tabBars.buttons` still
resolved an EMPTY `identifier` for the tab (confirmed: other, non-tab-bar
buttons in the same builds resolved their identifiers fine). One of those
app's own build history shows agents already discovered this and DELIBERATELY
reverted to the "wrong" chained placement, having found empirically that
nested-in-Label was what didn't work for *their* build. In other words:
either placement can fail, unpredictably, and neither is a dependable fix.

**Real fix: stop depending on `accessibilityIdentifier` for TabView tab
buttons in flow contracts at all.** Write declared-flow `tap` steps against
the tab's stable, on-screen **label text** ("Settings", "Timeline") instead
of an identifier token. `locate()`'s existing exact-label-match pass already
handles this with no crawler changes needed — labels are what's actually
reliable here, identifiers on TabView tab items are not. Keep
`.accessibilityIdentifier` in the Label as a best-effort (VoiceOver users and
some tooling still benefit when it happens to work) but do not architect a
flows.json contract, or any other automated check, around it resolving.

_Append new entries as real defects are observed. Keep each one concrete
(what was seen) plus a GENERIC rule, so it transfers to unrelated apps._
