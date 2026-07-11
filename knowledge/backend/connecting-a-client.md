<!-- keywords: client-server communication, URLSession, async await, Codable, Keychain, kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly, bearer token, Authorization header, token refresh, refresh token rotation, 401 unauthorized, 403 forbidden, actor AuthManager, refresh coalescing, Sendable, fetch, TypeScript client, AbortSignal.timeout, AbortSignal.any, TimeoutError, AbortController, retry, exponential backoff, full jitter, Retry-After, 429 too many requests, idempotency key, offline queue, HTTP caching, ETag, If-None-Match, 304 Not Modified, Cache-Control, stale-while-revalidate, Service Worker, IndexedDB, TanStack Query, OpenAPI 3.1, swift-openapi-generator, openapi-typescript, openapi-fetch, contract-first, code generation, error envelope, problem+json, RFC 9457, validation errors, API versioning, Deprecation Sunset headers, CORS, CSRF, HttpOnly cookie, certificate pinning, X-Request-Id, request tracing, snake_case decoding, appending(path:), RateLimit-Policy -->

# Connecting a Client to Your Backend (iOS + Web)

A reference for how mobile (iOS `URLSession` + `Codable`, token in Keychain) and web (`fetch`) clients talk to a backend, and what the **backend** must guarantee to make that talk correct: auth, error contracts, retries, offline/caching, and keeping the client/server schema in sync. Design the server so a well-behaved client is *easy* to write — every rule below has a client half and a server half.

---

## Core principles (read first)

- **The contract is the API, not the code.** Ship a machine-readable schema (OpenAPI 3.1). Both clients generate types from it. A field rename is a breaking change; treat it like one.
- **Errors are part of the contract.** A 4xx/5xx with a stable, typed JSON body is a feature. HTML error pages and bare 500s force clients to guess.
- **Make writes idempotent.** Networks retry. If the same `POST` can run twice and charge a card twice, the bug is on the server.
- **Version at the edge, evolve additively.** Prefer a `/v1` path plus *additive-only* changes within a version. The load-bearing rule that makes this safe: **clients ignore unknown response fields, servers tolerate unknown request fields.** Adding a field then never breaks anyone.
- **Auth belongs in headers, never in the URL.** URLs land in logs, proxies, referrers, and browser history.
- **Every response is JSON, including errors.** Send `Content-Type: application/json; charset=utf-8` (or `application/problem+json` for errors). Never return HTML to an API client.

---

## The request/response contract

### Baseline HTTP conventions the server must honor

- Methods carry meaning: `GET` (safe, cacheable, no body), `POST` (create / non-idempotent action), `PUT` (full idempotent replace), `PATCH` (partial update), `DELETE` (idempotent).
- Status codes clients branch on:
  - `200/201/204` success (`204` = success, no body — client must not parse a body).
  - `400` malformed, `401` unauthenticated (missing/expired/invalid token), `403` authenticated-but-forbidden, `404` not found, `409` conflict, `422` semantic validation failure.
  - `408/425/429` retryable client-ish, `500/502/503/504` retryable server-side.
- **`401` vs `403` is load-bearing.** Clients trigger token refresh on `401` and must **not** retry `403`. Do not conflate them.
- Send `Content-Type: application/json` on every request with a body; send `Accept: application/json` always.

### Standard error envelope (RFC 9457 `application/problem+json`)

Pick one error shape and use it for *every* error. RFC 9457 (Problem Details, which obsoletes RFC 7807) is the 2026 default:

```json
{
  "type": "https://api.example.com/errors/invalid-params",
  "title": "Your request parameters didn't validate.",
  "status": 422,
  "detail": "email must be a valid address",
  "instance": "/v1/users",
  "code": "VALIDATION_ERROR",
  "errors": [
    { "field": "email", "code": "INVALID_FORMAT", "message": "must be a valid address" }
  ],
  "requestId": "01JC6Z7Q9K8ABCDEF"
}
```

