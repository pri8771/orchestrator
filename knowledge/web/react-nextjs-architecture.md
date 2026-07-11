<!-- keywords: next.js, react, app router, server components, client components, rsc, use client, use server, server actions, useActionState, useFormStatus, useOptimistic, use hook, data fetching, suspense, streaming, caching, use cache, cacheComponents, cacheLife, cacheTag, revalidateTag, updateTag, revalidatePath, refresh, partial prerendering, ppr, routing, layout, loading, error boundary, global-error, route handlers, dynamic routes, route groups, parallel routes, intercepting routes, generateMetadata, generateStaticParams, params async, searchParams, state management, zustand, jotai, tanstack query, url state, project structure, next 16, react 19, next/image, next/font, redirect, notFound, zod validation, server actions security -->

## Scope & Versions

- Targets **Next.js 16.x (App Router)** + **React 19.2**. Assumes TypeScript, `next.config.ts`, and Node 20.9+.
- App Router (`app/`) is the default. Pages Router (`pages/`) is legacy — do not start new projects there.
- **Cache Components** (`cacheComponents: true`) is the modern caching model, stable in v16. It supersedes the Next.js 15 experimental flags `dynamicIO`, `useCache`, and `ppr` — all folded into one flag. Snippets below assume it is ON unless noted.
- Rule of thumb: **Server Components by default, Client Components only where you need interactivity or browser APIs.** Push `'use client'` as far down the tree (toward the leaves) as possible.

## Mental Model

- Every component in `app/` is a **Server Component (RSC)** unless the file (or an ancestor in the same module graph) is marked `'use client'`.
- Server Components: run only on the server, can be `async`, can touch the DB/filesystem/secrets, never ship their JS to the browser. They cannot use hooks (`useState`, `useEffect`), event handlers, or browser APIs.
- Client Components: run on the server (for SSR/prerender) **and** in the browser. They can use hooks, event handlers, `window`. They cannot be `async` function components — use the `use()` hook to unwrap promises instead.
- Data flows **down** as serializable props from Server → Client. Serializable = primitives, plain objects/arrays, `Date`, `Map`, `Set`, and Server Components/Server Actions passed through. You **cannot** pass plain functions (except Server Actions), class instances, or symbols across the boundary. You CAN pass Server Components as `children`/props into Client Components.

## Server vs Client Components

```tsx
// app/products/page.tsx — Server Component (default). Note: async.
import { db } from '@/lib/db'
import { AddToCart } from './add-to-cart' // a Client Component

export default async function ProductsPage() {
  const products = await db.product.findMany() // direct DB access, runs on server
  return (
    <ul>
      {products.map((p) => (
        <li key={p.id}>
          {p.name}
          <AddToCart productId={p.id} /> {/* interactivity isolated to a leaf */}
        </li>
      ))}
    </ul>
  )
}
```

```tsx
// app/products/add-to-cart.tsx — Client Component
'use client'
import { useState } from 'react'

export function AddToCart({ productId }: { productId: string }) {
  const [pending, setPending] = useState(false)
  return (
    <button
      disabled={pending}
      onClick={async () => {
        setPending(true)
        await fetch('/api/cart', { method: 'POST', body: JSON.stringify({ productId }) })
        setPending(false)
      }}
    >
      Add
    </button>
  )
}
```

> For a mutation triggered by your own UI, prefer a **Server Action** over a hand-rolled `fetch('/api/...')` (see below). The `fetch` above is shown only to contrast the client boundary.

### The composition pattern (avoid client-poisoning the tree)

- A Client Component **can render Server Components passed as `children`/props**, but cannot `import` a Server Component. Use this to keep data-fetching on the server while wrapping it in client-side UI (e.g. a theme provider, a tab panel).

```tsx
// Client shell that accepts server-rendered children
'use client'
import { useState, type ReactNode } from 'react'

export function Collapsible({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button onClick={() => setOpen((o) => !o)}>Toggle</button>
      {open && children}
    </div>
  )
}
```

