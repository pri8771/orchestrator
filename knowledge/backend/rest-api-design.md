<!-- keywords: rest api design, http status codes, resource modeling, url conventions, http verbs, pagination, cursor pagination, filtering, sorting, api versioning, idempotency key, idempotent requests, error envelope, rfc 9457 problem details, request response shape, api contract, openapi, conditional requests etag, content negotiation, rate limiting headers, partial update patch, json merge patch, bulk operations, field selection sparse fieldsets, http caching, api authentication, collection resource, sub-resource, http query method, long-running async jobs, webhooks, uuidv7, optimistic concurrency -->

# REST/HTTP API Design Reference

Practical, current (2026) conventions for designing a clean HTTP API consumed by mobile and web clients. Optimize for: predictable URLs, correct status codes, stable contracts, cheap client caching, and safe retries.

## Core Principles

- **Resources are nouns, operations are HTTP verbs.** The URL identifies *what*, the method says *what to do with it*. Never put verbs in paths (`/getUser`, `/createOrder` are wrong).
- **Be consistent over clever.** A predictable API a client can guess beats a "RESTfully pure" one they must look up.
- **JSON over HTTPS** is the default. `Content-Type: application/json; charset=utf-8`. Use UTF-8 everywhere.
- **Design for the client's screen, not your DB schema.** Resource shapes should map to what a mobile/web view needs, minimizing round-trips.
- **Make the common case one request.** Support embedding/expansion and field selection so clients avoid N+1 fetches.
- **Everything is versioned and contract-first.** Publish an OpenAPI spec; treat it as the source of truth.

## Resource Modeling

- Model **collections** and **items**: `/articles` (collection) and `/articles/{id}` (item).
- Use **plural nouns** for collections consistently: `/users`, `/orders`, `/payments`.
- **Sub-resources express containment/ownership**, not every relationship:
  - `/users/{id}/orders` — orders belonging to a user (scoped list).
  - `/orders/{id}/line-items/{lineId}` — nest at most ~2 levels deep. Beyond that, flatten and filter: `/line-items?orderId=...`.
- Prefer **flat top-level resources with filters** over deep nesting for anything queried across parents.
- **Singleton sub-resources** for 1:1 config-like data: `/users/{id}/settings`, `/account/preferences`. No collection, just GET/PUT/PATCH.
- **Identifiers**: use opaque, stable IDs. Prefer **UUIDv7** (RFC 9562 — time-ordered, index-friendly) or ULIDs over auto-increment integers — non-enumerable, shardable, and don't leak row counts. Never expose sequential PKs to untrusted clients.
- **URL slugs** are fine for humans but treat the canonical key as the ID: `/articles/{id}` canonical; `/articles/{id}/{slug}` optional for SEO.
- Use `kebab-case` in path segments (`/shipping-addresses`), `snake_case` **or** `camelCase` in JSON bodies — pick one per API and never mix. `camelCase` is the common default for JS/Swift clients.

### Actions that don't fit CRUD

Some operations are genuinely verbs (state transitions, computations). Two acceptable patterns:

- **Sub-resource as state**: model the transition as a resource.
  - `POST /orders/{id}/cancellation` or `POST /orders/{id}/refunds` (creates a refund record — auditable, idempotency-friendly).
- **Controller/action endpoint** when there's no natural resource:
  - `POST /articles/{id}/actions/publish`, `POST /carts/{id}/actions/checkout`.
- Prefer creating a resource (`/refunds`) over a bare RPC verb when the action produces a record you'd want to list or audit.

## HTTP Verbs & Semantics