Rules for the server:
- Set `Content-Type: application/problem+json`.
- `code` is a **stable machine string** (never localize it, never change it). `title`/`detail` are human-readable and may change.
- Include a `requestId` (echo the inbound `X-Request-Id` or generate one) so client logs correlate with server logs.
- Field-level errors go in an `errors` array with a stable per-field `code`, so the client attaches messages to form fields without string-matching human text.

---

## Authentication

### Token model

- Use short-lived **access tokens** (JWT or opaque, ~5–15 min) sent as `Authorization: Bearer <token>`, plus a long-lived **refresh token** used only against the refresh endpoint.
- Access token in memory (or Keychain on iOS); refresh token in the **most secure store available** (iOS Keychain; web: an `HttpOnly`, `Secure`, `SameSite` cookie — *not* `localStorage`).
- The server exposes `POST /v1/auth/refresh` that takes a refresh token and returns a new access token. **Rotate refresh tokens** and detect reuse: a replayed old refresh token ⇒ revoke the whole chain.

### Web: where does the token live?

- **Best for browsers: refresh token in an `HttpOnly` cookie.** JS cannot read it, which kills the main XSS token-theft vector. Requires CSRF defense (below).
- Keep the **access token in memory only** (a module-scoped variable). Never persist a bearer token in `localStorage`/`sessionStorage` — it's readable by any injected script.
- **CORS:** when the request sends credentials (cookies), the server must set `Access-Control-Allow-Origin` to the *exact* origin (never `*`), plus `Access-Control-Allow-Credentials: true`, and enumerate allowed methods/headers. The preflight (`OPTIONS`) must answer with these headers.
- **CSRF:** for cookie auth, require a double-submit token or `SameSite=Strict/Lax` plus an `Origin`/`Sec-Fetch-Site` check on state-changing requests.

### iOS: token in Keychain

- Store tokens in Keychain with `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` (survives backgrounding, never leaves the device, excluded from iCloud backups). Use `WhenUnlockedThisDeviceOnly` if the token must never be readable while the device is locked.
- Never put tokens in `UserDefaults` — it's an unencrypted plist.
- Do the refresh dance in a single actor so N concurrent 401s trigger **one** refresh, not N.

---

## iOS reference implementation (Swift 6, `async/await`)

Compiles against iOS 16+ with Swift 6 strict concurrency. Structured so every request goes through one client, one auth actor, one retry policy.

### Keychain wrapper

```swift
import Foundation
import Security

struct KeychainStore {
    let service: String

    func set(_ data: Data, account: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            let insert = query.merging(attributes) { _, new in new }
            let addStatus = SecItemAdd(insert as CFDictionary, nil)
            guard addStatus == errSecSuccess else { throw KeychainError.status(addStatus) }
        } else {
            guard status == errSecSuccess else { throw KeychainError.status(status) }
        }
    }

    func get(account: String) throws -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var out: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &out)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw KeychainError.status(status) }
        return out as? Data
    }

    func delete(account: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainError.status(status)
        }
    }
}

enum KeychainError: Error { case status(OSStatus) }
```

### Typed errors + decoded problem body

```swift
struct APIProblem: Decodable, Sendable {
    let type: String?
    let title: String?
    let status: Int?
    let detail: String?
    let code: String?
    let requestId: String?
}

enum APIError: Error, Sendable {
    case transport(URLError)                       // connectivity/timeout after retries
    case unauthorized                              // 401 after refresh failed
    case http(status: Int, problem: APIProblem?)   // non-2xx with parsed body
    case decoding(Error)
    case offline
}
```

### AuthManager: one refresh at a time (actor + cached `Task`)

The key trick: cache the in-flight refresh `Task`. Concurrent callers `await` the same task, so only one network refresh happens. A `Task {}` created inside an actor method inherits the actor's isolation, so mutating `refreshTask` from the `defer` is safe.