```tsx
// Server Component composes them — ServerData stays a Server Component
import { Collapsible } from './collapsible'
import { ServerData } from './server-data'

export default function Page() {
  return (
    <Collapsible>
      <ServerData /> {/* fetched on server, passed as children — never becomes client JS */}
    </Collapsible>
  )
}
```

### `'use client'` rules

- Marks a **module boundary**: the marked file and everything it imports (transitively) becomes part of the client bundle.
- Put it at the top of the file, before imports.
- A Server Component can import a Client Component freely. The reverse (Client `import`ing Server) is not allowed — pass as props/`children` instead.
- `'use server'` is unrelated to Server Components — it marks **Server Actions/Functions**, not RSCs. Do not confuse them.

## Routing (App Router file conventions)

- Routing is **folder-based** under `app/`. A route is defined by a `page.tsx` in a folder.

| File | Purpose |
|------|---------|
| `page.tsx` | Publicly routable UI for a segment |
| `layout.tsx` | Shared wrapper that persists across navigation; nests; must render `{children}` |
| `template.tsx` | Like layout but re-mounts on navigation (fresh state/effects) |
| `loading.tsx` | Suspense fallback for the segment (auto-wraps `page` in `<Suspense>`) |
| `error.tsx` | Error boundary (Client Component; gets `error` + `reset`/`unstable_retry`) |
| `not-found.tsx` | UI for `notFound()` and unmatched routes |
| `global-error.tsx` | Root-level error boundary; must render its own `<html>`/`<body>`; replaces the root layout when active |
| `route.ts` | API endpoint (Route Handler) — cannot coexist with `page.tsx` in the same folder |
| `default.tsx` | Parallel-route fallback for unmatched slots |

### Dynamic, catch-all, and route groups

- `app/blog/[slug]/page.tsx` → `/blog/hello`, param `slug`.
- `app/shop/[...all]/page.tsx` → catch-all; `app/shop/[[...all]]/page.tsx` → optional catch-all.
- `app/(marketing)/about/page.tsx` → **route group**: `(marketing)` is organizational, not in the URL (`/about`).
- `app/@team/page.tsx` + `app/@analytics/page.tsx` → **parallel routes**, rendered into named slots in the layout.
- `app/(.)photo/[id]/page.tsx` → **intercepting route** (e.g. modal over a feed).

### `params` & `searchParams` are async (v15+)

`params` and `searchParams` are **Promises**. Await them.

```tsx
// app/blog/[slug]/page.tsx
export default async function Post({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>
}) {
  const { slug } = await params
  const { q } = await searchParams
  return <article>{slug} — filter: {q}</article>
}
```

### Metadata

```tsx
// Static
import type { Metadata } from 'next'
export const metadata: Metadata = { title: 'Products', description: '...' }

// Dynamic
export async function generateMetadata({
  params,
}: { params: Promise<{ slug: string }> }): Promise<Metadata> {
  const { slug } = await params
  const post = await getPost(slug)
  return { title: post.title, openGraph: { images: [post.cover] } }
}
```

### Static params (SSG)

```tsx
// Pre-render dynamic routes at build time
export async function generateStaticParams() {
  const posts = await getPosts()
  return posts.map((p) => ({ slug: p.slug })) // [{ slug: 'a' }, ...]
}
```

### Navigation

```tsx
// Declarative — always prefer <Link> for internal navigation (auto-prefetches in viewport)
import Link from 'next/link'
;<Link href="/products">Products</Link>
```

```tsx
// Programmatic (Client Component only)
'use client'
import { useRouter, usePathname } from 'next/navigation'
export function Nav() {
  const router = useRouter()
  const pathname = usePathname()
  return <button onClick={() => router.push('/checkout')}>Go ({pathname})</button>
}
```

```tsx
// Server-side redirect / 404 (these throw — do not return them)
import { redirect, notFound } from 'next/navigation'
if (!user) redirect('/login')
if (!post) notFound()
```

## Data Fetching

