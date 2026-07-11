<!-- keywords: react query, tanstack query v5, keepPreviousData, swr, rsc, server components, data fetching, caching, staleTime, mutations, optimistic update, invalidateQueries, infinite query, cursor pagination, forms, zod 4, z.email, react hook form, useActionState, useFormStatus, server actions, form validation, fetch wrapper, rest api, api error handling, abort signal, AbortSignal.any, idempotency key, cors, authentication, httponly cookie, jwt, session, refresh token rotation, csrf, samesite, oauth pkce, route protection, data access layer, core web vitals, lcp, inp, cls, web-vitals, scheduler.yield, useTransition, useOptimistic, seo, metadata api, generateMetadata, sitemap, robots, json-ld, structured data, canonical url, next.js 15, app router, streaming suspense, revalidateTag, revalidatePath, progressive enhancement -->

# Web Data, Auth & Performance

Dense reference for data fetching/caching, forms + validation, backend API calls, auth, Core Web Vitals, and SEO. Targets 2026 stacks: React 19, Next.js 15 App Router (RSC), TanStack Query v5, SWR 2, Zod 4. Every snippet is idiomatic and compiles.

## Decision rules: how to fetch

- **Server-rendered app (Next App Router, Remix/React Router 7, TanStack Start)** → fetch on the server (RSC / loaders). Data arrives in HTML: no client waterfall, no spinner, no API token in the browser.
- **Client-side interactive data (dashboards, infinite lists, polling, cross-component cache)** → TanStack Query or SWR.
- **Both** → server-render the first paint, then hydrate TanStack Query for client interactivity (`HydrationBoundary`).
- **Never** `useEffect(() => fetch())` for data you can fetch on the server or with a query library. It causes waterfalls, races, no caching, no dedup.
- Rule of thumb: *reads* go through a cache (Query/SWR/RSC cache); *writes* go through explicit mutations that invalidate reads.

## RSC data fetching (Next.js App Router)

- Server Components are `async`. `fetch` is deduped per-request and cache-controlled via options. No `useState`/`useEffect`.
- In Next 15, `fetch` defaults to **uncached** (`no-store`) unless you opt in with `cache: 'force-cache'` or `next: { revalidate }`. Route segments are also dynamic by default. Set caching explicitly.

```tsx
// app/products/[id]/page.tsx — Server Component
import { notFound } from 'next/navigation';

async function getProduct(id: string) {
  const res = await fetch(`https://api.example.com/products/${id}`, {
    // Cache + revalidate every 60s (ISR-style). Use { cache: 'no-store' } for always-dynamic.
    next: { revalidate: 60, tags: [`product-${id}`] },
  });
  if (res.status === 404) notFound();
  if (!res.ok) throw new Error(`Failed: ${res.status}`);
  return res.json() as Promise<Product>;
}

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params; // params is a Promise in Next 15
  const product = await getProduct(id);
  return <h1>{product.name}</h1>;
}
```

- **Parallel, not sequential.** Kick off independent requests together to avoid server waterfalls:

```tsx
const [user, orders] = await Promise.all([getUser(id), getOrders(id)]);
```

- **Stream slow data** with `<Suspense>` so the shell paints immediately:

```tsx
import { Suspense } from 'react';

export default function Page() {
  return (
    <>
      <Header />
      <Suspense fallback={<ReviewsSkeleton />}>
        <Reviews /> {/* async Server Component; streams in when ready */}
      </Suspense>
    </>
  );
}
```

- **On-demand revalidation** after a write (from a Server Action or route handler):

```ts
import { revalidateTag, revalidatePath } from 'next/cache';
revalidateTag(`product-${id}`); // busts every fetch tagged with it
revalidatePath('/products');    // busts a route
```

- `cookies()`, `headers()`, `searchParams` are dynamic and async — awaiting them opts the route out of static rendering. Keep them at the leaf that needs them.

## TanStack Query v5 (client cache)

- One `QueryClient`, provided once. Set sane defaults; `staleTime` is the single most impactful knob (how long data is considered fresh → no refetch).

```tsx
'use client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 60_000,        // 1 min fresh; avoids refetch storms
            gcTime: 5 * 60_000,       // cache retained 5 min after unused (was cacheTime in v4)
            retry: 2,
            refetchOnWindowFocus: true,
          },
        },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
