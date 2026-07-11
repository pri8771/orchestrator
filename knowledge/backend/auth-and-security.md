<!-- keywords: jwt, sessions, oauth2, oidc, pkce, bcrypt, argon2, argon2id, password hashing, token storage, refresh token rotation, httponly cookie, samesite, cors, rate limiting, ipkeygenerator, input validation, zod, secrets management, owasp, csrf, ssrf, idor, bola, broken access control, authentication, authorization, api security, timing safe compare, constant time comparison, jwks, rs256, eddsa, helmet, csp, sql injection, nosql injection, rbac, multi-tenant isolation, user enumeration, content security policy -->

# Backend Auth & API Security — Build-Agent Cheatsheet (2026)

Dense, actionable rules for building an API server that authenticates users, authorizes requests, and doesn't get owned. Defaults assume Node 20+/TypeScript; Python notes where they differ. Snippets are verified against current libraries (argon2 0.44, jose 6, jsonwebtoken 9, zod 4, express-rate-limit 8) and are copy-paste runnable.

## Decision: sessions vs JWT (pick per surface, not per app)

- **First-party web app where you control the client → server-side sessions** (opaque session ID in an HttpOnly cookie). Safest default. Revocation is trivial (delete the row), no token-in-JS problem, no XSS token theft.
- **Mobile app, third-party API clients, service-to-service → short-lived JWT access tokens + refresh tokens.** Stateless verification scales; you accept weaker revocation.
- **Do NOT use JWTs as your session mechanism for a browser SPA just because it's trendy.** You gain statelessness you don't need and inherit a revocation problem you "solve" with a denylist — which *is* a session store, so you've reinvented sessions but worse.
- **Never store any token in `localStorage`/`sessionStorage`.** Readable by any XSS. Tokens belong in `HttpOnly; Secure; SameSite` cookies, or (mobile) the platform keychain/keystore.

| | Opaque session | JWT access token |
|---|---|---|
| Revocation | Instant (delete server-side) | Hard — needs short TTL + denylist |
| Verify cost | DB/Redis lookup | Signature check, no I/O |
| Payload readable by client | No | Yes (base64url, **not** encrypted) |
| Best for | First-party browser apps | Mobile, APIs, service mesh |

## Password hashing (the one you must not get wrong)

**Use Argon2id.** Fall back to bcrypt only on platforms without a vetted Argon2 binding. Never MD5, SHA-1, SHA-256, or any fast hash for passwords — those are for integrity, not secrets. Never invent your own scheme.

OWASP baseline parameters:
- **Argon2id:** memory 19 MiB (`19456` KiB), iterations `2`, parallelism `1`. Bump memory to 46–64 MiB on servers with headroom.
- **bcrypt:** cost factor `12` minimum (10 is the floor; 12–14 typical in 2026). bcrypt has a hard **72-byte** input limit.

```js
// Node — argon2 (npm i argon2). Salt + params are embedded in the output string.
import argon2 from 'argon2';

const OPTS = { type: argon2.argon2id, memoryCost: 19456, timeCost: 2, parallelism: 1 };

export const hashPassword = (pw) => argon2.hash(pw, OPTS);

export async function verifyPassword(hash, pw) {
  // argon2.verify is constant-time internally and returns false on mismatch (throws only on a malformed hash).
  if (!(await argon2.verify(hash, pw))) return { ok: false };
  // needsRehash is synchronous — transparently upgrade hashes when you raise params later.
  const newHash = argon2.needsRehash(hash, OPTS) ? await argon2.hash(pw, OPTS) : undefined;
  return { ok: true, newHash };
}
```

```python
# Python — argon2-cffi (pip install argon2-cffi). Same story: params baked into the hash.
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

ph = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)

def hash_password(pw: str) -> str:
    return ph.hash(pw)

def verify_password(stored_hash: str, pw: str) -> bool:
    try:
        ph.verify(stored_hash, pw)          # raises on mismatch
    except VerifyMismatchError:
        return False
    if ph.check_needs_rehash(stored_hash):  # re-store with ph.hash(pw) after raising params
        ...
    return True
```

### bcrypt's 72-byte trap (a real auth-bypass class)

bcrypt **silently truncates input at 72 bytes** and rejects strings containing a NUL byte. Two ways this becomes a vulnerability:

1. Long passwords: everything after byte 72 is ignored, so `"A"*72 + <anything>` all validate against the same hash.
2. **Pre-hashing done wrong** — the FreshRSS CVE-2025-68402 pattern: prepending a long constant (there, a 64-char SHA-256 hex nonce) before the password pushes the *password* past byte 72, truncating it away, so `password_verify()` returns true for **any** password.

Correct pre-hash (only needed if you must accept >72-byte passwords with bcrypt): SHA-256 the password, **base64-encode** it (44 chars, no NUL bytes, fits in 72), *then* bcrypt.

```js
import bcrypt from 'bcrypt';
import { createHash } from 'node:crypto';

const prehash = (pw) => createHash('sha256').update(pw, 'utf8').digest('base64'); // 44 chars, no NUL
export const hashPw = (pw) => bcrypt.hash(prehash(pw), 12);
export const checkPw = (pw, h) => bcrypt.compare(prehash(pw), h);
```

Prefer Argon2id and this whole footgun disappears — Argon2 has no length limit.

### Password rules that actually help

- Enforce a **minimum length (12+)**, a generous maximum (64–128, to bound hashing cost / DoS), and **allow all Unicode incl. spaces/emoji**. No composition rules ("1 uppercase + 1 symbol") — NIST dropped them; they push users toward `Password1!`.
- **Check candidates against a breached-password list** (k-anonymity range query against HaveIBeenPwned, or a local Bloom filter). Higher-signal than any complexity rule.
- **Never** enforce periodic rotation without a breach signal. Rate-limit and lock (with backoff) on repeated failures.

## Verifying secrets & tokens: constant-time only

Any `==`/`===` comparison of a secret, token, HMAC, or API key leaks length and content via timing. Use a constant-time compare.

```js
import { timingSafeEqual } from 'node:crypto';

export function safeEqual(a, b) {
  const ba = Buffer.from(a, 'utf8'), bb = Buffer.from(b, 'utf8');
  if (ba.length !== bb.length) return false; // timingSafeEqual THROWS on length mismatch
  return timingSafeEqual(ba, bb);
}
```

```python
from hmac import compare_digest
compare_digest(provided_token, expected_token)  # bytes or str; constant-time
```

## JWTs done correctly

A JWT is **signed, not encrypted** — anyone can read the payload. Never put secrets (passwords, PII, card data) in claims.

Non-negotiable rules:
1. **Pin the algorithm** on verify with an explicit allowlist (`algorithms: ['RS256']`). This kills the classic `alg: none` bypass and the RS256→HS256 confusion attack (attacker signs with the public key as the HMAC secret).
2. **Symmetric (HS256)** = one shared secret; only for single-service setups. **Asymmetric (RS256/ES256/EdDSA)** = sign with private key, verify with public key; use this the moment more than one service verifies tokens.
3. **Always set and validate** `exp`, `iss`, `aud`. Access-token TTL **5–15 min**.
4. Secret ≥ 32 random bytes for HS256. Rotate keys via a **JWKS** endpoint + `kid` header for asymmetric.

```js
// jsonwebtoken (npm i jsonwebtoken) — HS256, single service
import jwt from 'jsonwebtoken';

const token = jwt.sign(
  { sub: user.id, role: user.role },
  process.env.JWT_SECRET,                       // >= 32 random bytes
  { algorithm: 'HS256', expiresIn: '15m', issuer: 'api.example.com', audience: 'web' }
);

const claims = jwt.verify(token, process.env.JWT_SECRET, {
  algorithms: ['HS256'],                        // MUST pin
  issuer: 'api.example.com',
  audience: 'web',
});
```

```js
// jose (npm i jose) — asymmetric EdDSA, multi-service; verify with a rotating JWKS
import { SignJWT, jwtVerify, createRemoteJWKSet } from 'jose';

const jwt = await new SignJWT({ role: user.role })
  .setProtectedHeader({ alg: 'EdDSA', kid: currentKeyId })
  .setSubject(user.id).setIssuer('api.example.com').setAudience('web')
  .setIssuedAt().setExpirationTime('15m')
  .sign(privateKey);

const JWKS = createRemoteJWKSet(new URL('https://api.example.com/.well-known/jwks.json'));
const { payload } = await jwtVerify(jwt, JWKS, { issuer: 'api.example.com', audience: 'web' });
// jose enforces exp automatically; iss/aud are checked because you passed them.
```

### Refresh tokens + rotation (the part people skip)

