# The Vibe Coding Quality Rulebook
## Rules for Building AI-Assisted Apps That Behave Like Real Products

_Source document adopted 2026-07-16 as the standard the apps this orchestrator
builds are held to. This is the rulebook verbatim, as given. See
[QUALITY_RULES.md](QUALITY_RULES.md) for how each rule maps onto the engine's
actual enforcement mechanisms (build prompt vs. mechanical gate) — that is the
living, honest status doc; this file is the fixed reference it maps against._

### Purpose

Vibe coding is exceptionally good at producing visible progress quickly. It
can generate screens, models, navigation, animations, and entire feature flows
in a fraction of the time normally required.

Its most common failure is not that the code does nothing.

The more dangerous failure is that the app appears complete while its behavior
is only partially implemented.

A polished screen may:

- Break when the window is resized.
- Display sample data as though it were real.
- Show an error after an operation succeeded.
- Lose user data after relaunch.
- Allow the same action to execute multiple times.
- Contain controls that do not actually change anything.
- Work only with the exact test data used during development.
- Fail when permissions are denied.
- Look correct only on the developer's device.
- Quietly discard user input.
- Claim that something was saved, shared, synced, or deleted when it was not.

This rulebook exists to prevent **false completeness**.

---

## 1. The Core Principle

### A feature is not complete when the screen exists.

A feature is complete only when:

1. The interface adapts correctly.
2. The underlying behavior is real.
3. State transitions are predictable.
4. Data survives as expected.
5. Errors are handled correctly.
6. Empty, loading, offline, and permission-denied states work.
7. The feature is accessible.
8. The feature works with realistic and adversarial input.
9. The user is never misled about what happened.
10. The feature has been tested outside the happy path.

The app must not merely demonstrate the intended experience. It must survive
normal use.

---

## 2. The Ten Non-Negotiable Rules

### Rule 1: Never build only the happy path

Every operation must account for: Success · Failure · Empty results · Loading
· Cancellation · Retry · Duplicate actions · Interrupted actions · Missing
permissions · Missing or corrupted data

A successful demo is not evidence that a feature is production-ready.

### Rule 2: The interface must never lie

The app must not tell the user that something happened unless it actually
happened.

Prohibited examples:

- Showing "Saved" before persistence finishes.
- Showing "Deleted" when only the visible row was removed.
- Showing "Message sent" when the app only opened a composer.
- Showing "Synced" when data is still pending.
- Showing "Premium unlocked" without verifying entitlement.
- Showing generated statistics calculated from sample data.
- Displaying a toggle as enabled when the setting is not implemented.
- Showing a success animation regardless of the operation result.

The interface must reflect the true state of the system.

### Rule 3: Sample data must never masquerade as user data

Fake data is allowed only in: Design previews · Unit tests · UI tests ·
Explicit demo mode · Development-only fixtures

Fake data must not silently appear in production when real data is
unavailable. When no real data exists, show an honest empty state.

Bad: "You completed 12 sessions this month."
Better: "No sessions yet. Complete your first session to see your history."

Sample data must be visibly labeled when intentionally presented.

### Rule 4: Every screen must have a complete state model

Every data-driven screen must support at least:

| State | Required behavior |
| ----------------- | -------------------------------------------------- |
| Initial | The screen has not started loading |
| Loading | Progress is visible without blocking unnecessarily |
| Content | Real data is displayed |
| Empty | The absence of data is explained |
| Error | The current operation failed |
| Offline | Connectivity is unavailable, when relevant |
| Permission denied | The user declined required access |
| Partial content | Some data loaded and some failed |
| Stale content | Older data is shown while refreshing |
| Disabled | The feature is intentionally unavailable |

These states should be explicit in code rather than inferred from unrelated
Boolean variables.

Avoid combinations such as: `isLoading = true`, `hasError = true`,
`showEmptyState = true`, `data.count > 0`.