```

- **Query keys are the cache identity.** Structure them hierarchically and include every input the query depends on.

```tsx
'use client';
import { useQuery, keepPreviousData } from '@tanstack/react-query';

function useProducts(filters: { category: string; page: number }) {
  return useQuery({
    queryKey: ['products', filters], // objects are deep-compared; changing filters = new cache entry
    queryFn: ({ signal }) =>
      fetch(`/api/products?category=${filters.category}&page=${filters.page}`, { signal }).then((r) => {
        if (!r.ok) throw new Error('Network error');
        return r.json() as Promise<Product[]>;
      }),
    placeholderData: keepPreviousData, // keep prior page visible while next loads (v5 replacement for keepPreviousData: true)
  });
}
```

- **v5 vs v4 gotchas:** single object signature only; `cacheTime` → `gcTime`; `keepPreviousData: true` → `placeholderData: keepPreviousData` (imported helper); `isLoading` is now derived — use `isPending` (no data yet) vs `isFetching` (any in-flight fetch); dedicated `useSuspenseQuery`.
- **The `signal`** is wired to abort on unmount/key-change — always forward it to `fetch` to cancel stale requests.

### Mutations + cache invalidation

```tsx
'use client';
import { useMutation, useQueryClient } from '@tanstack/react-query';

function useAddToCart() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (item: CartItem) =>
      fetch('/api/cart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(item),
      }).then((r) => {
        if (!r.ok) throw new Error('Failed');
        return r.json();
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['cart'] }); // refetch reads that changed
    },
  });
}
```

### Optimistic updates (snapshot → rollback → reconcile)

```tsx
useMutation({
  mutationFn: updateTodo,
  onMutate: async (next) => {
    await qc.cancelQueries({ queryKey: ['todos'] });     // stop in-flight refetches racing us
    const prev = qc.getQueryData<Todo[]>(['todos']);     // snapshot for rollback
    qc.setQueryData<Todo[]>(['todos'], (old) =>
      old?.map((t) => (t.id === next.id ? next : t)),
    );
    return { prev };
  },
  onError: (_e, _next, ctx) => qc.setQueryData(['todos'], ctx?.prev), // rollback
  onSettled: () => qc.invalidateQueries({ queryKey: ['todos'] }),     // reconcile with server
});
```

### Infinite / paginated lists

```tsx
import { useInfiniteQuery } from '@tanstack/react-query';

useInfiniteQuery({
  queryKey: ['feed'],
  queryFn: ({ pageParam }) => fetchFeed(pageParam),
  initialPageParam: 0,
  getNextPageParam: (lastPage) => lastPage.nextCursor ?? undefined, // undefined => hasNextPage false
});
```

### Suspense + server hydration (RSC + Query together)

```tsx
// Server Component: prefetch, then dehydrate into the client boundary
import { dehydrate, HydrationBoundary, QueryClient } from '@tanstack/react-query';

export default async function Page() {
  const qc = new QueryClient();
  await qc.prefetchQuery({ queryKey: ['products', {}], queryFn: fetchProducts });
  return (
    <HydrationBoundary state={dehydrate(qc)}>
      <ProductList /> {/* client component uses useQuery; data already warm, no fetch flash */}
    </HydrationBoundary>
  );
}
```

- `useSuspenseQuery` returns non-nullable `data` (no `isPending` branch) — pair with a `<Suspense>` boundary and an error boundary. It has no `enabled` option; use component composition for dependent/conditional queries.

## SWR (lighter alternative)

```tsx
'use client';
import useSWR, { useSWRConfig } from 'swr';

const fetcher = (url: string) =>
  fetch(url).then((r) => {
    if (!r.ok) throw new Error('Request failed');
    return r.json();
  });

function Profile() {
  const { data, error, isLoading } = useSWR('/api/user', fetcher, {
    revalidateOnFocus: true,
    dedupingInterval: 2000,
  });
  if (isLoading) return <Spinner />;
  if (error) return <ErrorState />;
  return <div>{data.name}</div>;
}

