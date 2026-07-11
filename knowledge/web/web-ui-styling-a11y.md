<!-- keywords: web ui, component structure, compound components, variant pattern, forwardRef, tailwind css v4, css-first configuration, theme directive, css modules, design tokens, semantic tokens, oklch color, color-mix, dark mode, responsive layout, mobile-first, container queries, css grid auto-fit, flexbox, fluid typography, clamp, dvh viewport units, accessibility, a11y, wcag 2.2 aa, semantic html, landmarks, aria, accessible name, aria-label, keyboard navigation, tabindex, roving tabindex, focus management, focus-visible, focus trap, modal dialog, skip link, color contrast, target size, form accessibility, aria-live, live region, prefers-reduced-motion, screen reader -->

# Web UI: Structure, Styling, Responsive Layout, Design Tokens & Accessibility

Dense reference for building correct, accessible, maintainable web UI in 2026. Examples use React/JSX + TypeScript, but the rules generalize. Every snippet is idiomatic and compiles.

---

## Component Structure

### Rules
- **One component, one responsibility.** If a component both fetches data and renders complex layout, split it (container/presentational, or hook + view).
- **Colocate** the component, its styles, tests, and stories in one folder: `Button/{Button.tsx, Button.module.css, Button.test.tsx}`.
- **Props over prop-drilling.** Reach for context only for cross-cutting concerns (theme, auth, locale, router), not to avoid passing 2 props.
- **Composition over boolean explosion.** Don't accumulate `isPrimary`, `isLarge`, `hasIcon`. Use `variant`/`size` union types and slot children.
- **Controlled where the parent needs the value; uncontrolled otherwise.** Don't mirror props into state — derive during render.
- **Render the correct element.** A thing that navigates is `<a>`; a thing that acts is `<button>`. Never `<div onClick>`.
- **Stable keys.** Use domain IDs as list keys, never array index for reorderable/filterable lists.

### Composable, typed component (variant pattern)
In React 19, `ref` is a regular prop — `forwardRef` is no longer required for new code (and is deprecated). Type `ref` directly.
```tsx
import type { ButtonHTMLAttributes, Ref } from 'react';
import clsx from 'clsx';
import styles from './Button.module.css';

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
  ref?: Ref<HTMLButtonElement>;
};

export function Button({
  variant = 'primary',
  size = 'md',
  className,
  type = 'button',
  ...props
}: ButtonProps) {
  return (
    <button
      type={type}
      className={clsx(styles.button, styles[variant], styles[size], className)}
      {...props}
    />
  );
}
```
- Exposing `ref` lets parents focus/measure the node (essential for menus, tooltips, form libs).
- Default `type="button"` so a button inside a `<form>` doesn't accidentally submit; consumers can still override.
- Spread `...props` after your defaults so consumers can pass `aria-*`, `onClick`, `disabled`.
- Allow `className` passthrough for one-off overrides without new variants.

> On React ≤18 (or libraries needing it), wrap with `forwardRef<HTMLButtonElement, ButtonProps>(...)` instead of a `ref` prop.

### Slots / compound components
Assign sub-components with `Object.assign` so TypeScript sees them as properties of the parent (bare `Card.Header = ...` errors under `strict`).
```tsx
import type { ReactNode } from 'react';

function CardRoot({ children }: { children: ReactNode }) {
  return <section className={styles.card}>{children}</section>;
}
function CardHeader({ children }: { children: ReactNode }) {
  return <header className={styles.cardHeader}>{children}</header>;
}
function CardBody({ children }: { children: ReactNode }) {
  return <div className={styles.cardBody}>{children}</div>;
}

export const Card = Object.assign(CardRoot, { Header: CardHeader, Body: CardBody });
// <Card><Card.Header>…</Card.Header><Card.Body>…</Card.Body></Card>
```
Compound components give consumers layout freedom without a prop for every slot.