An explicit state model is safer:

```swift
enum ViewState {
    case idle
    case loading
    case loaded([Item])
    case empty
    case failed(AppError)
}
```

### Rule 5: Resizing must cause reflow, not clipping

No screen should assume one exact width or height.

**Common resizing failures**

- Buttons disappear below the window.
- Text overlaps nearby controls.
- Sidebars consume the entire content area.
- Forms retain fixed widths that exceed the window.
- Sheets open larger than the available screen.
- Toolbars wrap incorrectly.
- Content is cut off instead of scrolling.
- Charts become unreadable.
- Images stretch or overflow.
- The layout works in full screen but not split screen.
- The app allows the window to shrink below its usable size.

**Required behavior**

- Use adaptive layouts rather than screen-specific coordinates.
- Establish meaningful minimum window dimensions on macOS.
- Let content reflow when possible.
- Use scrolling when content cannot reasonably shrink.
- Define minimum and maximum widths for important panels.
- Collapse secondary content before primary content becomes unusable.
- Test long text, large text, and translated text.
- Test narrow, short, wide, and full-screen windows.
- Do not rely on one developer monitor or one simulator.

**Mandatory layout test sizes.** At minimum, test: smallest supported window ·
typical laptop window · full-screen desktop window · split-screen width ·
maximum accessibility text size · longest realistic content · empty content ·
keyboard-visible state on mobile.

The test is not "Does it technically render?" The test is "Can the user still
complete the task?"

---

## 3. Visual and Layout Failures

### 3.1 Fixed-size layouts

**Common mistake.** The generated interface uses hardcoded widths, heights,
offsets, and spacer values until it resembles the reference image. This
produces screenshot accuracy but not product quality.

**Rules**

- Avoid hardcoded screen-level coordinates.
- Fixed dimensions are acceptable for icons, controls, thumbnails, and known
  components — not entire layouts.
- Prefer constraints, flexible frames, grids, and adaptive containers.
- Do not use invisible spacers to repair structural layout problems.
- Do not solve every issue with additional padding.
- Make intentional decisions about compression, wrapping, truncation, and
  scrolling.

### 3.2 Content clipping

Text, controls, and images must not disappear without a deliberate reason.
When space is limited: (1) reflow the layout, (2) wrap text, (3) collapse
secondary information, (4) allow scrolling, (5) truncate only low-priority
content, (6) provide access to the complete content where necessary.

Critical instructions, errors, prices, dates, and destructive-action warnings
must not be silently truncated.

### 3.3 Safe areas and system UI

The interface must account for: notches and camera areas · home indicators ·
navigation bars · toolbars · sidebars · tab bars · software keyboards ·
window title bars · menu bars · browser chrome · input accessory views.

Do not place essential controls where system UI can cover them.

### 3.4 Dark mode and appearance changes

Dark mode is not complete merely because the background changes. Check:
contrast · dividers · disabled controls · selected states · input fields ·
images with transparent backgrounds · shadows · charts · maps · modal surfaces
· error and success colors · exported images · app icons and widgets.

Never encode semantic meaning exclusively through color.

### 3.5 Inconsistent components

AI-generated apps often recreate the same component multiple times with
slightly different: padding · corner radii · fonts · colors · button heights ·
icon sizes · shadows · loading behavior.

Shared visual patterns should use shared components and design tokens. A
button called "PrimaryButton" should not have five unrelated implementations.

---

## 4. State and Lifecycle Failures

### 4.1 Stale state

A common failure occurs when a screen continues displaying the result of a
previous operation. Examples: an old error remains after a successful retry ·
a deleted item remains visible · a loading spinner continues after navigation
· an old search result appears for a new query · a success banner appears when
reopening the screen · a form retains another record's values · a previous
user's content remains after logout.

**Rules**