```swift
struct TokenPair: Codable, Sendable {
    var accessToken: String
    var refreshToken: String
}

actor AuthManager {
    private let keychain: KeychainStore
    private let refreshEndpoint: URL
    private let session: URLSession
    private var cached: TokenPair?
    private var refreshTask: Task<TokenPair, Error>?

    init(keychain: KeychainStore, refreshEndpoint: URL, session: URLSession = .shared) {
        self.keychain = keychain
        self.refreshEndpoint = refreshEndpoint
        self.session = session
    }

    /// Returns a currently-valid access token, refreshing if needed.
    func validToken() async throws -> String {
        if let task = refreshTask { return try await task.value.accessToken }
        if let pair = try loadCached() { return pair.accessToken }
        return try await refresh().accessToken
    }

    /// Force a refresh (call this after a 401). Coalesces concurrent callers.
    @discardableResult
    func refresh() async throws -> TokenPair {
        if let task = refreshTask { return try await task.value }
        let task = Task { () throws -> TokenPair in
            defer { refreshTask = nil }
            guard let refreshToken = try loadCached()?.refreshToken else {
                throw APIError.unauthorized
            }

            var req = URLRequest(url: refreshEndpoint)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONEncoder().encode(["refreshToken": refreshToken])

            let (data, resp) = try await session.data(for: req)
            guard let http = resp as? HTTPURLResponse, http.statusCode == 200 else {
                throw APIError.unauthorized
            }
            let pair = try JSONDecoder().decode(TokenPair.self, from: data)
            try persist(pair)
            return pair
        }
        refreshTask = task
        return try await task.value
    }

    /// In-memory clear only; never throws. Keychain delete tolerates a missing item.
    func clear() {
        cached = nil
        refreshTask?.cancel()
        refreshTask = nil
        try? keychain.delete(account: "tokens")
    }

    private func loadCached() throws -> TokenPair? {
        if let cached { return cached }
        guard let data = try keychain.get(account: "tokens") else { return nil }
        let pair = try JSONDecoder().decode(TokenPair.self, from: data)
        cached = pair
        return pair
    }

    private func persist(_ pair: TokenPair) throws {
        cached = pair
        try keychain.set(try JSONEncoder().encode(pair), account: "tokens")
    }
}
```

### APIClient: auth injection, decode, retry, one 401-refresh-retry