- **Access token: JWT, 5–15 min, stateless.** **Refresh token: opaque random string, stored hashed server-side, long-lived (days–weeks).**
- **Rotate on every use:** issue a new refresh token, invalidate the old one. If an already-used (old) refresh token is presented, that's **theft — revoke the entire token family/session** and force re-login. This reuse detection is the whole point of rotation.
- Store refresh tokens in an **HttpOnly, Secure, SameSite=Strict** cookie for web; keychain/keystore for mobile.
- Keep a per-user `token_version` (or session id) integer. Bump it on logout/password-change → all outstanding access tokens fail the check at next verify without a per-token denylist.

## OAuth2 / OIDC — the 20% you need

- **OAuth2 = authorization** (access to resources). **OIDC = authentication** layered on OAuth2; it adds the **ID token** (a JWT about *who the user is*). "Log in with Google" = OIDC.
- **Only use the Authorization Code flow + PKCE.** Implicit flow and Resource-Owner-Password-Credentials flow are **deprecated — never use them.** PKCE is mandatory for public clients (SPA, mobile) and recommended for all.
- **ID token** → prove identity, read user claims; validate `iss`, `aud`, `exp`, `nonce`, signature. **Access token** → call APIs; do not parse it for identity if it's opaque.
- **Always validate `state`** (CSRF protection on the redirect) and **`nonce`** (replay protection on the ID token). Reject on mismatch.
- Register **exact** redirect URIs; never allow open/wildcard redirects (token exfiltration vector).

PKCE in one breath: client generates a random `code_verifier`; sends `code_challenge = BASE64URL(SHA256(code_verifier))` on the authorize request; sends the raw `code_verifier` on the token exchange. Server checks they match — a stolen auth code is useless without the verifier.

```js
import { randomBytes, createHash } from 'node:crypto';
const codeVerifier = randomBytes(32).toString('base64url');
const codeChallenge = createHash('sha256').update(codeVerifier).digest('base64url');
// authorize:  ...&code_challenge=<codeChallenge>&code_challenge_method=S256&state=<random>&nonce=<random>
// token:      ...&code=<code>&code_verifier=<codeVerifier>
```

## Cookies: the exact flags

```
Set-Cookie: __Host-sid=<opaque>; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=1209600
```

- `HttpOnly` — JS can't read it (blocks XSS token theft). Mandatory for auth cookies.
- `Secure` — HTTPS only. Mandatory.
- `SameSite=Lax` — good default; blocks cross-site POST CSRF while allowing top-level nav. Use `Strict` for refresh/admin cookies. Only use `SameSite=None` (requires `Secure`) if you genuinely need cross-site sending — and then you **must** add anti-CSRF tokens.
- `__Host-` prefix (`__Host-sid`) locks the cookie to the exact host, requires `Path=/` + `Secure`, and forbids a `Domain` attribute — strongest binding. Use it for session cookies.
- Set a real `Max-Age`/expiry; rotate the session id on privilege change (login, role elevation) to prevent session fixation.

## CORS — restrictive, per-origin, never reflect blindly

- **CORS is not a security boundary for your server** — it's browser-enforced and only constrains browser JS. It does not protect against non-browser clients (curl, server-to-server). Authz still lives server-side.
- **Allowlist exact origins.** Never combine `Access-Control-Allow-Origin: *` with `Access-Control-Allow-Credentials: true` — the browser rejects it, and reflecting the request `Origin` unchecked is a credential-leak hole.
- When you need credentialed cross-origin requests, echo the origin **only if it's in your allowlist**, and set `Vary: Origin`.

```js
// Express (npm i cors)
import cors from 'cors';
const allow = new Set(['https://app.example.com', 'https://admin.example.com']);
app.use(cors({
  origin: (origin, cb) => cb(null, !origin || allow.has(origin)), // no origin = same-origin / non-browser
  credentials: true,
  methods: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
  allowedHeaders: ['Content-Type', 'Authorization'],
  maxAge: 600,
}));
```

## Rate limiting & brute-force defense

- **Rate-limit by a stable key**, not raw IP alone (IPs are shared/spoofable behind proxies). Layer: per-IP, per-account, per-endpoint. Behind a proxy, set `app.set('trust proxy', <hops>)` for your known proxy count so `req.ip` reads the correct hop — never blindly trust `X-Forwarded-For`.
- **Tighter limits on auth endpoints** (login, password reset, token, signup, OTP verify) — e.g. 5–10/min per account with exponential backoff + lockout. General API: token bucket, e.g. 100/min/user.
- Use a **shared store (Redis)** so limits hold across instances. In-memory limiters are useless once you scale horizontally.
- Add **global concurrency/body-size limits** to blunt DoS. Return `429` with a `Retry-After` header.