- Reset transient state deliberately.
- Tie errors to the operation that produced them.
- Clear stale errors when a new operation begins.
- Cancel outdated requests.
- Ignore results from operations that are no longer current.
- Reset forms when their record identity changes.
- Treat logout and account switching as full state-boundary events.

### 4.2 Boolean-state explosions

Generated code often contains many loosely related Boolean values:

```text
isLoading
showError
showSuccess
isEmpty
isSaving
didSave
shouldDismiss
```

These can form impossible combinations. Prefer explicit state machines for
complex workflows, e.g.: `idle, editing, validating, saving, saved, failed`.
Every transition should have a clear cause.

### 4.3 Duplicate submissions

Buttons must not trigger an action repeatedly while the action is in
progress. Applies to: purchases · form submissions · file imports · deletes ·
exports · AI generation · email sending · uploads · account creation ·
payments · saving.

**Required protections**

- Disable or debounce the trigger while processing.
- Make backend or persistence operations idempotent where possible.
- Prevent double-taps from creating duplicate records.
- Restore the button after success or failure.
- Give the user visible progress.

### 4.4 Cancellation

Long-running work should be cancellable when practical. Cancellation must not
be treated as an error unless the user needs to know something was left
incomplete.

Do not show "Something went wrong." when the user intentionally closed a
picker, dismissed a sheet, cancelled authentication, or stopped generation.

### 4.5 Relaunch behavior

Test what happens when the app is: closed normally · force-quit · sent to the
background · relaunched during an incomplete operation · updated to a new
version · opened after the device restarts.

The user should not lose committed work or enter an impossible state.

---

## 5. Error Handling Rules

### 5.1 Errors must appear only when relevant

An error should be: caused by a real failure · associated with the current
operation · cleared when no longer applicable · written in language the user
can understand · actionable when an action exists.

Common AI-generated error bugs include: showing an error because an optional
value is absent · displaying an error before the first loading attempt
finishes · showing both content and a full-screen error · leaving an error
visible after a successful retry · displaying the same error in a banner,
alert, toast, and inline label · showing a generic network error when the user
cancelled · treating an empty result as a failure · treating permission denial
as an unexpected exception · displaying internal exception text to the user.

### 5.2 Errors must be specific

Bad: "Error occurred." Better: "The PDF could not be imported because it is
password-protected."

Bad: "Something went wrong." Better: "Your changes could not be saved. The
original version is still available."

Bad: "Network error." Better: "The app could not connect. Check your internet
connection and try again."

### 5.3 Errors must not expose internals

Never display raw: stack traces · database errors · file paths · API payloads
· authentication tokens · model prompts · internal identifiers · SQL errors ·
framework exception names.

Technical details may be logged securely, but user-facing errors must explain
impact and recovery.

### 5.4 Do not use alerts for everything

Choose the presentation based on the failure: inline validation for
field-specific problems · banner for non-blocking screen-level issues · empty
or error state for content that cannot load · alert for decisions requiring
immediate attention · toast for brief confirmation · sheet for complex
recovery or explanation.

Repeated alerts create a hostile experience.

### 5.5 Preserve user work after failure

A failed save or submission must not clear the form. The app should preserve:
entered text · selected files · chosen options · scroll position where
practical · draft content · unsaved edits.

The user should be able to correct the problem and retry.

---

## 6. Data Integrity Rules

### 6.1 Saving must be real

A saved object must survive: navigating away · returning to the screen ·
relaunching the app · refreshing the browser · reopening the document ·
restarting the device, when applicable.

Do not treat in-memory state as persistence.

### 6.2 Never silently discard data

Warn the user before abandoning meaningful unsaved work. This includes:
closing a window · navigating backward · dismissing a sheet · switching
records · logging out · replacing an imported file · starting over.

Autosave may remove the need for warnings, but autosave must actually be
reliable.

### 6.3 Partial saves must be handled deliberately

When a workflow updates multiple objects, determine whether it should be:
atomic (everything succeeds or nothing changes) · incremental (completed
steps remain saved) · recoverable (the app resumes from the incomplete step).