- **Fetch on the server in Server Components** by default. Colocate the fetch with the component that renders it.
- `await` data directly; no `useEffect`/`useState`/loading spinners for initial data.
- Parallelize independent requests with `Promise.all`; do NOT create waterfalls by awaiting sequentially when the requests don't depend on each other.

```tsx
// Parallel fetches — both start immediately
export default async function Dashboard() {
  const [user, stats] = await Promise.all([getUser(), getStats()])
  return <Profile user={user} stats={stats} />
}
```

### Streaming with Suspense

- Wrap slow data in `<Suspense>` so the shell renders instantly and the slow part streams in. `loading.tsx` does this automatically at the segment level.

```tsx
import { Suspense } from 'react'

export default function Page() {
  return (
    <section>
      <h1>Feed</h1> {/* instant */}
      <Suspense fallback={<FeedSkeleton />}>
        <Feed /> {/* async Server Component streams in */}
      </Suspense>
    </section>
  )
}

async function Feed() {
  const items = await getFeed() // slow
  return <ul>{items.map((i) => <li key={i.id}>{i.title}</li>)}</ul>
}
```

### `use()` in Client Components

- To consume a promise (or context) in a Client Component, use React 19's `use()`. Pass the promise **from a Server Component as a prop** (don't create it in render).

```tsx
'use client'
import { use } from 'react'
export function Comments({ promise }: { promise: Promise<Comment[]> }) {
  const comments = use(promise) // suspends until resolved
  return <ul>{comments.map((c) => <li key={c.id}>{c.body}</li>)}</ul>
}
```

```tsx
// Server Component kicks off the fetch without awaiting; streams via Suspense
import { Suspense } from 'react'
export default function Page() {
  const promise = getComments() // NOT awaited
  return (
    <Suspense fallback={<p>Loading…</p>}>
      <Comments promise={promise} />
    </Suspense>
  )
}
```

### Route Handlers (REST/webhooks)

```ts
// app/api/products/route.ts
import type { NextRequest } from 'next/server'

export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams.get('q') ?? ''
  const products = await db.product.search(q)
  return Response.json(products)
}

export async function POST(req: NextRequest) {
  const body = await req.json()
  const created = await db.product.create(body)
  return Response.json(created, { status: 201 })
}
```

- `Response.json()` (the Web API) is idiomatic; `NextResponse` is only needed for its extras (cookies, rewrites, redirects in middleware).
- Prefer **Server Actions over hand-rolled API routes** for internal mutations from your own UI. Reserve Route Handlers for webhooks, third-party callers, non-form clients, streaming responses, or public APIs.

## Caching (Cache Components model, Next.js 16)

Enable it:

```ts
// next.config.ts
import type { NextConfig } from 'next'
const nextConfig: NextConfig = {
  cacheComponents: true, // replaces experimental dynamicIO/useCache/ppr
}
export default nextConfig
```

### Defaults you must internalize

- With Cache Components: **data fetching is dynamic by default.** `fetch()` is NOT cached; any uncached async work marks that scope dynamic and renders at request time.
- Next.js prerenders a **static shell** and streams dynamic content (Partial Prerendering is the default — no separate `ppr` flag).
- You **opt IN** to caching with `'use cache'`. Nothing is silently cached.

### `'use cache'` directive

```tsx
import { cacheLife, cacheTag } from 'next/cache'

export async function getProducts() {
  'use cache'
  cacheLife('hours')        // preset profile: 'seconds'|'minutes'|'hours'|'days'|'weeks'|'max'
  cacheTag('products')      // tag for on-demand invalidation
  const res = await fetch('https://api.example.com/products')
  return res.json()
}
```