// Mutation + revalidation
function useUpdate() {
  const { mutate } = useSWRConfig();
  return async (patch: Partial<User>) => {
    await fetch('/api/user', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    mutate('/api/user'); // revalidate the key
  };
}
```

- **Query vs SWR:** SWR is smaller and simpler (key = URL string). TanStack Query has richer mutation/optimistic tooling, `invalidateQueries` key-matching, infinite queries, and devtools. Pick SWR for simple read-mostly UIs; Query for complex client state.

## Talking to a backend API

### A typed fetch wrapper (do this once)

```ts
// lib/api.ts
type ApiError = { status: number; message: string; details?: unknown };

export async function api<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const { json, headers, ...rest } = init;
  const res = await fetch(`${process.env.NEXT_PUBLIC_API_URL}${path}`, {
    ...rest,
    headers: {
      ...(json !== undefined && { 'Content-Type': 'application/json' }),
      ...headers,
    },
    body: json !== undefined ? JSON.stringify(json) : rest.body,
    credentials: 'include', // send httpOnly cookies cross-origin (needs CORS allow-credentials)
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw {
      status: res.status,
      message: body.message ?? res.statusText,
      details: body.details,
    } satisfies ApiError;
  }
  // 204 No Content / empty body
  return (res.status === 204 ? undefined : await res.json()) as T;
}
```

- **Timeouts:** `fetch` has no default timeout. Use `AbortSignal.timeout(ms)` for the signal, or `AbortSignal.any([userSignal, AbortSignal.timeout(10_000)])` to combine a caller's signal with a deadline (both Baseline in 2026).
- **Retries:** retry only idempotent methods (GET/PUT/DELETE) and only on `5xx`/network errors, with exponential backoff + jitter. Never blind-retry POST (double-charge risk) unless you send an idempotency key.
- **Idempotency:** for POSTs that create resources, send `Idempotency-Key: <uuid>` so retries don't duplicate.
- **Status handling:** `4xx` = client fault, do not retry (surface to user). `401` = re-auth. `403` = forbidden. `409` = conflict (show merge/refresh). `429` = respect the `Retry-After` header.
- **Pagination:** prefer **cursor-based** (`?cursor=`) over offset for large/live datasets — stable under inserts, O(1) DB seeks. Offset (`?page=`) is fine for small, static tables.

### CORS essentials (cross-origin API)

- Server must send `Access-Control-Allow-Origin: https://app.example.com` (exact origin, not `*`, when credentials are used).
- With cookies, server needs `Access-Control-Allow-Credentials: true` **and** client needs `credentials: 'include'`.
- Preflight (`OPTIONS`) fires for non-simple requests (custom headers, `PUT`/`PATCH`/`DELETE`, non-form content types like JSON). Cache it with `Access-Control-Max-Age`.

## Forms + validation

### Schema-first with Zod 4 (share client + server)

```ts
// lib/schemas.ts — one schema, reused on client and server
import { z } from 'zod';

export const signupSchema = z
  .object({
    email: z.email('Enter a valid email'),          // Zod 4 top-level format; z.string().email() is deprecated
    password: z.string().min(8, 'At least 8 characters'),
    confirm: z.string(),
    age: z.coerce.number().int().min(18, 'Must be 18+'), // coerce: FormData values are strings
  })
  .refine((d) => d.password === d.confirm, {
    message: 'Passwords do not match',
    path: ['confirm'],
  });

export type SignupInput = z.infer<typeof signupSchema>;
```

### React 19 + Server Actions + `useActionState` (progressive-enhancement path)

```tsx
// app/actions.ts
'use server';
import { signupSchema } from '@/lib/schemas';

export type FormState = { error?: string; fieldErrors?: Record<string, string[]> };

export async function signup(_prev: FormState, formData: FormData): Promise<FormState> {
  const parsed = signupSchema.safeParse(Object.fromEntries(formData)); // fine for single-value fields
  if (!parsed.success) {
    return { fieldErrors: z.flattenError(parsed.error).fieldErrors };  // Zod 4: z.flattenError(err)
  }
  // ... create user; on server-detected error return a message
  // redirect('/welcome')  // redirect() throws internally; don't wrap in a try/catch that swallows it
  return {};
}
```

```tsx
// app/signup/form.tsx
'use client';
import { useActionState } from 'react'; // React 19: replaces useFormState (which was in react-dom)
import { useFormStatus } from 'react-dom';
import { signup } from '../actions';

function Submit() {
  const { pending } = useFormStatus(); // must be rendered inside the <form>
  return <button disabled={pending}>{pending ? 'Creating…' : 'Sign up'}</button>;
}

export function SignupForm() {
  const [state, action] = useActionState(signup, {});
  return (
    <form action={action}>
      <input name="email" type="email" />
      {state.fieldErrors?.email && <p role="alert">{state.fieldErrors.email[0]}</p>}
      <input name="password" type="password" />
      <input name="confirm" type="password" />
      <input name="age" type="number" />
      {state.error && <p role="alert">{state.error}</p>}
      <Submit />
    </form>
  );
}
```

- This form **works without JS** (native `action`) and upgrades to async when hydrated. The Server Action runs on the server — no API endpoint to write, credentials never touch the client.

### React Hook Form + Zod (rich client-side UX)

```tsx
'use client';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { signupSchema, type SignupInput } from '@/lib/schemas';

export function Signup() {
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<SignupInput>({ resolver: zodResolver(signupSchema), mode: 'onBlur' });

  const onSubmit = handleSubmit(async (data) => {
    await fetch('/api/signup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
  });

  return (
    <form onSubmit={onSubmit}>
      <input {...register('email')} aria-invalid={!!errors.email} />
      {errors.email && <p role="alert">{errors.email.message}</p>}
      <input type="password" {...register('password')} />
      {errors.password && <p role="alert">{errors.password.message}</p>}
      <button disabled={isSubmitting}>Sign up</button>
    </form>
  );
}
```

- **Always re-validate on the server.** Client validation is UX, not security. The same Zod schema runs in the Server Action / API route as the trust boundary.
- **Controlled vs uncontrolled:** RHF is uncontrolled (fast, few re-renders). Prefer it for large forms. Reach for controlled state only for fields needing live derived UI.
- **Accessibility:** associate labels (`htmlFor`/`id`), set `aria-invalid`, put errors in `role="alert"` / `aria-describedby`, and let native validation attributes (`required`, `type="email"`) run as a baseline.

## Authentication & sessions

### httpOnly cookies vs JWT-in-JS — the core tradeoff

| Concern | httpOnly cookie session | JWT in localStorage / JS memory |
| --- | --- | --- |
| XSS token theft | **Safe** — JS can't read `httpOnly` cookies | **Exposed** — any XSS reads the token |
| CSRF | Needs protection (SameSite + token) | Not applicable (not auto-sent) |
| Revocation | Easy (server-side session store) | Hard (stateless until expiry) |
| Cross-domain APIs | Needs CORS credentials config | Trivial (`Authorization` header) |
| SSR access | Server reads cookie directly | Token not on server unless forwarded |

**Default recommendation (2026): store the session token in an `httpOnly`, `Secure`, `SameSite` cookie.** It removes XSS token exfiltration, the biggest risk for browser apps. Use raw JWTs in `Authorization` headers for machine-to-machine / mobile / third-party API consumers, not for first-party web sessions.

### Setting a secure session cookie (Next.js route handler / Server Action)

```ts
import { cookies } from 'next/headers';

export async function setSession(token: string) {
  (await cookies()).set('session', token, {
    httpOnly: true,       // not readable by document.cookie → blocks XSS theft
    secure: true,         // HTTPS only
    sameSite: 'lax',      // sent on top-level navigation; blocks most CSRF. 'strict' = max; 'none' requires Secure + cross-site
    path: '/',
    maxAge: 60 * 60 * 24 * 7, // 7 days
  });
}
```

- **`SameSite=Lax`** is the pragmatic default: cookies ride along on top-level GET navigations (so login persists across links) but not on cross-site POST/`fetch`, killing classic CSRF. Use `Strict` for high-value admin surfaces. `None` (required for cross-site) mandates `Secure`.
- **Session ID vs. JWT contents:** keep an opaque session ID pointing to server state when you need instant revocation and small cookies. Store a signed JWT in the cookie when you want stateless verification — but you lose easy revocation (mitigate with short expiry + refresh).

### Access + refresh token pattern

- **Access token:** short-lived (5–15 min), used to authorize requests.
- **Refresh token:** long-lived, `httpOnly` cookie, single purpose = mint new access tokens. Rotate on every use and detect reuse (a replayed old refresh token = revoke the whole family; it signals theft).
- On `401`, silently hit `/auth/refresh`; if that fails, redirect to login. Queue concurrent requests during a refresh so you refresh once, not N times.

### CSRF protection (needed when using cookies)

- `SameSite=Lax/Strict` blocks the common case. For defense-in-depth on state-changing requests, add the **double-submit** or **synchronizer token** pattern:

```ts
// Server sets a CSRF token in a readable cookie; client echoes it in a header.
// Server compares header == cookie. An attacker's cross-site request can't read the cookie to set the header.
const csrf = req.headers.get('x-csrf-token');
const cookie = req.cookies.get('csrf')?.value;
if (!csrf || csrf !== cookie) return new Response('Forbidden', { status: 403 });
```

- Only protect **mutating** methods (POST/PUT/PATCH/DELETE). Safe methods (GET/HEAD) must not change state.

### Route protection

- **Middleware** for coarse redirects (unauthenticated → `/login`); it runs before render. **Do not** treat middleware as your only authz — always re-check on the data layer.
- **Verify in the data access layer** (every query/mutation checks the session). This is the real security boundary; the UI/redirect is convenience.

```ts
// lib/auth.ts — call at the top of every protected server action / query
import { cache } from 'react';
import { cookies } from 'next/headers';

export const getSession = cache(async () => {
  const token = (await cookies()).get('session')?.value;
  if (!token) return null;
  return verifyToken(token); // returns { userId, roles } | null
});

export async function requireUser() {
  const s = await getSession();
  if (!s) throw new Error('UNAUTHORIZED');
  return s;
}
```

- `cache()` dedupes `getSession` across a single server request so you verify once.
- **OAuth/OIDC:** don't hand-roll it. Use a maintained library (Auth.js/NextAuth, Better Auth, Clerk, WorkOS, or your IdP's SDK). Use the **authorization code flow with PKCE** for SPAs/mobile; never the implicit flow (deprecated).
- Store secrets server-side. `NEXT_PUBLIC_*` env vars are shipped to the browser — never put API secrets, DB URLs, or signing keys there.