Do not leave the system half-updated without explaining the result.

### 6.4 Deletion must have clear semantics

Determine whether delete means: remove from the visible list · move to trash ·
delete locally · delete from every device · delete from the server · delete
associated files · remove a relationship but retain the record.

The interface must communicate the actual behavior. Destructive actions
should generally include: clear naming · confirmation when consequences are
meaningful · undo where practical · progress for long operations · honest
failure handling.

### 6.5 Schema changes require migration

Updating a model is not complete until existing user data still works. Test:
new required fields · renamed properties · changed data types · deleted
relationships · new uniqueness rules · existing records with missing values ·
older app versions · import files created by previous versions.

Never assume all users begin with a fresh database.

### 6.6 Dates, time zones, currencies, and decimals

These areas frequently appear correct while containing subtle bugs. Rules:
store absolute timestamps consistently · distinguish dates from date-times ·
do not assume the developer's time zone · test daylight-saving transitions ·
use locale-aware formatting · avoid floating-point arithmetic for money ·
define rounding rules explicitly · do not assume a period is always 24 hours ·
test month and year boundaries · clarify whether "today" is based on device
time, account time, or server time.

---

## 7. Forms and Input Validation

### 7.1 Validate at the correct time

Do not show every field as invalid when an untouched form first opens.
Validation may occur: as the user types · when the field loses focus · on
submission · after an external verification step. Choose intentionally.

### 7.2 Validate more than emptiness

Check: leading and trailing whitespace · maximum length · minimum length ·
unsupported characters · duplicate values · invalid formats · numeric bounds ·
decimal precision · future or past date restrictions · file type · file size ·
malformed imported content · conflicting selections.

Do not rely solely on the keyboard type or visible control to enforce
validity.

### 7.3 Buttons must reflect whether the action is available

A button should not appear functional when: required fields are missing · the
operation is already running · permission is unavailable · no changes have
been made · the selected content is incompatible · the user lacks
entitlement · the feature is unsupported on the current platform.

When disabled, the reason should be understandable where it is not obvious.

### 7.4 Keyboard and focus behavior

Check: the keyboard does not cover the active field · the user can dismiss the
keyboard · Return and Tab behavior is sensible · focus advances logically ·
forms can be completed with hardware keyboards · focus is restored after
validation errors · macOS and web users can navigate without a mouse ·
pressing Enter does not accidentally submit destructive actions.

---

## 8. Navigation Rules

### 8.1 Every destination must be reachable and escapable

No screen should trap the user. Check: back behavior · dismiss behavior ·
cancel behavior · deep links · tab switching · sidebar selection · window
restoration · browser back and forward · navigation after deletion ·
navigation after logout.

### 8.2 Do not duplicate destinations accidentally

AI-generated navigation often stacks the same detail screen repeatedly.
Prevent: opening the current record again · double-pushing on repeated taps ·
presenting multiple identical sheets · combining modal and push navigation for
the same action · leaving hidden screens active behind replacements.

### 8.3 Preserve context

When returning from a detail view, retain useful context such as: scroll
position · search query · filters · sort order · selected tab · expanded
sections. Do not reset the entire experience unnecessarily.

### 8.4 Deep links must fail safely

When a link references missing, deleted, restricted, or malformed content: do
not crash · do not show an unrelated default record · explain that the
destination is unavailable · offer a safe route back into the app.

---

## 9. Permissions and Privacy

### 9.1 Ask for permission in context

Do not request every permission during first launch. Explain the benefit
immediately before the system prompt. Examples: ask for camera access when the
user starts scanning · ask for location when enabling a location feature ·
ask for notifications when the value is clear · ask for photo access when
importing or saving an image.

### 9.2 Permission denial is a normal state