```swift
struct RetryPolicy: Sendable {
    var maxAttempts = 3
    var baseDelay: Double = 0.5   // seconds
    var maxDelay: Double = 8.0
}

/// Sentinel so `send(...)` can default `body` to "no body" while staying generic.
/// (`Never` isn't `Encodable`, so we can't use `Optional<Never>`.)
struct EmptyBody: Encodable {}

private let retryableStatuses: Set<Int> = [408, 425, 429, 500, 502, 503, 504]

// @unchecked because JSONDecoder is a non-Sendable class; here it's immutable
// after init and used only for reads, so concurrent use is safe.
final class APIClient: @unchecked Sendable {
    private let baseURL: URL
    private let session: URLSession
    private let auth: AuthManager
    private let policy: RetryPolicy
    private let decoder: JSONDecoder

    init(baseURL: URL, auth: AuthManager, policy: RetryPolicy = .init()) {
        self.baseURL = baseURL
        self.auth = auth
        self.policy = policy
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 15
        config.waitsForConnectivity = false        // fail fast; we handle offline explicitly
        config.httpAdditionalHeaders = ["Accept": "application/json"]
        self.session = URLSession(configuration: config)
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        d.keyDecodingStrategy = .convertFromSnakeCase
        self.decoder = d
    }

    func send<T: Decodable>(
        _ path: String,
        method: String = "GET",
        body: (some Encodable)? = EmptyBody?.none,
        idempotencyKey: String? = nil,
        as type: T.Type = T.self
    ) async throws -> T {
        let data = try await sendRaw(path, method: method, body: body, idempotencyKey: idempotencyKey)
        do { return try decoder.decode(T.self, from: data) }
        catch { throw APIError.decoding(error) }
    }

    /// For 204 / empty-body endpoints, call this instead of `send`.
    func sendVoid(
        _ path: String,
        method: String = "POST",
        body: (some Encodable)? = EmptyBody?.none,
        idempotencyKey: String? = nil
    ) async throws {
        _ = try await sendRaw(path, method: method, body: body, idempotencyKey: idempotencyKey)
    }

    private func sendRaw(
        _ path: String, method: String,
        body: (some Encodable)?, idempotencyKey: String?
    ) async throws -> Data {
        var attempt = 0
        var didRefresh = false

        while true {
            attempt += 1
            let token = try await auth.validToken()
            let request = try makeRequest(path, method, body, token, idempotencyKey)

            do {
                let (data, response) = try await session.data(for: request)
                guard let http = response as? HTTPURLResponse else {
                    throw APIError.transport(URLError(.badServerResponse))
                }

                switch http.statusCode {
                case 200...299:
                    return data

                case 401 where !didRefresh:
                    didRefresh = true
                    _ = try await auth.refresh()      // coalesced; one retry only
                    continue

                case 401:
                    await auth.clear()
                    throw APIError.unauthorized

                // NOTE: the guard must cover every retryable status — a bare
                // `case 429, 500, ... where attempt < max` binds `where` only to the
                // LAST label, so 429/500 would loop forever at max attempts.
                case let s where retryableStatuses.contains(s) && attempt < policy.maxAttempts:
                    try await sleepForBackoff(attempt: attempt, response: http)
                    continue

                default:
                    let problem = try? decoder.decode(APIProblem.self, from: data)
                    throw APIError.http(status: http.statusCode, problem: problem)
                }
            } catch let urlError as URLError {
                if urlError.code == .cancelled { throw urlError }   // honor Task cancellation
                if urlError.code == .notConnectedToInternet { throw APIError.offline }
                if attempt < policy.maxAttempts, Self.isRetryable(urlError) {
                    try await sleepForBackoff(attempt: attempt, response: nil)
                    continue
                }
                throw APIError.transport(urlError)
            }
        }
    }

    private func makeRequest(
        _ path: String, _ method: String,
        _ body: (some Encodable)?, _ token: String, _ idempotencyKey: String?
    ) throws -> URLRequest {
        // `appending(path:)` (iOS 16+) preserves multi-segment paths like "/v1/users/me";
        // the old `appendingPathComponent` would percent-encode the slashes.
        var req = URLRequest(url: baseURL.appending(path: path))
        req.httpMethod = method
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let idempotencyKey { req.setValue(idempotencyKey, forHTTPHeaderField: "Idempotency-Key") }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            req.httpBody = try encoder.encode(body)
        }
        return req
    }

    private static func isRetryable(_ e: URLError) -> Bool {
        switch e.code {
        case .timedOut, .networkConnectionLost, .cannotConnectToHost, .dnsLookupFailed:
            return true
        default:
            return false
        }
    }

    /// Honor Retry-After if present; otherwise exponential backoff with full jitter.
    private func sleepForBackoff(attempt: Int, response: HTTPURLResponse?) async throws {
        if let header = response?.value(forHTTPHeaderField: "Retry-After"),
           let seconds = Self.parseRetryAfter(header) {
            try await Task.sleep(for: .seconds(min(seconds, policy.maxDelay)))
            return
        }
        let expo = min(policy.maxDelay, policy.baseDelay * pow(2, Double(attempt - 1)))
        try await Task.sleep(for: .seconds(Double.random(in: 0...expo)))   // full jitter
    }

    /// Retry-After is either delta-seconds or an HTTP-date (RFC 9110).
    private static func parseRetryAfter(_ value: String) -> Double? {
        if let seconds = Double(value) { return seconds }
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.timeZone = TimeZone(identifier: "GMT")
        fmt.dateFormat = "EEE, dd MMM yyyy HH:mm:ss zzz"
        guard let date = fmt.date(from: value) else { return nil }
        return max(0, date.timeIntervalSinceNow)
    }
}
```

Usage:

```swift
struct User: Decodable { let id: String; let displayName: String }
struct CreateUser: Encodable { let email: String; let displayName: String }

let me: User = try await client.send("/v1/users/me")

let created: User = try await client.send(
    "/v1/users",
    method: "POST",
    body: CreateUser(email: "a@b.com", displayName: "Ada"),
    idempotencyKey: UUID().uuidString
)
```