### Anti-patterns
- Passing JSX through props (`title={<Icon/>}`) when `children` or a slot would do.
- `useEffect` to sync derived state — compute it in render or with `useMemo`.
- Giant `index.ts` barrels that break tree-shaking and create circular imports.
- Components that read `window`/`document` during render (breaks SSR/hydration). Guard with `useEffect`, `useSyncExternalStore`, or `typeof window !== 'undefined'`.

---

## Styling: Tailwind CSS v4

Tailwind v4 is **CSS-first**: no `tailwind.config.js` by default. You import Tailwind and declare tokens in CSS with `@theme`. The engine (Oxide) is dramatically faster; browser targets are modern (uses `@property`, cascade layers, `color-mix()`).

### Setup
```css
/* app.css */
@import "tailwindcss";

@theme {
  --color-brand-50:  oklch(0.97 0.02 265);
  --color-brand-500: oklch(0.62 0.19 265);
  --color-brand-600: oklch(0.55 0.20 265);
  --font-sans: "Inter var", system-ui, sans-serif;
  --radius-card: 0.75rem;
  --breakpoint-3xl: 120rem;        /* adds 3xl: variant */
}
```
- **Every `--color-*` token auto-generates utilities**: `--color-brand-500` → `bg-brand-500`, `text-brand-500`, `border-brand-500`, `ring-brand-500`, `fill-brand-500`.
- `--font-*` → `font-sans`; `--radius-*` → `rounded-card`; `--breakpoint-*` → new responsive variants.
- The spacing scale is derived from a built-in `--spacing` base (`0.25rem`), so `p-4` = `1rem` out of the box; override `--spacing` in `@theme` only if you need a different unit.
- Tokens are emitted as real CSS variables at `:root`, so third-party/custom CSS can read `var(--color-brand-500)` and you can inspect them in DevTools.