| Verb | Purpose | Safe | Idempotent | Body | Success |
|------|---------|------|------------|------|---------|
| GET | Read resource/collection | Yes | Yes | No | 200 |
| POST | Create; non-idempotent action | No | No | Yes | 201 / 200 / 202 |
| PUT | Full replace (or create-at-known-id) | No | Yes | Yes | 200 / 201 / 204 |
| PATCH | Partial update | No | No* | Yes | 200 / 204 |
| DELETE | Remove | No | Yes | No | 204 / 200 |
| QUERY | Safe read with a request body | Yes | Yes | Yes | 200 |
| HEAD | Headers only (existence/caching) | Yes | Yes | No | 200 |
| OPTIONS | Capabilities / CORS preflight | Yes | Yes | No | 204 / 200 |

- **Safe** = no observable state change. **Idempotent** = same effect whether called once or N times.
- `GET` must never mutate state. Don't accept side-effecting query params.
- `PUT` replaces the entire resource — omitted fields are cleared/reset. Use `PATCH` for partial updates.
- `PATCH` is *not* inherently idempotent (e.g. `{"op":"increment"}`), but most field-set patches are effectively idempotent. Use idempotency keys if retries matter.
- `DELETE` is idempotent: deleting an already-deleted resource returns `204` **or** `404` — pick one policy and document it. Returning `204` on repeat delete is friendlier to retry logic.
- **Never use `GET` with a request body** — many proxies/CDNs strip it. For large or structured queries, prefer the **HTTP `QUERY` method** (safe + idempotent with a body; IESG-approved Proposed Standard, Nov 2025) where your stack supports it; otherwise fall back to `POST /resource/search`, documented as a query rather than a mutation. See *Filtering* below.

## Status Codes

Use the specific code; don't collapse everything to 200/500.

### 2xx Success
- `200 OK` — GET success; PUT/PATCH returning the updated body; POST action returning a result.
- `201 Created` — resource created. **Include a `Location` header** with the new resource URL and return the created body.
- `202 Accepted` — request accepted for async processing; work not done yet. Return a status/job URL (`Location`).
- `204 No Content` — success with empty body (typical for DELETE, and PUT/PATCH when you don't echo the resource).
- `207 Multi-Status` — per-item results in a batch (partial success). See *Bulk & Batch*.

### 3xx
- `301 Moved Permanently` / `308 Permanent Redirect` — resource moved. `308` guarantees the method/body are preserved on the follow-up; `301` historically allowed clients to switch `POST`→`GET`, so use `308` when the method must not change.
- `304 Not Modified` — conditional GET hit (`If-None-Match`/`If-Modified-Since`). Saves bandwidth on mobile.

### 4xx Client Errors
- `400 Bad Request` — malformed syntax, unparseable JSON, invalid query params. Not for domain validation of well-formed input.
- `401 Unauthorized` — missing/invalid/expired credentials. **Always send `WWW-Authenticate`.** (Misnamed: means *unauthenticated*.)
- `403 Forbidden` — authenticated but not permitted. Don't use `401` here.
- `404 Not Found` — resource doesn't exist. Also used deliberately to **hide existence** of private resources from unauthorized callers (prefer `404` over `403` so you don't leak that the resource exists — see *Authentication & Security*).
- `405 Method Not Allowed` — verb unsupported on this URL. **Must send `Allow` header** listing valid methods.
- `406 Not Acceptable` — can't satisfy `Accept` header.
- `409 Conflict` — state conflict: duplicate unique key, edit conflict, business-rule collision (e.g. "cannot cancel a shipped order").
- `410 Gone` — resource permanently removed (stronger than 404; helps clients purge caches).
- `412 Precondition Failed` — `If-Match`/`If-Unmodified-Since` failed (optimistic concurrency lost).
- `415 Unsupported Media Type` — `Content-Type` you don't accept.
- `422 Unprocessable Content` — **well-formed but semantically invalid** (validation errors on parseable JSON). The go-to for field-level validation failures. (Some teams use `400` for all client errors — acceptable if consistent; `422` is more precise. Renamed from "Unprocessable Entity" in RFC 9110; the number is unchanged.)
- `428 Precondition Required` — you require `If-Match` but the client didn't send it.
- `429 Too Many Requests` — rate limited. **Send `Retry-After`.**