## Core Web Vitals (2026)

Google measures at the **75th percentile** of real users (field data). All three must be "Good" to pass.

| Metric | Measures | Good | Needs work | Poor |
| --- | --- | --- | --- | --- |
| **LCP** (Largest Contentful Paint) | Loading — largest element painted | ≤ 2.5s | 2.5–4.0s | > 4.0s |
| **INP** (Interaction to Next Paint) | Responsiveness — worst interaction latency | ≤ 200ms | 200–500ms | > 500ms |
| **CLS** (Cumulative Layout Shift) | Visual stability — unexpected shifts | ≤ 0.1 | 0.1–0.25 | > 0.25 |

- INP replaced FID (March 2024) and measures *every* interaction's full input→paint latency, keeping the worst. It's the most-failed vital in 2026.
- **Lab vs field:** Lighthouse/PSI lab scores ≠ field CrUX data. Optimize for field (real users). Measure locally with the `web-vitals` library.

### LCP wins

- **Prioritize the LCP image:** `priority` (Next `<Image>`) / `fetchpriority="high"`, and preload it. Never lazy-load the hero.
- Serve responsive, modern formats (AVIF/WebP) with correct `sizes`. Use a CDN/image optimizer.
- Eliminate render-blocking resources: inline critical CSS, defer non-critical JS, `preconnect` to critical origins.
- Server-render / stream so the LCP element is in the initial HTML, not painted after JS hydration.
- Fast TTFB: cache at the edge, use ISR/SSG for stable pages.