```js
// express-rate-limit v7+/v8 + Redis (npm i express-rate-limit rate-limit-redis)
import rateLimit, { ipKeyGenerator } from 'express-rate-limit';
import { RedisStore } from 'rate-limit-redis';

const loginLimiter = rateLimit({
  windowMs: 60_000,
  limit: 8,
  standardHeaders: 'draft-7',           // RateLimit-* headers (draft-8 also supported)
  legacyHeaders: false,
  // Per-account, falling back to IP. MUST wrap req.ip in ipKeyGenerator — a bare
  // `?? req.ip` throws ERR_ERL_KEY_GEN_IPV6 at startup (IPv6 addresses would each
  // count as a distinct key and bypass the limit).
  keyGenerator: (req) => req.body?.email ?? ipKeyGenerator(req.ip),
  store: new RedisStore({ sendCommand: (...args) => redis.sendCommand(args) }),
});
app.post('/login', loginLimiter, loginHandler);
```

## Input validation — allowlist at the boundary

- **Validate and parse every external input** (body, query, params, headers) against a strict schema at the edge. Reject unknown fields (`strict()`), coerce types explicitly, bound lengths/ranges. Deny-by-default: define what's allowed, not what's forbidden.
- **Validation != output encoding.** Validation stops malformed data; you *also* need context-aware output encoding to stop injection (see below).
- Parse into a typed object and pass *that* downstream — never thread raw `req.body` through your app.

```ts
import { z } from 'zod';

// Zod 4: use top-level z.email() (the z.string().email() method form is deprecated).
const CreateUser = z.object({
  email: z.email().max(254),
  age: z.coerce.number().int().min(13).max(120),
  role: z.enum(['user', 'admin']).default('user'),
}).strict();                              // reject unexpected keys

app.post('/users', (req, res) => {
  const parsed = CreateUser.safeParse(req.body);
  if (!parsed.success) {
    return res.status(422).json({ errors: z.flattenError(parsed.error) });
  }
  createUser(parsed.data);               // typed, clean
  res.sendStatus(201);
});
```

## Injection: SQL, NoSQL, command, SSRF

- **SQL: always parameterize / use bound placeholders.** String-concatenating user input into SQL is the #1 way small apps get dumped. An ORM or query builder is fine *as long as* you never drop to raw concatenated SQL.

  ```js
  // GOOD — parameterized
  await db.query('SELECT * FROM users WHERE email = $1', [email]);
  // BAD  — never do this
  await db.query(`SELECT * FROM users WHERE email = '${email}'`);
  ```