- Works at **file**, **component**, or **function** level. Put `'use cache'` at the top of the scope (or top of file, in which case every export must be `async`).
- **Cache key** = build ID + function identity + serialized arguments + captured closure variables. Different args → different entries.
- **Constraint:** cached scopes **cannot** call `cookies()`, `headers()`, or read `searchParams`/`params`. Read those OUTSIDE the cached scope and pass the values in as arguments. (Directly calling them inside throws; awaiting an unresolved request-time promise inside a cache during prerender hangs the build.)
- **Interleaving:** non-serializable `children`/slots and Server Actions may be passed *through* a cached component as long as you don't introspect them — they don't join the cache key.
- Default (`default`) profile: `stale` 5 min (client), `revalidate` 15 min (server), never expires by time. Presets range from `seconds` (revalidate 1s) to `max` (revalidate 30 days).
- Cache an entire route by adding `'use cache'` to the top of **both** `layout.tsx` and `page.tsx` (each segment caches independently).

### On-demand invalidation

`updateTag` and `revalidateTag` do different things — pick deliberately:

```tsx
// app/products/actions.ts
'use server'
import { updateTag } from 'next/cache'

export async function saveProduct(id: string, data: FormData) {
  await db.product.update(id, data)
  updateTag('products') // read-your-writes: next request blocks for fresh data
}
```

```ts
// app/api/revalidate/route.ts — Route Handler (webhook)
import type { NextRequest } from 'next/server'
import { revalidateTag } from 'next/cache'

export async function POST(req: NextRequest) {
  revalidateTag('products', 'max') // stale-while-revalidate; second arg REQUIRED in v16
  return Response.json({ ok: true })
}
```

- **`updateTag(tag)`** — Server-Actions-only. Immediately expires the tag; the next request waits for fresh data. Use for **read-your-writes** (the user just made the change and must see it now).
- **`revalidateTag(tag, profile)`** — Server Actions **and** Route Handlers. The `profile` arg is now required (`'max'` = stale-while-revalidate; the single-arg form is **deprecated**). Use for content where a brief delay is fine (catalogs, blog posts). For webhooks needing hard expiry, pass `{ expire: 0 }`.
- **`revalidatePath(path)`** — invalidate a specific route path instead of a tag.
- **`refresh()`** (`next/cache`, Server Actions) — re-render *uncached* dynamic content (live counters, notifications) without touching any cache entry.
- **Never cache per-user/personalized data** (session, account, personalized feed) in a shared `'use cache'` scope. Cache the shared data layer and pass user context in as args, or leave the route dynamic. For unavoidable per-request caching, `'use cache: private'` exists but is a last resort.

### Migration note

- If you see `experimental.dynamicIO`, `experimental.useCache`, or `experimental.ppr` in an existing config, they are obsolete in v16 — replace with the single `cacheComponents: true`.
- Legacy Next.js 14/early-15 patterns (`export const dynamic`, `export const revalidate`, `fetch(url, { next: { revalidate } })`) still work but are superseded by `'use cache'` + `cacheLife`. Do not mix the two models in one project.

## Server Actions (mutations)

- Server Actions are async functions marked `'use server'`. They run on the server, can be called from forms or event handlers, and are the idiomatic way to mutate data.
- Mark them per-function or per-file. Define in a dedicated `actions.ts` for reuse.

```tsx
// app/todos/actions.ts
'use server'
import { revalidateTag } from 'next/cache'
import { redirect } from 'next/navigation'
import { z } from 'zod'

const Schema = z.object({ title: z.string().min(1) })

export async function createTodo(_prev: unknown, formData: FormData) {
  const parsed = Schema.safeParse({ title: formData.get('title') })
  if (!parsed.success) {
    return { error: parsed.error.flatten().fieldErrors.title?.[0] ?? 'Invalid' }
  }
  await db.todo.create({ data: parsed.data })
  revalidateTag('todos', 'max')
  redirect('/todos') // throws — code after this does not run
}
```

### Forms with `useActionState` (React 19)

- `useActionState` (imported from **`react`**, not `react-dom`) wires a form to a Server Action and exposes `[state, formAction, isPending]`.