### 5xx Server Errors
- `500 Internal Server Error` — unexpected server fault. Never leak stack traces to clients.
- `502 Bad Gateway` / `503 Service Unavailable` / `504 Gateway Timeout` — upstream/availability failures. `503` should include `Retry-After` when planned.

**Rule of thumb:** if the client can fix it by changing the request → 4xx; if it can't → 5xx. Auth failures are 401/403; input validation is 422 (or 400); state/business conflicts are 409.

## Request & Response Shapes

- **Envelope for collections, bare object for items** is a clean, common choice:

```json
// GET /articles/42
{
  "id": "art_01HXQ...",
  "title": "Designing APIs",
  "status": "published",
  "authorId": "usr_01HX...",
  "createdAt": "2026-06-30T14:00:00Z",
  "updatedAt": "2026-06-30T15:12:00Z"
}
```

```json
// GET /articles?limit=2
{
  "data": [ { "id": "art_1", "title": "..." }, { "id": "art_2", "title": "..." } ],
  "pagination": {
    "nextCursor": "eyJpZCI6ImFydF8yIn0",
    "hasMore": true
  }
}
```

- **Be consistent about the envelope.** Either always wrap in `{ "data": ... }` (easier to add metadata later) or never wrap items. Wrapping *collections* while returning *items* bare is a pragmatic middle ground many APIs use.
- **Timestamps**: ISO 8601 / RFC 3339 UTC with `Z` (`2026-06-30T14:00:00Z`). Suffix with `At` (`createdAt`). Send timezone-aware; let the client localize.
- **Money**: never floats. Use integer **minor units** (`"amount": 1099, "currency": "USD"`) or a decimal **string** (`"amount": "10.99"`). Document which.
- **Enums as lowercase strings** (`"status": "pending"`), not magic integers. Document the closed set; clients should tolerate unknown values gracefully (forward-compat).
- **Booleans stay booleans**; don't send `"true"`/`0`/`1`.
- **Nulls vs omitted**: decide a policy. Common: omit unknown/not-loaded fields; use explicit `null` for "known to be empty." Don't flip between them for the same field.
- **IDs are strings** even if numeric internally — avoids JS `Number` precision loss (>2^53) and lets you change ID format later.
- **Don't leak internal fields** (DB row versions, internal flags, PII you don't need). Serialize an explicit DTO, not your ORM entity.
- Echo a **`requestId`** (correlation ID) in responses and error bodies for support/debugging.

## Pagination

**Prefer cursor (keyset) pagination** for anything large, real-time, or deep. Offset pagination breaks (skips/dupes) when rows are inserted/deleted between pages and is O(n) slow at high offsets.