The app must work gracefully when a user declines. Provide: an explanation of
what will not work · alternative behavior where possible · a route to
settings when appropriate · no repeated harassment. Do not repeatedly display
the system prompt or block unrelated functionality.

### 9.3 Collect only necessary data

Every collected field should have a product reason. Do not log or transmit
sensitive information merely because it is convenient. Avoid placing the
following in analytics or logs: user-entered private text · authentication
credentials · full document contents · health information · financial
information · precise location · contact details · AI prompts containing
personal data · access tokens · uploaded files.

### 9.4 Secrets never belong in client code

Do not place production secrets in: source files · public repositories · app
bundles · JavaScript shipped to browsers · example configuration files · logs
· screenshots · prompts sent to coding agents. Assume anything shipped to a
client can be extracted.

---

## 10. Accessibility Rules

Accessibility is not an optional final cleanup phase. Every interactive
element must support: a meaningful accessible name · a correct role · a clear
value or state · logical focus order · keyboard or switch navigation where
applicable · sufficient contrast · large text · reduced motion · non-color
indicators · touch targets of reasonable size.

Test: screen reader navigation · maximum text size · keyboard-only navigation
· reduced-motion mode · increased contrast · color blindness · voice control ·
content with long accessibility labels.

Icon-only buttons must have accessible labels. Do not read decorative
elements as meaningful content.

---

## 11. Localization and Content Expansion

Even when version one supports only one language, layouts should not assume
English-length content. Test with: text 30–50% longer than normal · long names
· long dates · large currency values · right-to-left layouts when future
support is plausible · different decimal separators · different calendar
conventions.

Do not embed visible user-facing text directly throughout the codebase. Avoid
constructing sentences from fragments because word order changes across
languages.

---

## 12. Async, Networking, and Concurrency

### 12.1 Loading must end

Every asynchronous operation must have a terminal outcome: success · failure
· cancellation · timeout. A spinner must never continue indefinitely because
one code path forgot to reset a variable. Use cleanup mechanisms that execute
regardless of outcome.

### 12.2 Old requests must not overwrite new state

Examples: a slow earlier search replaces a newer result · a previous record
finishes loading after the user selects another · a cancelled generation
displays its result anyway · a refresh overwrites edits made while it was
running. Track request identity or cancel obsolete work.

### 12.3 Offline behavior must be intentional

Decide whether each feature: requires connectivity · uses cached data ·
queues work · becomes read-only · supports full offline operation. Do not let
the user complete a long workflow only to discover at the final step that
connectivity was required.

### 12.4 Retry must be safe

Retries must not: duplicate purchases · create duplicate records · upload the
same file repeatedly · send the same message twice · apply the same mutation
multiple times.

---

## 13. AI Feature Rules

AI-generated features introduce additional ways for the interface to
misrepresent certainty.

### 13.1 Never present generated output as verified fact

Clearly distinguish: user-provided information · retrieved source information
· deterministic calculations · AI-generated suggestions · AI interpretations ·
estimated values. When correctness matters, provide verification paths.

### 13.2 Do not fabricate completion

An AI workflow must not claim: a file was analyzed when parsing failed · all
records were processed when some failed · a source was consulted when it was
not · an action was performed when only instructions were generated · a
result is based on the user's data when demo data was used · a model is
"confident" without a defined confidence mechanism.

### 13.3 Handle malformed and adversarial input

Test AI features with: empty documents · extremely long documents · scanned
documents · unsupported formats · contradictory instructions · prompt
injection inside uploaded content · personal or sensitive information ·
requests outside the feature's intended scope · model refusal · model timeout
· invalid structured output · partially valid JSON · unexpected language.
Never assume the model will return the requested structure. Validate all
model output before using it.

### 13.4 Provide recovery from generation failures

Users should be able to: retry · edit the result · restore their original
content · continue without AI · see which portion failed · cancel generation
· avoid being charged twice for the same attempt, where applicable.

### 13.5 AI output should not directly perform destructive actions