```tsx
'use client'
import { useActionState } from 'react'
import { createTodo } from './actions'

export function TodoForm() {
  const [state, formAction, isPending] = useActionState(createTodo, null)
  return (
    <form action={formAction}>
      <input name="title" required />
      {state?.error && <p role="alert">{state.error}</p>}
      <button disabled={isPending}>{isPending ? 'Saving…' : 'Add'}</button>
    </form>
  )
}
```

### `useFormStatus` for nested submit buttons

- Reads the pending state of the **enclosing** `<form>`. Must be in a child component of the form.

```tsx
'use client'
import { useFormStatus } from 'react-dom' // note: react-dom, not react
export function SubmitButton() {
  const { pending } = useFormStatus()
  return <button disabled={pending}>{pending ? 'Submitting…' : 'Submit'}</button>
}
```

### Optimistic UI with `useOptimistic`

```tsx
'use client'
import { useOptimistic } from 'react'
import { addLike } from './actions'

export function Likes({ count }: { count: number }) {
  const [optimistic, addOptimistic] = useOptimistic(count, (c, delta: number) => c + delta)
  return (
    <form action={async () => { addOptimistic(1); await addLike() }}>
      <button>♥ {optimistic}</button>
    </form>
  )
}
```

### Server Action security

- Every Server Action is a **public POST endpoint**. Always re-authenticate and re-authorize inside the action — never trust that the UI hid the button.
- Validate all input (e.g. Zod). Never pass raw `FormData` straight to the DB.
- Do not expose a Server Action that takes an object id without an ownership check.

## State Management

Decide by scope. Reach for the lightest tool that fits.

- **Server state (data from your backend):** the App Router IS your data layer. Fetch in Server Components + cache with `'use cache'`. Do not mirror server data into a client store.
- **URL state (filters, tabs, pagination, sort):** put it in the URL via `searchParams` + `useRouter`/`<Link>`. Shareable, back-button-friendly, SSR-able. Prefer this over local state for anything a user might want to bookmark.
- **Local UI state:** `useState`/`useReducer` in a Client Component. Keep it colocated.
- **Cross-tree client state (theme, cart drawer, auth UI):** React Context via a Client provider mounted in the root layout. Keep providers thin.
- **Complex global client state:** **Zustand** (simple, unopinionated) or **Jotai** (atomic). Avoid Redux for new projects unless the team already standardizes on it.
- **Client-side server-cache (if you fetch on the client):** **TanStack Query**. Only needed for highly interactive client-fetching (infinite scroll, polling); most apps don't need it with RSC.

```tsx
// Context provider pattern (root layout)
'use client'
import { createContext, useContext, useState, type ReactNode } from 'react'

type CartState = ReturnType<typeof useState<string[]>>
const CartCtx = createContext<CartState | null>(null)

export function CartProvider({ children }: { children: ReactNode }) {
  const value = useState<string[]>([])
  return <CartCtx.Provider value={value}>{children}</CartCtx.Provider>
}
export function useCart() {
  const ctx = useContext(CartCtx)
  if (!ctx) throw new Error('useCart must be used inside CartProvider')
  return ctx
}
```

```tsx
// URL-as-state (preferred for filters)
'use client'
import { useRouter, useSearchParams, usePathname } from 'next/navigation'
export function SortSelect() {
  const router = useRouter()
  const pathname = usePathname()
  const params = useSearchParams()
  return (
    <select
      defaultValue={params.get('sort') ?? 'new'}
      onChange={(e) => {
        const next = new URLSearchParams(params)
        next.set('sort', e.target.value)
        router.push(`${pathname}?${next}`)
      }}
    >
      <option value="new">Newest</option>
      <option value="price">Price</option>
    </select>
  )
}
```

- **Anti-patterns:** global stores holding server data that RSC already owns; `useEffect` to fetch initial data (fetch on the server instead); Context for high-frequency updates (causes broad re-renders — use Zustand/Jotai).

## Project Structure

- Colocate by feature; keep `app/` about routing, push logic into `lib/`, `components/`, and per-feature folders. Use the `@/*` path alias.