```tsx
import Image from 'next/image';
<Image src="/hero.avif" alt="" width={1200} height={600} priority sizes="100vw" />;
```

### INP wins

- **Break up long tasks.** Yield to the main thread so queued interactions can run: `await scheduler.yield()` (Chromium + Firefox; not yet in Safari, so feature-detect or fall back to `setTimeout`/`isInputPending`).
- Debounce/throttle expensive handlers; move heavy compute to a **Web Worker**.
- Reduce hydration cost: ship less JS, use RSC / islands so interactive code is small. Code-split with `next/dynamic` / `React.lazy`.
- Use `useTransition` for non-urgent state updates so typing/clicks stay responsive:

```tsx
const [isPending, startTransition] = useTransition();
startTransition(() => setFilter(next)); // keeps input responsive while the list re-renders
```

- Provide immediate visual feedback (optimistic UI, `useOptimistic`) so perceived latency stays low even on a slow network.

### CLS wins

- **Always reserve space for media.** Set `width`/`height` (or `aspect-ratio`) on images/video/iframes so the browser reserves the box.
- Reserve space for ads/embeds/injected banners with `min-height`.
- Preload fonts, use `font-display: optional|swap` + `size-adjust` to avoid font-swap reflow. Prefer `next/font` (self-hosts + sets metrics automatically).
- Never insert content above existing content after load (cookie bars, notices) — overlay it or reserve space.
- Animate with CSS `transform`/`opacity` (compositor-only, no layout).