- **NoSQL (Mongo):** reject objects where you expect scalars — `{ email: { $ne: null } }` is an auth-bypass payload. Your Zod schema (`z.string()`) already blocks this; that's a security control, not just hygiene.
- **OS commands:** avoid shelling out. If you must, use `execFile`/`spawn` with an **args array** (never `exec` with an interpolated string), and never pass user input as the command name.
- **SSRF:** if the server fetches a user-supplied URL (webhooks, image imports, link previews), **allowlist schemes + hosts**, resolve DNS and **block private/link-local ranges** (`127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `169.254.0.0/16` incl. the `169.254.169.254` cloud-metadata IP, `::1`, `fc00::/7`), and disable redirects to internal targets. Re-resolve and re-check after every redirect (DNS rebinding).

## Output encoding & security headers

- **Escape on output, contextually** (HTML body vs attribute vs JS vs URL). If you render HTML, use an auto-escaping template engine; if you accept rich HTML, sanitize with a maintained allowlist library (DOMPurify). Do not roll your own regex sanitizer.
- **Set a Content-Security-Policy.** A tight CSP is your last line of defense against XSS token/data theft. Start `default-src 'self'`; avoid `'unsafe-inline'`.
- Ship secure headers by default (helmet): `Content-Security-Policy`, `Strict-Transport-Security` (HSTS), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and `frame-ancestors 'none'` in CSP (supersedes the legacy `X-Frame-Options: DENY`).

```js
import helmet from 'helmet';
app.use(helmet()); // sane defaults; then tighten the CSP explicitly for your app
```

## Access control (Broken Access Control = OWASP #1)

This is the bug that bites small apps hardest. Authentication proves *who you are*; **authorization** proves *you may do this specific thing to this specific object*.

- **Enforce authz server-side on every request**, per object. Never rely on the UI hiding a button or a client-sent role.
- **IDOR / BOLA:** the killer. `GET /orders/123` must verify order 123 **belongs to the caller** — don't just check they're logged in. Every object lookup includes an ownership/tenant predicate.

  ```js
  // BAD: any authenticated user can read any order
  const order = await db.order.findById(req.params.id);
  // GOOD: scope to the caller
  const order = await db.order.findOne({ id: req.params.id, userId: req.user.id });
  if (!order) return res.sendStatus(404); // 404 not 403 — don't leak existence
  ```

- **Deny by default.** New routes are inaccessible until you grant access. Centralize checks in middleware/policies, not scattered `if` statements.
- **Never trust a client-supplied role/`isAdmin`/tenant id.** Derive authorization facts from the server-side session/verified token only. Privilege comes from the store, not the request body.
- Multi-tenant: put `tenant_id` in **every** query. A missing tenant predicate = cross-tenant data leak.

## Secrets management

- **Secrets never live in source, git history, client bundles, or logs.** Rotate immediately any secret that touched a repo. Add a pre-commit secret scanner (gitleaks) and gitignore `.env`.
- **Load secrets from the environment or a secrets manager** (AWS/GCP Secret Manager, Vault, Doppler). `.env` files are for local dev only.
- **Validate required secrets at boot and crash if missing** — fail fast, don't run half-configured.

  ```ts
  import { z } from 'zod';
  const Env = z.object({
    JWT_SECRET: z.string().min(32),
    DATABASE_URL: z.url(),               // Zod 4 top-level url validator
    NODE_ENV: z.enum(['development', 'test', 'production']),
  });
  export const env = Env.parse(process.env); // throws at startup if anything is missing/weak
  ```

- **Different secrets per environment.** Never reuse prod secrets in staging/dev.
- **Scrub secrets, tokens, passwords, and PII from logs.** Redact `authorization` headers and `password`/`token` fields before logging. Log auth *events* (login success/fail, token issue/revoke) for audit — without the credentials.
- Encrypt sensitive data at rest; use TLS 1.2+ everywhere in transit (no plaintext internal hops on shared networks).

## Error handling that doesn't leak

- **Return generic errors to clients; log the detail server-side.** Never send stack traces, SQL, or file paths to the client in production.
- **Uniform auth failures:** login with a wrong password vs a nonexistent user must return the *same* message and *similar* timing — otherwise you've built a user-enumeration oracle. (Hash a dummy password on the not-found path to equalize timing.) The same uniformity applies to signup and password-reset responses.
- Use precise status codes: `400/422` bad input, `401` unauthenticated, `403` authenticated-but-forbidden, `404` to hide existence from unauthorized callers, `429` rate-limited.

## Pre-ship security checklist

- [ ] Passwords hashed with Argon2id (or bcrypt cost ≥12 with correct base64 pre-hash for >72B).
- [ ] All secret/token comparisons constant-time.
- [ ] JWT verify pins `algorithms` and checks `exp`/`iss`/`aud`; no secrets in claims.
- [ ] Refresh tokens rotate with reuse-detection; access TTL ≤ 15 min.
- [ ] Auth cookies: `HttpOnly; Secure; SameSite`; `__Host-` prefix on session id.
- [ ] CORS allowlist is explicit; no `*` + credentials.
- [ ] Rate limiting on auth endpoints via shared store; `429` + `Retry-After`; `keyGenerator` wraps IP via `ipKeyGenerator`.
- [ ] Every input validated against a strict schema at the edge; unknown fields rejected.
- [ ] Every object query scoped to the caller/tenant (no IDOR); privilege derived server-side.
- [ ] All DB access parameterized; no string-built SQL; scalars validated (no NoSQL operator injection).
- [ ] Security headers (helmet + tight CSP `frame-ancestors 'none'`) + HSTS set.
- [ ] Secrets from env/manager, validated at boot, gitignored, scrubbed from logs.
- [ ] SSRF guard on any server-side fetch of user URLs (block private ranges + metadata IP, re-check on redirect).
- [ ] Generic client errors; uniform auth-failure responses (login/signup/reset); deps audited in CI (`npm audit` / `pip-audit`).