Generated recommendations should not automatically: delete data · send
messages · publish content · transfer money · modify permissions · commit
code · change production records. Require review or explicit confirmation for
consequential actions.

---

## 14. Platform-Native Behavior

An app should behave like it belongs on its platform.

**iOS and iPadOS.** Check: rotation and multitasking · Dynamic Type · back
gestures · share sheets · scene restoration · keyboard behavior · safe areas ·
haptics · background transitions · permission flows · iPad layouts rather than
enlarged iPhone screens.

**macOS.** Check: window resizing · minimum usable window size · multiple
windows · menu commands · keyboard shortcuts · undo and redo · drag and drop ·
file opening · closing unsaved windows · toolbar customization where
appropriate · focus and Tab navigation · full-screen and split-screen
behavior.

**Web.** Check: browser back and forward · refresh behavior · deep links ·
responsive widths · keyboard navigation · visible focus states · slow
connections · multiple tabs · session expiration · form resubmission · zoom
levels · mobile browser behavior.

Do not force mobile interaction patterns onto desktop interfaces or desktop
interaction patterns onto mobile interfaces.

---

## 15. Performance and Resource Use

AI-generated code often works with small test data but degrades sharply with
real usage. Test with: thousands of records · large images · long text ·
large imported files · many navigation transitions · repeated opening and
closing · background and foreground cycles · poor connectivity · low storage ·
low memory.

**Rules**

- Do not perform heavy work on the main interface thread.
- Avoid loading an entire dataset when only part is visible.
- Resize and cache images appropriately.
- Cancel unnecessary background work.
- Avoid timers that run forever.
- Remove observers and subscriptions correctly.
- Measure before claiming performance improvements.
- Do not repeatedly recalculate expensive values during rendering.
- Do not perform network or database work directly from view-rendering code.

---

## 16. The "Fake Feature" Prohibition

The following must never be considered complete:

- A button with an empty action.
- A toggle that changes only its appearance.
- A search field that filters hardcoded data.
- A settings screen whose values are not read anywhere.
- A purchase screen without entitlement verification.
- A share button that only prints to the console.
- A delete button that only removes the row temporarily.
- A chart populated with placeholder values.
- An onboarding choice that is never persisted.
- A notification setting that does not schedule or cancel anything.
- An export button that creates an invalid or empty file.
- A login screen that accepts any credentials.
- A success screen that always appears.
- An AI result drawn from a static fixture.
- A "coming soon" destination presented as an active feature.

Incomplete features must be: removed, clearly labeled, disabled with an
explanation, or kept behind a development-only flag.

A fake implementation is worse than an honest omission.

---

## 17. Logging and Observability

Errors should be diagnosable without exposing private data. For meaningful
operations, log: what operation began · whether it succeeded or failed · a
safe error category · relevant non-sensitive identifiers · duration when
useful · app version · environment.

Do not log sensitive payloads by default. Production logs should not be
filled with: debug print statements · entire model objects · authentication
values · user documents · raw AI prompts · raw AI responses containing
personal data.

Analytics events should describe user actions, not capture private content.

---

## 18. Release Hygiene

Before release, remove or verify: debug menus · test accounts · development
servers · staging API keys · sample records · placeholder text · "Lorem
ipsum" · TODO buttons · disabled security checks · mock purchase logic ·
hardcoded entitlements · excessive logging · preview-only assets · internal
environment labels · experimental flags · hidden developer gestures · unused
permissions · broken legal links · temporary icons · incorrect version
numbers.

A production build must be tested as a production build. Do not assume
behavior observed in the development environment will be identical.

---

## 19. Required Edge-Case Test Set

Every meaningful feature should be tested with:

1. No data
2. One record
3. Many records
4. Very long text
5. Unusual characters
6. Duplicate input
7. Invalid input
8. Interrupted operation
9. Repeated rapid tapping
10. Permission denial
11. Offline operation
12. Slow operation
13. Failed operation
14. Retry after failure
15. Relaunch
16. Existing old-version data
17. Smallest supported screen or window
18. Largest supported text size
19. Dark mode
20. Keyboard-only or screen-reader use

These tests should be part of feature development, not postponed until
launch.

---

## 20. Mandatory Screen Review

Before approving any screen, answer:

**Layout.** Does it work at the smallest supported size? Does resizing reflow
rather than clip? Can all content be reached? Does long text break the
layout? Does it work with large accessibility text? Does it work in dark
mode?

**State.** What are the loading, empty, content, error, and permission
states? Can stale state appear? What happens after retry? What happens after
navigating away and returning? What happens after relaunch?

**Data.** Is the displayed data real? Is it persisted? Could sample data leak
into production? Could saving create duplicates? Could deletion leave
orphaned data? Will existing users survive a model change?

**Interaction.** Can the user trigger the action twice? Can the operation be
cancelled? Is progress visible? Is the success message truthful? Are
destructive consequences clear? Is user input preserved after failure?

**Accessibility.** Can the screen be completed without relying on color? Do
controls have accessible labels? Is focus order correct? Can it be used with
a keyboard? Does reduced motion work?

---

## 21. Definition of Done

A feature is done only when all applicable statements are true:

**Behavior.** The feature performs its real intended function. No control is
decorative unless it clearly appears decorative. Success is shown only after
confirmed success. Failure does not destroy user work. Repeated input cannot
create accidental duplicate operations. Cancellation is handled separately
from failure.

**Interface.** The layout adapts across supported sizes. Content does not
become inaccessible. Loading, empty, error, and disabled states are designed.
Dark mode and large text work. The feature follows platform conventions.

**Data.** Persistence has been verified after relaunch. Sample data cannot
appear as real production data. Existing data remains compatible. Import and
export outputs have been opened and validated. Delete behavior matches the
wording shown to the user.

**Quality.** At least one non-happy-path test exists. Important logic has
automated coverage. Sensitive data is not exposed in logs. No placeholder
implementation remains. Production configuration has been verified. The
feature has been tested with realistic data volume.

---

## 22. Instructions for AI Coding Agents

Every coding agent working on the app should receive the following rules:

1. Do not implement screenshot-only interfaces.
2. Do not use fake data outside previews and tests unless explicitly
   requested.
3. Do not create controls without implementing their full behavior.
4. Do not declare a feature complete based only on compilation.
5. Do not optimize exclusively for the happy path.
6. Model loading, empty, success, error, cancellation, and permission states
   explicitly.
7. Preserve user data across failures, navigation, and relaunch.
8. Prevent duplicate submissions and stale asynchronous results.
9. Use adaptive layouts and test the smallest supported dimensions.
10. Follow platform-native interaction patterns.
11. Never display raw internal errors to users.
12. Never claim an operation succeeded before confirmation.
13. Validate imported, generated, network, and user-provided data.
14. Treat accessibility as part of implementation.
15. Identify any placeholder, mock, incomplete, or simulated behavior in the
    completion report.
16. State what was tested and what remains unverified.
17. Never silently weaken requirements to make the task easier.
18. Do not remove functioning behavior while repairing an unrelated problem.
19. Do not add fallback data that conceals a failure.
20. Do not mark the task complete when known acceptance criteria are unmet.

---

## 23. Final Product Standard

The goal is not to eliminate every possible defect.

The goal is to eliminate the particular type of defect that AI-assisted
development creates most often:

> Software that looks more complete than it actually is.

A trustworthy app should communicate its real state, preserve the user's
work, behave correctly outside the demo path, and remain usable when the
environment changes.

The final test is simple:

> Would a real user understand what is happening, complete the task
> successfully, recover when something goes wrong, and trust what the app
> tells them?

When the answer is uncertain, the feature is not finished.