### Utility-first rules
- **Compose utilities inline; don't prematurely extract.** Repetition in markup is cheaper than a leaky abstraction. Extract into a component when the *markup* repeats, not just the classes.
- **For reused class strings, extract a component** (React), not `@apply`. `@apply` is a last resort (e.g. styling markdown/CMS HTML you don't control).
- **Use logical utilities** for i18n: `ps-4`/`pe-4` (padding-inline-start/end) and `ms-`/`me-` instead of `pl-`/`pr-`/`ml-`/`mr-`. They flip automatically in RTL.
- **Arbitrary values sparingly**: `top-[117px]` is a smell — add a token or use the spacing scale.
- **Order-independent**: class order doesn't affect specificity in v4 (cascade layers). But keep an ordering convention (`prettier-plugin-tailwindcss`) for readability.

### Variants that matter
```html
<button class="bg-brand-600 hover:bg-brand-700 focus-visible:outline-2
               focus-visible:outline-offset-2 focus-visible:outline-brand-600
               disabled:opacity-50 disabled:pointer-events-none
               data-[loading=true]:cursor-wait">
  Save
</button>
```
- `focus-visible:` not `focus:` for keyboard rings (see A11y).
- `data-[state=open]:` targets `data-*` attributes — pairs with headless libs (Radix, React Aria).
- `group`/`peer` for parent/sibling state: `<label class="peer-invalid:text-red-600">`.
- `dark:` for dark mode; `motion-reduce:` and `motion-safe:` for reduced motion.
- `supports-[display:grid]:` for feature queries; `has-[:checked]:` for `:has()` styling.

### Custom utilities & variants (v4 syntax)
```css
@utility tap-target {           /* becomes a real utility class, works with variants */
  min-block-size: 2.75rem;
  min-inline-size: 2.75rem;
}
/* Shorthand form; combine states with :is(), not comma-separated selectors */
@custom-variant hocus (&:is(:hover, :focus-visible));
```

---

## Styling: CSS Modules (framework-agnostic alternative)

Use when you want scoped, plain CSS with zero utility vocabulary, or in a design-system package that shouldn't depend on Tailwind.

```css
/* Button.module.css */
.button {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  border-radius: var(--radius-md);
  padding-block: var(--space-2);
  padding-inline: var(--space-4);
  font: inherit;
  cursor: pointer;
}
.button:focus-visible {
  outline: 2px solid var(--color-focus);
  outline-offset: 2px;
}
.primary { background: var(--color-brand-600); color: white; }
.primary:hover { background: var(--color-brand-700); }
```
```tsx
import styles from './Button.module.css';
// className={styles.button} → compiles to a hashed class like Button_button__x7f2a
```
- Class names are locally scoped (hashed), so no global collisions and no specificity wars.
- Compose with `composes`: `.primary { composes: button; background: var(--color-brand-600); }` (CSS Modules feature; must be the first declaration).
- Use CSS custom properties for tokens so Modules and Tailwind can share the same `:root` variables.
- Global escape hatch: `:global(.some-third-party-class) { … }`.

### Tailwind vs CSS Modules — pick one primary
- **Tailwind**: fast iteration, enforced consistency via scale, tiny prod CSS, great for app UI.
- **CSS Modules**: full CSS power (complex selectors, keyframes, container queries), better for a portable component library, no build-time class scanning.
- Don't split a single component across both. Share **design tokens** (CSS variables) across whichever you use.

---

## Design Tokens

Tokens are the single source of truth for visual decisions. Structure them in **three tiers**:

1. **Primitive / base** — raw values. `--blue-500: oklch(0.62 0.19 265)`. No meaning.
2. **Semantic / alias** — purpose. `--color-action: var(--blue-500)`, `--color-text: var(--gray-900)`. Components consume *these*.
3. **Component** — scoped overrides. `--button-bg: var(--color-action)`.

Components reference semantic tokens; theming swaps primitives → semantics. This is what makes dark mode and rebrands one-line changes.

```css
:root {
  /* Tier 1: primitives */
  --gray-0:   oklch(1 0 0);
  --gray-900: oklch(0.21 0.01 265);
  --blue-500: oklch(0.62 0.19 265);

  /* Tier 2: semantic (light theme) */
  --color-bg:      var(--gray-0);
  --color-text:    var(--gray-900);
  --color-action:  var(--blue-500);
  --color-focus:   var(--blue-500);

  /* type scale, spacing, radius, shadow, z-index, motion */
  --space-1: 0.25rem; --space-2: 0.5rem; --space-4: 1rem; --space-8: 2rem;
  --radius-md: 0.5rem;
  --shadow-1: 0 1px 2px oklch(0 0 0 / 0.08);
  --z-modal: 1000;
  --ease-standard: cubic-bezier(0.2, 0, 0, 1);
  --duration-fast: 120ms;
}

/* Dark theme: reassign SEMANTIC tokens only */
@media (prefers-color-scheme: dark) {
  :root {
    --color-bg:   var(--gray-900);
    --color-text: var(--gray-0);
  }
}
/* Manual override wins over system preference */
[data-theme="dark"] {
  --color-bg:   var(--gray-900);
  --color-text: var(--gray-0);
}
```

### Token rules
- **Use OKLCH** for color. Perceptually uniform lightness → consistent contrast when you derive shades, and it reaches P3 wide-gamut colors that hex can't. `color-mix(in oklch, …)` for tints/alpha.
- **Name by role, not value.** `--color-danger`, not `--color-red`. A red that becomes orange later shouldn't require renaming.
- **Reference, don't repeat.** Semantic tokens point at primitives via `var()`. One change cascades.
- **Themeable = swap at the semantic tier.** Never redefine primitives per theme.
- **Motion & z-index are tokens too.** Centralize durations, easings, and a z-index scale to stop stacking-context bugs.
- **Sync with Tailwind:** define the same names in `@theme` (`--color-action`) so utilities and hand-written CSS agree.

---

## Responsive Layout

### Mental model
- **Mobile-first**: unprefixed styles target small screens; `sm:`/`md:`/`lg:` add at breakpoints going up. `min-width` queries only.
- **Prefer intrinsic layout over breakpoints.** Flexbox `wrap`, Grid `auto-fit`, and `clamp()` handle most responsiveness with *zero* media queries. Reach for breakpoints only for structural rearrangement.
- **Container queries** size a component by its *container*, not the viewport — the correct tool for reusable components that live in sidebars, grids, and modals.

### Fluid, breakpoint-free grid
```css
.grid {
  display: grid;
  gap: var(--space-4);
  /* Items are min 16rem, grow to fill, wrap automatically. No media queries. */
  grid-template-columns: repeat(auto-fit, minmax(min(16rem, 100%), 1fr));
}
```
The `min(16rem, 100%)` prevents overflow on viewports narrower than 16rem.

### Container queries (component-level responsiveness)
```css
.card-list { container-type: inline-size; container-name: cards; }

.card { display: grid; gap: var(--space-2); }
@container cards (min-width: 30rem) {
  .card { grid-template-columns: 8rem 1fr; }  /* side-by-side when container is wide */
}
```
Tailwind v4 equivalent (container queries are built in, no plugin needed):
```html
<div class="@container">
  <article class="grid gap-2 @md:grid-cols-[8rem_1fr]">…</article>
</div>
```

### Fluid typography & spacing with clamp()
```css
:root {
  /* min 1rem, scales with viewport, max 1.25rem — no breakpoints */
  --step-0: clamp(1rem, 0.9rem + 0.5vw, 1.25rem);
  --step-1: clamp(1.5rem, 1.2rem + 1.5vw, 2.25rem);
}
h1 { font-size: var(--step-1); }
```
Keep the fluid range zoom-safe: use `rem`-based min/max (not `px`) so it scales with the user's font-size setting, and keep the growth factor small enough that 200% zoom doesn't break layout.

### Layout primitives
- **Flexbox** for 1-D distribution (toolbars, nav, form rows): `flex flex-wrap items-center gap-4`.
- **Grid** for 2-D layout and page scaffolds. Named areas keep complex layouts readable:
```css
.app { display: grid; grid-template: "head head" auto "nav main" 1fr / 16rem 1fr; }
```
- **`gap` over margins** for spacing between siblings — no margin-collapse surprises, no last-child hacks.
- **Avoid fixed heights.** Let content dictate height; use `min-height` and `aspect-ratio` instead.
- **`aspect-ratio: 16 / 9`** + `object-fit: cover` for media without layout shift.
- **Reserve space for async content** (`min-height`, skeletons, `width`/`height` on images) to prevent CLS.

### Modern viewport units
- Use `dvh`/`svh`/`lvh` (dynamic/small/large viewport height) instead of `vh` for full-height mobile layouts — `100vh` overflows under mobile browser chrome; `100dvh` accounts for it.

---

## Accessibility (WCAG 2.2 AA — the 2026 baseline)

WCAG 2.2 is the current standard (published Oct 2023, updated Dec 2024; ratified as **ISO/IEC 40500:2025**). Target **Level AA**. Accessibility is not a bolt-on; the checklist below is build-time, not audit-time.

### 1. Semantic HTML (do this first — it's 80% of a11y)
- **Landmarks**: one `<header>`, `<nav>` (multiple allowed if each has a distinct `aria-label`), exactly one `<main>` per page, `<footer>`, `<aside>`. Screen-reader users navigate by landmark.
- **Headings form an outline.** Exactly one `<h1>`; never skip levels (no `<h1>` → `<h3>`). Don't pick a level for its size — style with CSS.
- **Lists for lists** (`<ul>/<ol>/<dl>`), `<table>` for tabular data (with `<th scope>`), `<figure>/<figcaption>` for media.
- **Native controls**: `<button>`, `<a href>`, `<input>`, `<select>`, `<details>/<summary>`, `<dialog>`. They come with focus, keyboard, and role for free.
- `<a>` = navigation (has `href`). `<button>` = action. A button without `type="button"` inside a form defaults to `submit` — set it explicitly.

```html
<main id="main">
  <h1>Invoices</h1>
  <nav aria-label="Invoice filters">…</nav>
  <ul>
    <li><a href="/invoices/1">Invoice #1</a></li>
  </ul>
</main>
```

### 2. Accessible names & ARIA
- **First rule of ARIA: don't use ARIA if a native element does the job.** Bad ARIA is worse than none.
- **Every interactive control needs an accessible name.** Visible text is best. Otherwise `aria-label` or `aria-labelledby`. Icon-only buttons *must* have one:
```html
<button type="button" aria-label="Close dialog">
  <svg aria-hidden="true" focusable="false">…</svg>
</button>
```
- **Label form fields** with `<label for>` (or by wrapping the input). `placeholder` is NOT a label — it vanishes on input and often fails contrast.
- **`aria-describedby`** links hints/errors to a field. **`aria-live`** announces dynamic changes.
- **`aria-hidden="true"`** hides decorative content from AT; never put it on a focusable element.
- **State attributes**: `aria-expanded`, `aria-pressed`, `aria-current="page"`, `aria-selected`, `aria-checked`, `aria-invalid`. Keep them in sync with visual state. Prefer the native `disabled` attribute over `aria-disabled` unless you need the control to stay focusable.
- **Roles** only when building a non-native widget: `role="tablist"/"tab"/"tabpanel"`, `role="dialog"`, `role="menu"`. If you use a role, you own its full keyboard interaction contract (see the ARIA Authoring Practices Guide, APG).
- Decorative SVGs: `aria-hidden="true" focusable="false"`. Meaningful ones: `role="img"` + `<title>` or `aria-label`.

### 3. Keyboard navigation (everything works without a mouse)
- **All interactive elements reachable and operable via keyboard.** Tab / Shift+Tab to move, Enter/Space to activate, Esc to dismiss, arrows within composite widgets.
- **Logical tab order** = DOM order. Don't fight it with positive `tabindex`. Valid values:
  - `tabindex="0"`: make a custom element focusable in natural order.
  - `tabindex="-1"`: focusable programmatically (`el.focus()`) but not in the tab sequence — for roving focus and focus targets.
  - **Never positive** `tabindex` — it hijacks order and is unmaintainable.
- **No keyboard traps** (WCAG 2.1.2): focus must be able to leave any component (except intentional modal traps that Esc closes).
- **Roving tabindex** for composite widgets (menus, tabs, grids, toolbars): the container has one tab stop; arrow keys move a single `tabindex=\"0\"` among items (others `-1`):
```tsx
import type { KeyboardEvent, RefObject } from 'react';

function useRovingFocus(items: RefObject<HTMLElement[]>) {
  return function onKeyDown(e: KeyboardEvent, index: number) {
    const els = items.current;
    if (!els?.length) return;
    let next = index;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = (index + 1) % els.length;
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = (index - 1 + els.length) % els.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = els.length - 1;
    else return;
    e.preventDefault();
    els[next].focus();
  };
}
```
- **Dragging alternative** (WCAG 2.2 — 2.5.7): any drag interaction (reorder, slider, kanban) must also work with single clicks/taps (e.g. move-up/move-down buttons, or click-to-place).

### 4. Focus management & visible focus
- **Visible focus indicator is mandatory** (WCAG 2.4.7; complemented by 2.4.11 Focus Not Obscured and 2.4.13 Focus Appearance, new in 2.2). **Never** `outline: none` without an equally-visible replacement.
- Use **`:focus-visible`** so pointer users don't see rings but keyboard users do. Styling only `:focus-visible` (not `:focus`) already suppresses the ring for mouse clicks — no separate reset needed:
```css
:focus-visible {
  outline: 2px solid var(--color-focus);
  outline-offset: 2px;
}
```
- **Focus Appearance (2.4.13):** the indicator should be at least a 2px-thick perimeter and have ≥3:1 contrast against the adjacent colors. A 2px offset outline in a contrasting color satisfies this.
- **Focus Not Obscured (2.4.11):** sticky headers/footers must not fully cover the focused element. Add `scroll-margin-top` to offset:
```css
:target, [tabindex]:focus { scroll-margin-top: 5rem; } /* clears a 5rem sticky header */
```
- **Move focus on route/view change** so screen readers announce the new context. In SPAs, focus the new `<h1>` (or a container with `tabindex="-1"`) after navigation.
- **Modal focus trap**: `<dialog>` with `.showModal()` handles most of this natively — focus moved inside, background made inert, Esc-to-close, and a `::backdrop`:
```tsx
import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';

function Modal({ open, onClose, children }: {
  open: boolean; onClose: () => void; children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    else if (!open && el.open) el.close();
  }, [open]);
  return (
    <dialog ref={ref} onClose={onClose} aria-labelledby="modal-title">
      <h2 id="modal-title">Confirm</h2>
      {children}
      <button type="button" onClick={onClose}>Close</button>
    </dialog>
  );
}
```
Native `<dialog>.showModal()` moves focus inside, makes the background inert, closes on Esc, and exposes `::backdrop`. Browsers now restore focus to the invoker on `close()`; still restore focus yourself if you unmount or re-render the trigger away.

### 5. Skip link (bypass blocks — WCAG 2.4.1)
```html
<a href="#main" class="skip-link">Skip to main content</a>
```
```css
.skip-link {
  position: absolute;
  inset-inline-start: 0;
  inset-block-start: 0;
  transform: translateY(-150%);   /* pushed off-screen but still focusable */
  background: var(--color-bg);
  padding: var(--space-2) var(--space-4);
}
.skip-link:focus-visible { transform: translateY(0); }  /* reveal on focus */
```
Do NOT hide skip links with `display: none` or `visibility: hidden` — that removes them from the tab order.

### 6. Color & contrast
- **Text contrast**: ≥ **4.5:1** for normal text, **3:1** for large text (≥24px, or ≥18.66px bold) — WCAG 1.4.3.
- **Non-text contrast**: ≥ **3:1** for UI component boundaries, icons, focus indicators, and graph elements (1.4.11).
- **Never convey meaning by color alone** (1.4.1). Errors need an icon/text, not just red. Links in body text need underline or another non-color cue. Chart series need labels/patterns.
- Verify each pairing (including hover/disabled/dark-mode states). OKLCH lightness helps but doesn't guarantee contrast — measure.
- Respect `forced-colors` (Windows High Contrast): don't kill system colors; use `forced-color-adjust` deliberately and test.

### 7. Target size (WCAG 2.2 — 2.5.8, Level AA)
- Pointer targets ≥ **24×24 CSS px** (exceptions: inline text links, ≥24px spacing between adjacent targets, or an equivalent larger control exists).
- **Recommended: 44×44** (matches mobile guidance) for primary touch controls. Add hit-area padding rather than shrinking the visual:
```css
.icon-button { min-block-size: 2.75rem; min-inline-size: 2.75rem; }
```

### 8. Forms (highest-value a11y surface)
- Programmatic label for every field. Group related controls with `<fieldset>` + `<legend>` (radio/checkbox groups).
- Errors: set `aria-invalid="true"`, link the message with `aria-describedby`, and put the message in a live region so it's announced. Focus the first invalid field on submit.
- **Redundant Entry (WCAG 2.2 — 3.3.7):** don't force users to re-enter info already provided in the same process — autofill or offer it.
- **Accessible Authentication (WCAG 2.2 — 3.3.8):** don't require memorization/transcription puzzles; allow paste into OTP/password fields, support password managers and passkeys.
- Use correct `type` and `autocomplete` (`type="email" autocomplete="email"`, `inputmode="numeric"`) — better UX and AT support.
```html
<div>
  <label for="email">Email</label>
  <input id="email" name="email" type="email" autocomplete="email"
         aria-describedby="email-error" aria-invalid="true" required />
  <p id="email-error" role="alert">Enter a valid email address.</p>
</div>
```

### 9. Live regions & async UI
- Announce async results (toasts, validation, search-result counts) via a live region:
  - `aria-live="polite"` (or `role="status"`) — non-urgent; waits for a pause.
  - `aria-live="assertive"` (or `role="alert"`) — urgent; interrupts. Use sparingly.
- The live region must exist in the DOM **before** content is injected; screen readers announce the *change*, not the initial mount.
```tsx
<p role="status">{resultCount} results</p>  {/* role=status implies aria-live=polite */}
```
- Loading states: `aria-busy="true"` on the region being updated; give spinners an accessible label or mark purely-decorative ones `aria-hidden`.

### 10. Motion & preferences
- Honor `prefers-reduced-motion` (WCAG 2.3.3): disable parallax, large transitions, autoplay. Keep essential motion subtle.
```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```
Tailwind: gate animations with `motion-safe:animate-…` so reduced-motion users opt out by default.
- No content flashing more than 3×/sec (2.3.1 — seizure safety).
- Support 200% zoom and 400% reflow without loss of content/function (1.4.4, 1.4.10): use `rem`/`em` and relative units; avoid fixed-px containers that clip.

### 11. Language, titles, media
- `<html lang="en">` (and `lang` on any foreign-language passages) — drives screen-reader pronunciation.
- Unique, descriptive `<title>` per page/view; update it on SPA navigation.
- Images: meaningful `alt`; decorative images `alt=""`. Video needs captions (1.2.2); audio needs transcripts.

---

## Pre-ship Accessibility Checklist
- [ ] Keyboard-only pass: reach and operate every control; Esc closes overlays; no traps.
- [ ] Visible `:focus-visible` on all interactive elements, not obscured by sticky UI.
- [ ] One `<h1>`, no skipped heading levels, landmarks present, skip link works.
- [ ] Every control has an accessible name; icon buttons labeled; images have correct `alt`.
- [ ] Contrast: text ≥4.5:1 (large ≥3:1), UI/icons/focus ≥3:1, in all states + dark mode.
- [ ] No color-only meaning; links/errors have a second cue.
- [ ] Targets ≥24×24px (44×44 for touch primaries).
- [ ] Forms: labels, `fieldset/legend`, `aria-invalid` + `aria-describedby` errors, focus-to-first-error, paste allowed in auth fields.
- [ ] Live regions announce async changes; exist before content injection.
- [ ] `prefers-reduced-motion` respected; layout holds at 200% zoom / 400% reflow.
- [ ] Run automated checks (axe / Lighthouse) — they catch ~30–40%; the rest is the manual list above.

---

## Quick Decision Table
| Need | Reach for |
|---|---|
| App UI, fast iteration | Tailwind v4 utilities + `@theme` tokens |
| Portable component library | CSS Modules + shared CSS-variable tokens |
| Component responsive to its container | Container queries (`@container` / `container-type`) |
| Page responsive to viewport | Mobile-first `min-width` media queries |
| Responsive size without breakpoints | `clamp()`, `auto-fit` grid, `flex-wrap` |
| Theming / dark mode | Swap semantic tokens; `prefers-color-scheme` + `[data-theme]` |
| Navigation vs action | `<a href>` vs `<button type>` |
| Modal | Native `<dialog>.showModal()` |
| Custom widget (tabs/menu/combobox) | Follow WAI-ARIA APG + roving tabindex, or a headless lib (Radix / React Aria) |
| Announce async change | `role="status"` / `aria-live` region present in DOM up front |