```css
img, video { aspect-ratio: 16 / 9; width: 100%; height: auto; }
```

## SEO basics

- **Rendering:** crawlers index SSR/SSG HTML reliably; heavy client-only rendering risks partial/late indexing. Server-render content you want ranked.
- **One `<h1>` per page**, semantic headings, meaningful `<a href>` links (not `onClick` divs) so crawlers follow them.
- **Metadata** — unique `<title>` (≤ ~60 chars) and `meta description` per page. In Next App Router use the Metadata API:

```tsx
// app/products/[id]/page.tsx
import type { Metadata } from 'next';

export async function generateMetadata({ params }: { params: Promise<{ id: string }> }): Promise<Metadata> {
  const { id } = await params;
  const p = await getProduct(id);
  return {
    title: `${p.name} — Acme`,
    description: p.summary,
    alternates: { canonical: `https://acme.com/products/${id}` },
    openGraph: { title: p.name, images: [p.image], type: 'website' },
    twitter: { card: 'summary_large_image' },
  };
}
```

- **Canonical URLs** to dedupe (trailing slash, query params, `www`/non-`www`, http/https). Pick one host and 301 the rest.
- **`sitemap.xml` + `robots.txt`.** Next generates both from code:

```ts
// app/sitemap.ts
import type { MetadataRoute } from 'next';
export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const products = await getAllProductIds();
  return products.map((id) => ({
    url: `https://acme.com/products/${id}`,
    lastModified: new Date(),
    changeFrequency: 'weekly',
    priority: 0.8,
  }));
}
```

```ts
// app/robots.ts
import type { MetadataRoute } from 'next';
export default function robots(): MetadataRoute.Robots {
  return {
    rules: { userAgent: '*', allow: '/', disallow: '/admin/' },
    sitemap: 'https://acme.com/sitemap.xml',
  };
}
```

- **Structured data (JSON-LD)** for rich results (Product, Article, FAQ, Breadcrumb). Emit it server-side:

```tsx
export default function ProductJsonLd({ product }: { product: Product }) {
  const data = {
    '@context': 'https://schema.org',
    '@type': 'Product',
    name: product.name,
    image: product.image,
    offers: { '@type': 'Offer', price: product.price, priceCurrency: 'USD' },
  };
  return <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(data) }} />;
}
```

- **Core Web Vitals are a ranking signal** — the performance section above is also SEO work.
- **Other essentials:** descriptive `alt` text (accessibility + image search), clean human-readable slugs, `hreflang` for i18n, `noindex` thin/duplicate/paginated-noise pages, keep redirects to a single 301 hop, and ship a mobile-friendly responsive layout (mobile-first indexing).

## Cross-cutting checklist

- [ ] Reads cached (Query/SWR/RSC), writes invalidate reads; no `useEffect` fetching.
- [ ] Every `fetch` handles `!res.ok`, applies a timeout (`AbortSignal.timeout`), and forwards the abort `signal`.
- [ ] One shared Zod schema validates on **both** client (UX) and server (trust boundary).
- [ ] Session token in `httpOnly` + `Secure` + `SameSite` cookie; short-lived access + rotating refresh; authz re-checked in the data layer, not just middleware.
- [ ] Secrets server-only; nothing sensitive in `NEXT_PUBLIC_*`.
- [ ] LCP image prioritized, media has reserved dimensions, long tasks broken up, JS payload minimal.
- [ ] Every indexable page: SSR/SSG HTML, unique title/description, canonical, in the sitemap; JSON-LD where it earns rich results.