### Cursor pagination (recommended)
```
GET /articles?limit=20&cursor=eyJpZCI6ImFydF8yMDAifQ
```
```json
{
  "data": [ /* ... up to limit items ... */ ],
  "pagination": {
    "nextCursor": "eyJpZCI6ImFydF8yMjAifQ",
    "prevCursor": "eyJpZCI6ImFydF8xODAifQ",
    "hasMore": true
  }
}
```
- The cursor is an **opaque, base64url-encoded** token (e.g. encoding the last item's sort key + tiebreaker id). Clients must treat it as opaque — never construct or parse it.
- Sort by a **stable, unique** key (or `(sortField, id)` composite) so the keyset is deterministic.
- `nextCursor: null` / absent, or `hasMore: false`, signals the end.
- Don't return a total `count` for large sets — it's expensive and races. Offer a separate `/count` or approximate count only if genuinely needed.

### Offset/limit (acceptable for small, stable, admin-style lists)
```
GET /articles?page=2&pageSize=20      // or ?offset=20&limit=20
```
- Cap `limit`/`pageSize` (e.g. max 100) and apply a sane default (e.g. 20). Reject over-cap with `400`/`422`.

### Link header (alternative, RFC 8288)
```
Link: <https://api.ex.com/articles?cursor=abc>; rel="next",
      <https://api.ex.com/articles?cursor=xyz>; rel="prev"
```

**Rules:** always enforce a default and max page size; keep pagination params consistent across every collection endpoint; document the sort guarantee.

## Filtering, Sorting, Field Selection

- **Filter** via query params named after fields:
  - `GET /orders?status=paid&customerId=usr_1`
  - Multiple values: `?status=paid,shipped` (CSV) or repeated `?status=paid&status=shipped`. Pick one convention.
- **Ranges/operators** — keep readable; two common styles:
  - Suffix operators: `?createdAt[gte]=2026-01-01&price[lt]=5000`
  - Or dedicated params: `?minPrice=0&maxPrice=5000&createdAfter=2026-01-01`
- **Full-text/simple search**: `GET /articles?q=rest+design`.
- **Rich/structured queries** that exceed URL length limits or need nested JSON: use the **HTTP `QUERY` method** (safe + idempotent, so it's cacheable and retry-safe) where supported, or `POST /articles/search` with a JSON body as the widely deployed fallback. Document either as a read, not a mutation.
```
QUERY /articles
Content-Type: application/json

{ "filter": { "status": "published", "tags": ["rest", "design"] }, "sort": ["-createdAt"] }
```
- **Sorting**: `?sort=createdAt` (asc), `?sort=-createdAt` (desc, leading `-`). Multi-key: `?sort=-priority,createdAt`. Whitelist sortable fields.
- **Sparse fieldsets** (shrink mobile payloads): `?fields=id,title,author`. Return only requested fields.
- **Expansion/embedding** (avoid N+1): `?expand=author,comments` inlines related resources:
```json
{ "id": "art_1", "authorId": "usr_9",
  "author": { "id": "usr_9", "name": "Ada" } }
```
- **Whitelist everything** — filter/sort/expand/field names must be an explicit allowlist to prevent injection and accidental exposure of unindexed columns.

## Versioning

- **Version from day one.** Breaking changes are inevitable; give clients a stable contract to pin.
- **URL path versioning is the pragmatic default** — visible, cacheable, trivial to route: `/v1/articles`, `/v2/articles`. Use major version only (`v1`, not `v1.2`).
- **Header versioning** (`Accept: application/vnd.example.v2+json` or `X-API-Version: 2026-06-01`) keeps URLs clean and is more "pure," but is harder to test/cache/debug. Choose one and stick with it.
- **Date-based versions** (Stripe-style: `2026-06-01`) work well when clients pin a version and you roll many small changes.
- **Only bump the major version for breaking changes.** These are backward-compatible (no bump needed):
  - Adding a new endpoint, optional request field, or response field.
  - Adding a new enum value (clients must tolerate unknowns).
  - Making a required field optional.
- **Breaking** (needs new version): removing/renaming fields, changing types, changing status-code semantics, tightening validation, changing defaults, removing endpoints.
- **Deprecation**: signal with the `Deprecation` (RFC 9745) and `Sunset` (RFC 8594) HTTP response headers plus a `Link` to migration docs. Announce timelines; keep old majors alive for a documented window.

```
Deprecation: @1782777599
Sunset: Wed, 31 Dec 2026 23:59:59 GMT
Link: <https://api.ex.com/docs/v2-migration>; rel="deprecation"; type="text/html"
```

> `Deprecation` is a Structured Fields **Date** (a `@`-prefixed Unix timestamp), *not* `Deprecation: true` — that older form is non-conformant under RFC 9745. `Sunset` keeps the legacy HTTP-date format and its timestamp must not precede `Deprecation`.

## Idempotency

Non-idempotent operations (`POST` creating charges, orders, messages) must be **safely retryable** so mobile clients on flaky networks don't double-submit.

- Client sends a unique **`Idempotency-Key`** header (a client-generated UUID) on the request.
- Server stores the key → result mapping. On retry with the same key:
  - Return the **original response** (same status + body) without re-executing.
  - If the original is still **in progress**, return `409 Conflict` (or block briefly).
  - If the same key is reused with a **different request body**, return `422`/`400` (key reuse mismatch).
- Scope keys per endpoint + per authenticated principal. **Expire** stored keys (e.g. 24h).

```
POST /payments
Idempotency-Key: 3f1c2b8e-1a2b-4c3d-9e8f-7a6b5c4d3e2f
Content-Type: application/json

{ "amount": 5000, "currency": "USD", "source": "card_..." }
```

- `GET/PUT/DELETE` are already idempotent by HTTP semantics — no key needed.
- Persist the key **atomically with the effect** (same DB transaction) so a crash mid-request can't create the effect without recording the key.
- Document the header name, format, and retention window. (An IETF `Idempotency-Key` header draft exists; the header name and semantics above are the de-facto industry convention.)

## Optimistic Concurrency & Conditional Requests

Prevent lost updates when two clients edit the same resource.

- Server returns an **`ETag`** on GET (hash or version of the representation).
- Client sends **`If-Match: "<etag>"`** on PUT/PATCH/DELETE.
  - Match → apply, return new `ETag`.
  - Stale → **`412 Precondition Failed`** (someone else changed it; client refetches & retries).
- Use **`If-None-Match`** on GET for caching → `304 Not Modified` when unchanged (saves mobile bandwidth/battery).
- `Last-Modified` + `If-Modified-Since`/`If-Unmodified-Since` is the weaker time-based alternative.
- Optionally require conditional writes with `428 Precondition Required` when a client omits `If-Match`.

```
GET /articles/42            → 200, ETag: "a1b2c3"
PUT /articles/42
If-Match: "a1b2c3"          → 200 (updated) or 412 (stale)
```

## Error Envelope — RFC 9457 Problem Details

Standardize on **RFC 9457 (`application/problem+json`, obsoletes RFC 7807)**. Machine-parseable, human-readable, extensible.

```json
// 422 Unprocessable Content
{
  "type": "https://api.ex.com/problems/validation-error",
  "title": "Validation failed",
  "status": 422,
  "detail": "The request body has 2 invalid fields.",
  "instance": "/articles",
  "requestId": "req_01HXQZ...",
  "errors": [
    { "field": "title",  "code": "required",  "message": "Title is required." },
    { "field": "author", "code": "not_found", "message": "Unknown author id." }
  ]
}
```

- `type` — URI identifying the error class (dereferenceable to docs ideally); the **stable machine key** clients switch on. Defaults to `"about:blank"` if omitted.
- `title` — short, human, stable per `type`.
- `status` — mirrors the HTTP status.
- `detail` — human explanation of *this* occurrence.
- `instance` — URI of the specific occurrence/request.
- **Extensions**: add `errors[]`, `requestId`, `retryAfter`, etc. as top-level members.
- Set `Content-Type: application/problem+json`.

**Error rules:**
- Clients should branch on `type`/`code`, **never** on `title`/`detail` strings (those are for humans and may change).
- Never leak stack traces, SQL, internal hostnames, or PII in error bodies.
- Return **all** validation errors at once (`errors[]`), not just the first — saves round-trips.
- Keep a documented, finite catalog of error `type`s/`code`s. Treat it as part of the contract.
- Match the HTTP status to the problem: don't send `200` with `{"error": ...}` — that breaks HTTP tooling, caches, and retries.

## Partial Updates (PATCH)

Two standard formats — **pick one and document it**:

### JSON Merge Patch (RFC 7396) — simplest, most common
`Content-Type: application/merge-patch+json`. Send only changed fields; **`null` deletes/clears a field**; omitted fields are untouched.
```
PATCH /users/42
Content-Type: application/merge-patch+json

{ "displayName": "Ada L.", "phone": null }   // sets name, clears phone
```
- Limitation: can't unambiguously set a field *to* JSON `null` (null always means delete), and can't patch inside arrays element-wise.

### JSON Patch (RFC 6902) — precise, ops-based
`Content-Type: application/json-patch+json`. An array of operations; supports array indexing and tests.
```
PATCH /users/42
Content-Type: application/json-patch+json

[
  { "op": "replace", "path": "/displayName", "value": "Ada L." },
  { "op": "remove",  "path": "/phone" },
  { "op": "add",     "path": "/roles/-", "value": "admin" }
]
```
- More powerful (test/move/copy, array ops) but verbose. Use when clients need fine-grained edits.

Reject unknown fields with `422` (fail-closed) rather than silently ignoring them.

## Bulk & Batch Operations

- **Batch create/update**: `POST /articles/batch` (or `POST /articles` accepting an array). Decide semantics:
  - **All-or-nothing** (transactional) → `201`/`200` on success or `422` on any failure.
  - **Partial success** → return `207 Multi-Status` with per-item results so clients know which succeeded:
```json
// POST /articles/batch  → 207
{ "results": [
  { "status": 201, "id": "art_1" },
  { "status": 422, "error": { "type": "...", "detail": "title required" } }
] }
```
- Cap batch size; document it and the failure semantics explicitly.
- For huge datasets prefer async jobs (below) over synchronous batch.

## Long-Running / Async Operations

For work that can't finish within a request budget (video transcode, export, bulk import):

1. `POST /exports` → **`202 Accepted`**, `Location: /exports/{jobId}`, body with `{ "id", "status": "queued" }`.
2. Client polls `GET /exports/{jobId}` → `{ "status": "processing" | "succeeded" | "failed", ... }`.
3. On completion, response includes the result or a `resultUrl` (e.g. signed download link).
- Include a `Retry-After` hint on the job resource to pace polling; or push completion via **webhooks**.
- Model the job as a real resource (listable, cancelable via `DELETE`/`POST .../actions/cancel`).

## Webhooks (server → client callbacks)

- POST an event envelope to a client-registered URL. Include `id`, `type`, `createdAt`, `data`.
- **Sign payloads** (HMAC-SHA256 over the raw body, in a header like `X-Signature`) so receivers can verify authenticity; include a timestamp to prevent replay. (For a standardized approach, see the Webhooks-derived `Webhook-Signature`/`Webhook-Id`/`Webhook-Timestamp` header convention.)
- **Deliver at-least-once** → events must carry a stable `id` so consumers dedupe (idempotent handlers). Retry with backoff on non-2xx; expect a fast `2xx` ack.

## Caching & Performance

- **Cacheable GETs**: set `Cache-Control` (`public`/`private`, `max-age`, `stale-while-revalidate`) and `ETag`/`Last-Modified`.
- Use `Cache-Control: no-store` for sensitive/per-user data that must never be cached.
- **`Vary`** on headers that change the response (`Vary: Accept, Authorization, Accept-Encoding`) so caches don't serve the wrong variant.
- Enable **compression** (`Content-Encoding: gzip`/`br`/`zstd`); huge win on mobile.
- Use **sparse fieldsets** and **expansion** (see *Filtering*) to cut payload size and round-trips.
- Return `304` on conditional GETs to save bandwidth.

## Rate Limiting

- Advertise limits with the IETF `RateLimit` / `RateLimit-Policy` structured header fields (`draft-ietf-httpapi-ratelimit-headers`, still an Internet-Draft but widely adopted) so clients can self-throttle:
```
RateLimit-Policy: "default";q=1000;w=60
RateLimit: "default";r=12;t=30
```
  - `RateLimit-Policy` describes the quota: `q` = quota units, `w` = window seconds.
  - `RateLimit` reports the live state: `r` = remaining quota, `t` = seconds until the window resets.
  - The **legacy `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`** triplet is still the most widespread form in the wild — emit it too (or instead) for compatibility, and document whichever you use.
- On limit exceeded → **`429 Too Many Requests`** with **`Retry-After`** (seconds or HTTP-date).
- Rate-limit per API key / user / IP as appropriate; document limits and burst behavior.

## Authentication & Security

- **HTTPS only.** Redirect/reject plain HTTP; enable HSTS.
- **Bearer tokens**: `Authorization: Bearer <token>`. Use **OAuth 2.1 / OIDC** for user auth; short-lived access tokens + refresh tokens. For native mobile apps use **Authorization Code + PKCE** (never embed client secrets in the app).
- **API keys** for server-to-server; scope and rotate them.
- Never put secrets/tokens in **URLs** (they leak into logs, history, referrers). Use headers.
- **Validate and whitelist all input**; enforce max body size; set timeouts.
- **CORS**: return explicit `Access-Control-Allow-Origin` (never `*` for credentialed requests), handle `OPTIONS` preflight, and restrict allowed methods/headers.
- Send security headers (`X-Content-Type-Options: nosniff`, etc.) and strip server version banners.
- **Authorize every request at the object level** — this prevents IDOR / broken object-level authorization, consistently the top API risk. Verify the caller may act on *this specific* resource, not just that they're authenticated. To avoid leaking that a private resource exists, return `404` (not `403`) when the caller has no right to know about it.
- **Pagination and rate limits are security controls** too — they bound data exfiltration and abuse.

## API Contracts & Documentation

- **Contract-first with OpenAPI 3.2** (Sept 2025; still JSON Schema 2020-12, adds native `QUERY`, streaming media types, and OAuth 2.0 Device Flow — 3.1 remains fine if your tooling lags). The spec is the source of truth for servers, clients (codegen), mocks, and tests.
- Version the spec alongside the API; run **contract tests** in CI so implementation can't drift from spec.
- Document for every endpoint: verb, path, params (with constraints), request/response schemas, all status codes, error `type`s, auth scopes, rate limits, idempotency support, and pagination/sort/filter/expand fields.
- Provide **examples** for requests and responses (including error responses) — the single highest-leverage doc improvement for client devs.
- Publish a **changelog** and deprecation timeline. Never make a breaking change to a published version.
- Ship a **health endpoint** (`GET /health` / `GET /healthz`) returning `200` when serving; keep it unauthenticated and cheap.

## Quick Decision Checklist

- Verb wrong? → nouns in URLs, actions via verbs; use `/actions/x` or a resource for non-CRUD.
- Which 4xx? → syntax `400`; unauth `401`; forbidden `403`; missing/hidden `404`; validation `422`; state/duplicate `409`; rate `429`.
- Created something? → `201` + `Location` + body.
- Deep/large list? → cursor pagination, opaque cursor, stable sort key, capped `limit`.
- Big/structured query? → `QUERY` (or `POST /search`), never `GET` with a body.
- Retryable POST? → `Idempotency-Key`, stored atomically, replay original response.
- Concurrent edits? → `ETag` + `If-Match` → `412` on stale.
- Partial edit? → `PATCH` with merge-patch (default) or json-patch (precise).
- Error body? → RFC 9457 `problem+json`, machine `type`, `errors[]`, `requestId`, no leaks.
- Breaking change? → new major version; `Deprecation` (RFC 9745, date value) + `Sunset` on the old one.
- Every response documented in OpenAPI, examples included, contract-tested in CI.

KEYWORDS: rest api design, http status codes, resource modeling, url conventions, http verbs, http query method, pagination, cursor pagination, filtering, sorting, api versioning, idempotency key, idempotent requests, error envelope, rfc 9457 problem details, request response shape, api contract, openapi 3.2, conditional requests etag, content negotiation, rate limiting headers, partial update patch, json merge patch, bulk operations, field selection sparse fieldsets, http caching, api authentication, uuidv7, collection resource, sub-resource, long-running async jobs, webhooks, optimistic concurrency