```
src/
  app/
    (marketing)/            # route group, not in URL
      page.tsx
      layout.tsx
    (app)/
      layout.tsx            # authed shell
      dashboard/
        page.tsx
        loading.tsx
        error.tsx
      products/
        page.tsx
        [id]/page.tsx
        actions.ts          # Server Actions for this feature
        _components/        # private folder (underscore = not routable)
          product-card.tsx
    api/
      webhooks/stripe/route.ts
    layout.tsx              # root layout (html/body, providers)
  components/               # shared, presentational, reusable UI
    ui/                     # design-system primitives (Button, Input)
  lib/
    db.ts                   # DB client singleton
    auth.ts                 # session helpers
    validations/            # Zod schemas shared client+server
  hooks/                    # shared client hooks
  types/                    # shared TS types
```

- **Private folders** (`_components`, `_lib`): prefixed with `_`, excluded from routing — use for feature-local, non-routable files.
- **Root layout** must render `<html>` and `<body>` and is where global providers live.
- Keep `page.tsx` thin: fetch + compose. Push rendering into `_components`, logic into `lib`.
- Share Zod schemas between Server Actions and client validation from `lib/validations`.

```tsx
// src/app/layout.tsx — root layout
import type { ReactNode } from 'react'
import './globals.css'
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}
```

## Rendering & Performance Rules

- Default to Server Components; add `'use client'` only at interactivity leaves. Never mark a whole page `'use client'` to fix one button.
- Use `next/image` for images (automatic optimization, lazy loading, correct sizing) and `next/font` for fonts (self-hosted, zero layout shift).
- Prefetch is automatic on `<Link>` in viewport; keep it for internal nav. v16 dedupes shared layouts and prefetches incrementally, so many links on a page are cheap.
- Stream slow sections with `<Suspense>`; give slow segments a `loading.tsx`.
- Avoid request waterfalls: `Promise.all` independent fetches; move data fetching up so siblings don't block each other.
- Keep client bundles small: don't import heavy libs (date, markdown, charting) into Client Components when the work can be done in a Server Component.
- Don't leak secrets: env vars without `NEXT_PUBLIC_` are server-only. Anything referenced in a Client Component with a `NEXT_PUBLIC_` prefix is shipped to the browser.

## Error & Loading Boundaries

```tsx
// app/dashboard/error.tsx — must be a Client Component
'use client'
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string }
  reset: () => void
}) {
  return (
    <div role="alert">
      <p>Something went wrong.</p>
      <button onClick={reset}>Retry</button>
    </div>
  )
}
```

```tsx
// app/dashboard/loading.tsx — auto Suspense fallback for the segment
export default function Loading() {
  return <DashboardSkeleton />
}
```

- `error.tsx` catches errors in its segment's `page` and children — **not** its own `layout`/`template` (those bubble to the parent boundary; the root layout is covered only by `global-error.tsx`).
- `reset()` re-renders the boundary's children without re-fetching. In v16.2+, `unstable_retry()` is also passed and re-fetches + re-renders (preferred once stable); wire whichever fits, `reset` is the safe default today.
- `notFound()` renders the nearest `not-found.tsx`.

## Quick Decision Cheatsheet

- Need interactivity / hooks / browser APIs? → Client Component (`'use client'`), kept small.
- Just rendering data? → Server Component (default).
- Fetching your own data? → `await` in a Server Component. Slow? → wrap in `<Suspense>`.
- Mutating data? → Server Action; then `updateTag` (read-your-writes) or `revalidateTag(tag, 'max')` / `revalidatePath`.
- Form? → `<form action={serverAction}>` + `useActionState` (from `react`) + `useFormStatus` (from `react-dom`).
- Shareable filter/tab state? → URL `searchParams`.
- Global client state? → Context (light) or Zustand/Jotai (heavy).
- Cache shared, non-personalized data? → `'use cache'` + `cacheLife` + `cacheTag`.
- Public API / webhook? → Route Handler (`route.ts`), invalidate with `revalidateTag(tag, 'max')`.