### iOS notes

- A cancelled surrounding `Task` surfaces as `URLError.cancelled` — propagate it, never retry it (handled explicitly above).
- Prefer **structured concurrency** (`async let`, `TaskGroup`) for fan-out; don't spin up detached tasks per request.
- `.convertFromSnakeCase` maps `snake_case` JSON automatically; use explicit `CodingKeys` when names don't map cleanly.
- Set `config.waitsForConnectivity = true` **only** for user-initiated, cancellable, long-lived uploads — otherwise it hides offline state and blocks your own retry logic.
- **Decode leniently:** `Codable` ignores unknown keys by default — keep it that way (don't add a custom `init(from:)` that rejects extras), so a new server field never fails an old client.

---

## Web reference implementation (TypeScript `fetch`)

Runs in the browser and in modern Node/Deno/Bun (all ship `fetch`, `AbortController`, `AbortSignal.timeout`, `AbortSignal.any`, `crypto.randomUUID`).

### Typed errors + problem parsing

```ts
export interface ApiProblem {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  code?: string;
  requestId?: string;
  errors?: { field: string; code: string; message: string }[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly problem: ApiProblem | null,
    readonly requestId?: string,
  ) {
    super(problem?.title ?? `HTTP ${status}`);
    this.name = "ApiError";
  }
}

export class OfflineError extends Error {
  constructor() {
    super("offline");
    this.name = "OfflineError";
  }
}
```

### Auth: in-memory access token, refresh coalescing, cookie refresh

```ts
type Tokens = { accessToken: string };

class AuthManager {
  private accessToken: string | null = null;
  private refreshInFlight: Promise<Tokens> | null = null;

  constructor(private readonly refreshUrl: string) {}

  async validToken(): Promise<string> {
    if (this.accessToken) return this.accessToken;
    return (await this.refresh()).accessToken;
  }

  // Coalesce concurrent refreshes into one network call.
  refresh(): Promise<Tokens> {
    if (this.refreshInFlight) return this.refreshInFlight;
    this.refreshInFlight = (async () => {
      try {
        // Refresh token travels in an HttpOnly cookie -> credentials: "include".
        const res = await fetch(this.refreshUrl, {
          method: "POST",
          credentials: "include",
          headers: { Accept: "application/json" },
        });
        if (!res.ok) throw new ApiError(res.status, null);
        const tokens = (await res.json()) as Tokens;
        this.accessToken = tokens.accessToken;
        return tokens;
      } finally {
        this.refreshInFlight = null;
      }
    })();
    return this.refreshInFlight;
  }

  clear() {
    this.accessToken = null;
  }
}
```

### Client: timeout, retries, one 401 refresh-and-retry

```ts
export interface RetryPolicy {
  maxAttempts: number; // total attempts including the first
  baseDelayMs: number;
  maxDelayMs: number;
}

const DEFAULT_POLICY: RetryPolicy = { maxAttempts: 3, baseDelayMs: 500, maxDelayMs: 8000 };
const RETRYABLE_STATUS = new Set([408, 425, 429, 500, 502, 503, 504]);

export interface RequestOptions {
  method?: string;
  body?: unknown;
  idempotencyKey?: string;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export class ApiClient {
  constructor(
    private readonly baseUrl: string,
    private readonly auth: AuthManager,
    private readonly policy: RetryPolicy = DEFAULT_POLICY,
  ) {}

  async send<T>(path: string, opts: RequestOptions = {}): Promise<T> {
    const method = opts.method ?? "GET";
    let attempt = 0;
    let didRefresh = false;

    while (true) {
      attempt++;
      const token = await this.auth.validToken();
      const res = await this.dispatch(path, method, token, opts);

      if (res.ok) {
        if (res.status === 204) return undefined as T;
        return (await res.json()) as T;
      }

      if (res.status === 401 && !didRefresh) {
        didRefresh = true;
        try {
          await this.auth.refresh();
          continue;
        } catch {
          this.auth.clear();
          throw new ApiError(401, null);
        }
      }

      if (RETRYABLE_STATUS.has(res.status) && attempt < this.policy.maxAttempts) {
        await this.backoff(attempt, res.headers.get("Retry-After"), opts.signal);
        continue;
      }

      const problem = await this.parseProblem(res);
      throw new ApiError(res.status, problem, res.headers.get("X-Request-Id") ?? undefined);
    }
  }

  private async dispatch(
    path: string,
    method: string,
    token: string,
    opts: RequestOptions,
  ): Promise<Response> {
    const headers: Record<string, string> = {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
    };
    if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
    let body: string | undefined;
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }

    // Merge caller signal with a per-request timeout signal.
    const timeout = AbortSignal.timeout(opts.timeoutMs ?? 15_000);
    const signal = opts.signal ? AbortSignal.any([opts.signal, timeout]) : timeout;

    try {
      // NOTE: a leading-slash path replaces any path prefix on baseUrl. If your
      // base is "https://host/api", pass paths WITHOUT a leading slash and end
      // the base with "/". With a bare-host base, "/v1/..." is fine.
      return await fetch(new URL(path, this.baseUrl), {
        method,
        headers,
        body,
        credentials: "include",
        signal,
      });
    } catch (err) {
      // A caller cancel aborts with AbortError; a timeout aborts with TimeoutError.
      // Neither is "offline" — re-throw so callers can distinguish them.
      if (err instanceof DOMException && (err.name === "AbortError" || err.name === "TimeoutError")) {
        throw err;
      }
      // Everything else from fetch is a TypeError = genuine network failure.
      throw new OfflineError();
    }
  }

  private async parseProblem(res: Response): Promise<ApiProblem | null> {
    const ct = res.headers.get("Content-Type") ?? "";
    if (!ct.includes("json")) return null;
    try {
      return (await res.json()) as ApiProblem;
    } catch {
      return null;
    }
  }

  private async backoff(attempt: number, retryAfter: string | null, signal?: AbortSignal) {
    if (signal?.aborted) throw signal.reason;
    const delay = this.computeDelay(attempt, retryAfter);
    await new Promise<void>((resolve, reject) => {
      const id = setTimeout(resolve, delay);
      signal?.addEventListener(
        "abort",
        () => {
          clearTimeout(id);
          reject(signal.reason); // propagate the real reason (Abort/TimeoutError)
        },
        { once: true },
      );
    });
  }

  // Retry-After is either delta-seconds or an HTTP-date (RFC 9110).
  private computeDelay(attempt: number, retryAfter: string | null): number {
    if (retryAfter) {
      const secs = Number(retryAfter);
      if (Number.isFinite(secs)) return Math.min(secs * 1000, this.policy.maxDelayMs);
      const at = Date.parse(retryAfter);
      if (!Number.isNaN(at)) return Math.min(Math.max(0, at - Date.now()), this.policy.maxDelayMs);
    }
    const expo = Math.min(this.policy.maxDelayMs, this.policy.baseDelayMs * 2 ** (attempt - 1));
    return Math.random() * expo; // full jitter
  }
}
```

Usage:

```ts
const api = new ApiClient("https://api.example.com", new AuthManager("https://api.example.com/v1/auth/refresh"));

const me = await api.send<{ id: string; displayName: string }>("/v1/users/me");

const created = await api.send<{ id: string }>("/v1/users", {
  method: "POST",
  body: { email: "a@b.com", displayName: "Ada" },
  idempotencyKey: crypto.randomUUID(),
});
```

### Web notes

- `fetch` **rejects only on network failure**, not on HTTP error status. Always check `res.ok` — a `500` is a resolved promise.
- `AbortSignal.timeout(ms)` is the idiomatic per-request timeout; it aborts with a **`TimeoutError`** (not `AbortError`) — the reason your catch block must treat that separately from a caller cancel. `AbortSignal.any([...])` merges the caller's cancel signal with the timeout.
- `credentials: "include"` is required to send the `HttpOnly` refresh cookie cross-origin (and the server must send matching CORS headers).
- Keep the access token in a module-scoped variable, never `localStorage`; it's naturally cleared on reload, and refresh re-mints it.
- In React, wrap this client in a data layer (TanStack Query / SWR) — get dedupe, caching, retries, and `staleTime` for free instead of hand-rolling them per component.

---

## Retries — the rules both clients share

- **Only retry idempotent or idempotency-keyed requests.** `GET/PUT/DELETE` are safe. Retry a `POST` **only** if it carries an `Idempotency-Key` the server honors.
- **Retryable:** `408, 425, 429, 500, 502, 503, 504`, and transport errors (timeout, connection reset, DNS). **Never retry:** `400`, `401` (refresh instead), `403, 404, 409, 422`.
- **Backoff = exponential + jitter.** `delay = random(0, min(cap, base * 2^attempt))` (full jitter). Jitter is not optional — without it, all clients retry in lockstep and create a thundering herd that keeps the server down.
- **Honor `Retry-After`** (delta-seconds *or* HTTP-date) on `429`/`503` — it overrides your computed backoff.
- **Cap attempts (3–5) and cap total wall-clock** against a deadline tied to the UX. A user staring at a spinner should not wait 30s of backoff.
- **Refresh-on-401 is a separate one-shot**, not part of the retry counter: refresh once, replay once, then surface the error.
- **Server side:** return `429` with `Retry-After` when throttling; never return `500` for expected conditions (validation, auth) — reserve `5xx` for genuine server faults so client retry logic behaves.

### Idempotency keys (server contract)

- Client sends `Idempotency-Key: <uuid>` on unsafe requests it may retry, and **reuses the same key across retries of one logical operation**.
- Server stores `(key → response)` for a TTL (e.g. 24h). A repeated key returns the **stored response** without re-executing the side effect.
- Same key + **different** request body ⇒ `409 Conflict` (the client is misusing the key).
- Scope keys per user + per endpoint to avoid collisions.

---

## Offline & caching

### HTTP caching (the server drives it)

- `GET` responses should carry validators: **`ETag`** (content hash) and/or `Last-Modified`. Clients send them back as `If-None-Match` / `If-Modified-Since`; the server answers `304 Not Modified` with no body when unchanged. This saves bandwidth and battery.
- `Cache-Control` directives: `no-store` (never cache — auth/PII), `no-cache` (cache but revalidate every time), `private` (browser only, not shared proxies), `max-age=N`, and `stale-while-revalidate=N` (serve stale instantly, refresh in the background).
- **Never cache authenticated responses in a shared cache.** Use `Cache-Control: private` (or `no-store`) for anything user-specific.

### iOS caching

- `URLSession` honors `ETag`/`Cache-Control` automatically via `URLCache` under `.useProtocolCachePolicy` (the default). A well-behaved server gets you HTTP caching for free.
- For an **offline read model**, persist decoded models in SwiftData / Core Data / SQLite (GRDB). Pattern: read from the local store immediately (instant UI), then revalidate from network and reconcile. The network layer feeds the store; the UI reads the store.
- Conditional-request example: keep the last `ETag` per resource, send `req.setValue(etag, forHTTPHeaderField: "If-None-Match")`, and on `304` load from the local store.

### Web caching / offline

- The browser HTTP cache handles `ETag`/`Cache-Control` transparently for `fetch`.
- For app-level offline, use a **Service Worker** (Cache Storage API) with a stale-while-revalidate strategy for `GET`s, plus **IndexedDB** for structured offline data and a mutation queue.
- Data libraries (TanStack Query) give you an in-memory + optionally persisted cache with `staleTime`/`gcTime`, background refetch, and offline-aware retries — prefer them over ad-hoc caching.

### Offline write queue (both platforms)

- Queue unsafe mutations locally when offline; each carries a pre-generated `Idempotency-Key`. Flush on reconnect (iOS: `NWPathMonitor`; web: `online` event / Background Sync). Because the server is idempotent, replays are safe even if the queue double-fires.

---

## Keeping the client/server contract in sync

### Contract-first with OpenAPI 3.1 (recommended)

- The OpenAPI document is the single source of truth. Generate client types from it — no hand-written models drifting from reality.
- **iOS:** `apple/swift-openapi-generator` (1.x, stable) generates `Sendable` `Codable` types + a typed client via an SPM build plugin. Types regenerate on every build, so a schema change that breaks the client fails at **compile time**, not runtime.
- **TypeScript:** `openapi-typescript` (emits pure types) paired with `openapi-fetch` (tiny typed `fetch` wrapper), or `orval` / `@hey-api/openapi-ts` if you want generated hooks. These give end-to-end type safety from spec to call site.
- Wire generation into CI: fail the build if the committed generated code differs from a fresh generation against the current spec.

### If you don't do full codegen

- At minimum, generate **types** (Swift `Codable` structs / TS interfaces) from the schema and share the enum of error `code` values.
- Add a **contract test** in CI: hit a running server (or a Prism/mock derived from the spec) and assert responses validate against the OpenAPI schema. This catches server drift before clients do.

### Versioning & evolution rules

- **Additive is safe:** new optional fields, new endpoints, new enum values *if clients handle unknowns*. Keep clients tolerant — unknown response fields ignored; unknown enum values mapped to a `default`/`unknown` case (in Swift, decode enums via a raw-value fallback; in TS, treat the union as open).
- **Breaking changes** (removing/renaming a field, changing a type, tightening validation) require a **new version** (`/v2`) and a deprecation window. Announce with `Deprecation` and `Sunset` response headers.
- Because mobile clients **can't be force-updated**, old app versions call your API for months. Never break `/v1` while old binaries are live. Track the minimum supported client version and return `426 Upgrade Required` (with a problem body) when you must cut one off.
- Send a client version header (`X-Client-Version`) so the server can log and, if needed, branch behavior or warn.

---

## Observability & security checklist

- **Correlate requests:** client generates `X-Request-Id` (or `traceparent` for W3C Trace Context); server echoes it and logs it in every error. This turns "it failed" into a single grep.
- **Never log tokens, `Authorization` headers, or PII.** Redact at the logging boundary on both sides.
- **TLS only.** iOS App Transport Security enforces HTTPS by default — don't disable it. For high-value apps, consider **certificate/public-key pinning** (`URLSessionDelegate.urlSession(_:didReceive:completionHandler:)` on iOS) but pair it with a backup pin and a kill-switch to avoid bricking clients on rotation.
- **Validate on the server, always.** Client-side validation is UX; the server is the security boundary. Re-check auth, ownership, and input shape on every request.
- **Don't trust device clocks.** Access-token expiry checks depend on clocks that drift. Treat `401` as the real signal — don't rely on the client's local expiry judgment alone.
- **Rate-limit per user and per IP**, and advertise limits via the IETF `RateLimit`/`RateLimit-Policy` headers (`draft-ietf-httpapi-ratelimit-headers`) plus `Retry-After` on `429`, so well-behaved clients self-throttle.

---

## Quick decision table

| Situation | Client does | Server provides |
|---|---|---|
| Access token expired | Refresh once on `401`, replay request | `POST /auth/refresh`, rotating refresh tokens |
| Forbidden action | Show error, do **not** retry | `403` + problem body with `code` |
| Transient 5xx / timeout | Retry w/ exp backoff + jitter, capped | Idempotent handlers; `Retry-After` on throttle |
| Retrying a `POST` | Attach `Idempotency-Key`, reuse across retries | Dedup by key, return stored response |
| Slow / no network | Timeout, surface offline, queue writes | Fast failure modes; no long hangs |
| Unchanged `GET` | Send `If-None-Match` (ETag) | `ETag` + `304 Not Modified` |
| New backend field | Ignore unknown fields | Additive change within version |
| Removed/renamed field | Requires app update | New `/v2` + `Deprecation`/`Sunset` headers |
| Validation failure | Map field `code`s to form fields | `422` + `errors[]` with stable `code`s |
