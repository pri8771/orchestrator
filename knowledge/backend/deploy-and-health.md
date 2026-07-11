<!-- keywords: health endpoint, GET /health 200, liveness probe, readiness probe, /ready 503, 12-factor config, environment variables, PORT env var, fail fast config, Zod 4 z.url, z.prettifyError, pydantic-settings, Dockerfile multi-stage, non-root container, exec form CMD, uv astral-sh Docker, SIGTERM handling, graceful shutdown, connection draining, server.close closeIdleConnections, srv.Shutdown, uvicorn timeout-graceful-shutdown unbounded, structured logging, JSON logs stdout, request id correlation, pino, slog, structlog dict_tracebacks, Fly.io fly.toml http_service checks, Render healthCheckPath, Render PORT 10000, Railway healthcheckPath, Railway healthcheckTimeout seconds, RAILWAY_DEPLOYMENT_DRAINING_SECONDS, Kubernetes livenessProbe readinessProbe, preStop hook, terminationGracePeriodSeconds, zero downtime deploy, npm start SIGTERM trap, bind 0.0.0.0, deploy checklist, Express 5 res.status json, FastAPI lifespan, Go net/http database/sql, distroless, dockerignore, secrets management -->

# Deploying and Operating a Small Backend

Reference for shipping a production-grade small backend: config, container, health/readiness, logging, graceful shutdown, and a fast path to a host. The single most load-bearing requirement: a real `GET /health` that returns `200`. Automated verification, load balancers, and platform deploy gates all poll it — if it's wrong, nothing else matters.

---

## The health endpoint (do this first, get it exactly right)

Rules that platforms and verifiers actually enforce:

- `GET /health` MUST return HTTP `200` with a small body when the process is up. `2xx`/`3xx` is often accepted, but return **exactly `200`** — it's what every checker treats as unambiguously healthy.
- It MUST be **cheap and dependency-free**: no DB query, no downstream calls, no auth. It answers "is this process alive and serving HTTP?" — nothing more. A slow health check causes flapping and failed deploys.
- It MUST be reachable **without authentication** and excluded from any auth middleware, rate limiter, or tenant/host guard.
- It MUST respond well under the checker timeout (default ~1–5s on most platforms). Aim for < 50ms.
- It MUST bind the same port/host the platform routes to (`0.0.0.0`, `$PORT`).
- Do NOT log every health hit at info level — checkers poll every few seconds and will drown your logs. Filter them out (see logging).
- Return JSON so humans can eyeball it, but the **status code is the contract**, not the body.

Split liveness from readiness once you have dependencies:

| Endpoint | Question | Checks deps? | Failure meaning | Used by |
|---|---|---|---|---|
| `GET /health` (liveness) | Is the process alive? | No | Restart the process | Platform restart policy, uptime monitors |
| `GET /ready` (readiness) | Can it serve real traffic *now*? | Yes (DB, cache, migrations) | Stop routing traffic; don't restart | Load balancer, deploy gate, rolling updates |

- Liveness failing → orchestrator kills and restarts. Never make liveness depend on a DB, or a DB blip triggers a restart storm.
- Readiness failing → orchestrator stops sending traffic but leaves the process running. This is also how you drain during shutdown (flip readiness to `503`).
- Small single-instance apps: one `/health` returning `200` is enough. Add `/ready` the moment you have a database or you deploy with rolling/zero-downtime.

### Health handler — Node (Express 5)

```js
// health.js
export function mountHealth(app, deps) {
  // Liveness: dependency-free, always 200 while the event loop runs.
  app.get("/health", (_req, res) => {
    res.status(200).json({ status: "ok" });
  });

  // Readiness: reflects whether we should receive traffic.
  app.get("/ready", async (_req, res) => {
    if (deps.shuttingDown) {
      return res.status(503).json({ status: "shutting_down" });
    }
    try {
      // Cheap liveness ping, not a full query. 1s budget.
      await deps.db.query("SELECT 1");
      res.status(200).json({ status: "ready" });
    } catch (err) {
      res.status(503).json({ status: "not_ready", reason: err.code ?? "db_error" });
    }
  });
}
```

### Health handler — Go (net/http, stdlib only)

```go
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"sync/atomic"
	"time"
)

type Health struct {
	db           *sql.DB
	shuttingDown atomic.Bool
}

func (h *Health) Live(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (h *Health) Ready(w http.ResponseWriter, r *http.Request) {
	if h.shuttingDown.Load() {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "shutting_down"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), time.Second)
	defer cancel()
	if err := h.db.PingContext(ctx); err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "not_ready"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
```

### Health handler — Python (FastAPI)

```python
# health.py
from fastapi import APIRouter, Request, Response
from sqlalchemy import text

router = APIRouter()
_state = {"shutting_down": False}

@router.get("/health")
def health():
    # Liveness: no dependencies, always 200 while the worker runs.
    return {"status": "ok"}

@router.get("/ready")
async def ready(request: Request, response: Response):  # engine on app.state
    if _state["shutting_down"]:
        response.status_code = 503
        return {"status": "shutting_down"}
    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:
        response.status_code = 503
        return {"status": "not_ready"}
```

**Common /health bugs that fail automated verification:**

- Binding to `127.0.0.1` instead of `0.0.0.0` → checker can't reach it from outside the container.
- Ignoring `$PORT` and hardcoding `3000`/`8080` → platform routes to the wrong port. Render's default `PORT` is `10000`; Railway/Fly inject their own — always read the env var.
- Auth/CSRF/host middleware sitting in front of `/health` → checker gets `401`/`403`.
- Health check that pings the DB → a slow DB fails the *liveness* check and triggers restart loops. Keep liveness dependency-free.
- Redirecting `/health` → `/health/` (trailing-slash 301) when the checker only accepts `200`.

---

## 12-factor config

- **All config comes from environment variables.** Nothing environment-specific baked into the image or committed to git. The same image promotes dev → staging → prod; only env differs.
- **Fail fast at boot** if a required variable is missing or malformed. A crash on startup with a clear message beats a `500` on the first request in prod. Validate once, at process start.
- **Read `PORT` from the environment.** Every PaaS assigns it. Default to a sane local value only as a fallback.
- **Never log secrets.** Load them, reference them by name, and keep them out of structured log fields and error messages.
- **No `.env` files in production.** `.env` is a *local dev* convenience. In prod, inject real environment variables via the platform's secret store. Add `.env` to `.gitignore` and `.dockerignore`.
- **Treat backing services as attached resources** addressed by URL/DSN from env (`DATABASE_URL`, `REDIS_URL`). Swapping a managed Postgres for another is a config change, not a code change.

### Typed, validated config — Node (Zod 4)

```js
// config.js
import { z } from "zod";

const schema = z.object({
  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
  PORT: z.coerce.number().int().positive().default(3000),
  DATABASE_URL: z.url(),                          // z.string().url() is deprecated in Zod 4
  LOG_LEVEL: z.enum(["debug", "info", "warn", "error"]).default("info"),
  SHUTDOWN_TIMEOUT_MS: z.coerce.number().int().default(10_000),
});

const parsed = schema.safeParse(process.env);
if (!parsed.success) {
  // z.prettifyError -> human-readable multiline string (Zod 4).
  console.error("Invalid environment:\n" + z.prettifyError(parsed.error));
  process.exit(1); // fail fast, do not boot half-configured
}

export const config = parsed.data;
```

### Typed config — Go (stdlib, no deps)

```go
type Config struct {
	Port            string        // PORT
	DatabaseURL     string        // DATABASE_URL
	LogLevel        string        // LOG_LEVEL
	ShutdownTimeout time.Duration // SHUTDOWN_TIMEOUT
}

func Load() (Config, error) {
	c := Config{
		Port:            getenv("PORT", "8080"),
		DatabaseURL:     os.Getenv("DATABASE_URL"),
		LogLevel:        getenv("LOG_LEVEL", "info"),
		ShutdownTimeout: 10 * time.Second,
	}
	if c.DatabaseURL == "" {
		return Config{}, errors.New("DATABASE_URL is required")
	}
	return c, nil
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
```

### Typed config — Python (pydantic-settings)

```python
# config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    port: int = 8000
    database_url: str            # required; ValidationError at import if missing
    log_level: str = "info"
    shutdown_timeout_s: float = 10.0

settings = Settings()  # raises on boot if DATABASE_URL is unset
```

---

## Dockerfile

Principles: **multi-stage build**, small final image, **non-root user**, pinned base tags, no build tools or secrets in the runtime layer, and correct signal handling so `SIGTERM` reaches your process.

### Node (multi-stage, non-root)

```dockerfile
# ---- build stage ----
FROM node:22-slim AS build
WORKDIR /app
# Copy manifests first for layer-cache reuse on unchanged deps.
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build            # if you have a build step; else skip

# ---- runtime stage ----
FROM node:22-slim AS runtime
ENV NODE_ENV=production
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /app/dist ./dist
# node:*-slim ships a non-root `node` user.
USER node
EXPOSE 3000
# Exec form (JSON array) => PID 1 is node, so it receives SIGTERM directly.
CMD ["node", "dist/server.js"]
```

### Go (static binary, distroless)

```dockerfile
# ---- build stage ----
FROM golang:1.24 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
# CGO off => fully static binary that runs on scratch/distroless.
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /app ./cmd/server

# ---- runtime stage ----
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /app /app
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/app"]
```

### Python (slim, non-root, uv)

```dockerfile
FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
# Copy uv from its official image (2026-standard fast installer); pip works too.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
RUN useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000
# exec + $PORT expansion so uvicorn is PID 1 and gets SIGTERM.
CMD ["sh", "-c", "exec uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```

### Dockerfile rules that bite people

- **Use exec form `CMD ["bin", "arg"]`, never shell form `CMD bin arg`.** Shell form runs your app as a child of `/bin/sh`, which does NOT forward `SIGTERM` — graceful shutdown silently never fires. If you must use a shell (e.g. to expand `$PORT`), prefix with `exec`: `CMD ["sh", "-c", "exec myserver --port $PORT"]` so the server replaces the shell as PID 1.
- **Run as non-root.** Add a `USER` line. Distroless `:nonroot` and the `node` user handle this for you.
- **Order layers by change frequency**: dependency manifests → install → source. Copying source before installing deps busts the cache on every code change.
- **Add a `.dockerignore`** (`node_modules`, `.git`, `.env`, `dist`, `__pycache__`, `*.log`). Keeps the build context small and secrets out of the image.
- **Pin base tags** to a major (`node:22-slim`, `python:3.13-slim`, `golang:1.24`). Avoid bare `latest`.
- **Don't add a Docker `HEALTHCHECK` that shells out to `curl`** in a distroless/slim image — `curl` isn't installed. Use a tiny built-in checker binary, or rely on the *platform's* HTTP health check against `/health` (preferred on Fly/Render/Railway).
- **Keep secrets out of build args and `ENV`.** Use build secrets (`RUN --mount=type=secret`) if you truly need one at build time.

---

## Structured logging

- **Log JSON to stdout/stderr. One event per line.** The platform captures stdout; you do not manage log files, rotation, or paths (12-factor: logs are event streams).
- **Structured, not string-concatenated.** Emit fields (`level`, `msg`, `timestamp`, `request_id`, `latency_ms`, `status`) so logs are queryable, not grep-only.
- **Attach a request/correlation ID** to every log line within a request. Read an inbound `X-Request-Id` if present, else generate one; echo it back in the response header.
- **Levels**: `debug` (local/verbose), `info` (normal ops), `warn` (recoverable oddities), `error` (needs attention). Default prod level `info`.
- **Never log secrets, tokens, full auth headers, or PII.** Redact.
- **Suppress health-check noise.** Skip access logs for `/health` and `/ready`, or they bury real traffic.
- **Log unhandled errors once, with a stack**, at the boundary — not at every layer they bubble through.

### Node — pino (fast JSON logger)

```js
// logger.js
import pino from "pino";
import { randomUUID } from "node:crypto";
import { config } from "./config.js";

export const logger = pino({
  level: config.LOG_LEVEL,
  // Prod: raw JSON to stdout. Use pino-pretty only in dev, piped: `node app | pino-pretty`.
  redact: ["req.headers.authorization", "*.password", "*.token"],
  formatters: { level: (label) => ({ level: label }) },
});

// Request logging with correlation id + health-check suppression:
export function requestLogger(req, res, next) {
  if (req.path === "/health" || req.path === "/ready") return next();
  const id = req.headers["x-request-id"] ?? randomUUID();
  res.setHeader("x-request-id", id);
  const start = process.hrtime.bigint();
  res.on("finish", () => {
    const ms = Number(process.hrtime.bigint() - start) / 1e6;
    logger.info(
      { request_id: id, method: req.method, path: req.path, status: res.statusCode, latency_ms: ms },
      "request",
    );
  });
  next();
}
```

### Go — slog (stdlib structured logging)

```go
import (
	"log/slog"
	"os"
)

func newLogger(level string) *slog.Logger {
	var lvl slog.Level
	_ = lvl.UnmarshalText([]byte(level)) // "info", "debug", ...
	h := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: lvl})
	return slog.New(h)
}

// Usage: attach request_id via logger.With(...) in middleware; skip /health.
logger.Info("request",
	"request_id", id, "method", r.Method, "path", r.URL.Path,
	"status", sw.status, "latency_ms", ms)
```

### Python — structlog (JSON to stdout)

```python
import structlog, logging, sys

logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.dict_tracebacks,   # machine-readable stack traces
        structlog.processors.JSONRenderer(),
    ],
)
log = structlog.get_logger()
log.info("request", request_id=rid, method=method, path=path, status=code, latency_ms=ms)
```

---

## Graceful shutdown

When a platform deploys a new version or scales down, it sends **`SIGTERM`**, waits a grace period, then sends **`SIGKILL`**. Handling `SIGTERM` correctly is what makes deploys drop zero requests.

The correct shutdown sequence:

1. **Receive `SIGTERM`.** Set `shuttingDown = true`.
2. **Flip readiness to `503`.** The load balancer sees `/ready` fail and stops routing *new* requests to this instance. Keep `/health` (liveness) at `200` so you aren't force-killed mid-drain.
3. **Stop accepting new connections** (`server.close()` / `Shutdown(ctx)`), but let in-flight requests finish.
4. **Drain in-flight requests** up to a bounded timeout (e.g. 10s).
5. **Close resources** (DB pool, Redis, queues) after HTTP is drained.
6. **`exit(0)`.** If the timeout elapses first, force-exit non-zero so you don't hang until `SIGKILL`.

Critical platform nuances:

- **The endpoint-removal race.** Orchestrators remove your instance from the router *asynchronously* while sending `SIGTERM`. For ~1–3s after `SIGTERM`, new requests may still arrive. That's exactly why steps 2–3 matter (keep serving what arrives; stop advertising ready). On Kubernetes, a `preStop` sleep of a few seconds bridges this gap; on PaaS it's handled for you if you keep serving in-flight during drain.
- **The platform's drain/grace window must exceed your shutdown timeout**, or you get `SIGKILL`ed mid-drain. If your app drains up to 10s, give the platform ≥ 15s.
- **Railway's default drain is `0s`** — it `SIGKILL`s immediately unless you set `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` (e.g. `15`). Without it, graceful shutdown is pointless there.
- **The package-manager trap (Node/Python).** If your start command is `npm start` / `yarn start` / `pnpm start`, the package manager becomes PID 1 and **swallows `SIGTERM`** — your handler never runs. Start your app *directly*: `node dist/server.js`, `uvicorn ...`. Same reason exec-form `CMD` matters.

### Node (Express/http) graceful shutdown

```js
// server.js
import http from "node:http";
import { config } from "./config.js";
import { logger } from "./logger.js";

const deps = { db, shuttingDown: false };
const server = http.createServer(app);
// Tighten idle keep-alive so drain doesn't wait the full timeout on idle sockets.
server.keepAliveTimeout = 5_000;
server.listen(config.PORT, "0.0.0.0", () => logger.info({ port: config.PORT }, "listening"));

function shutdown(signal) {
  logger.info({ signal }, "shutdown initiated");
  deps.shuttingDown = true;              // /ready now returns 503 -> LB drains us

  const forceTimer = setTimeout(() => {
    logger.error("forced shutdown after timeout");
    process.exit(1);
  }, config.SHUTDOWN_TIMEOUT_MS);
  forceTimer.unref();

  server.close(async () => {             // stop new conns, wait for in-flight
    try {
      await deps.db.end();               // close pool after HTTP drained
      logger.info("shutdown complete");
      clearTimeout(forceTimer);
      process.exit(0);
    } catch (err) {
      logger.error({ err }, "error during shutdown");
      process.exit(1);
    }
  });
  // Node 18.2+: proactively refuse idle keep-alive connections during drain.
  server.closeIdleConnections?.();
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT")); // Ctrl-C in dev
```

### Go graceful shutdown

```go
func main() {
	cfg, err := Load()
	if err != nil {
		log.Fatal(err)
	}
	h := &Health{db: db}
	srv := &http.Server{Addr: ":" + cfg.Port, Handler: mux(h)}

	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("listen: %v", err)
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	<-ctx.Done() // block until SIGTERM/SIGINT

	h.shuttingDown.Store(true) // /ready now returns 503

	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil { // drains in-flight, no new conns
		log.Printf("graceful shutdown failed: %v", err)
	}
	db.Close()
}
```

### Python (uvicorn/FastAPI) graceful shutdown

Uvicorn traps `SIGTERM`, stops accepting new connections, and drains in-flight requests before exiting. Do resource cleanup in FastAPI's lifespan and flip readiness on shutdown:

```python
# main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from config import settings
from health import router as health_router, _state

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = create_async_engine(settings.database_url)
    yield                               # ---- app runs ----
    _state["shutting_down"] = True      # /ready -> 503 during drain
    await app.state.engine.dispose()    # close DB pool on shutdown

app = FastAPI(lifespan=lifespan)
app.include_router(health_router)
```

Run uvicorn so `SIGTERM` reaches it directly (not through a shell/PM wrapper). **`--timeout-graceful-shutdown` has no default — uvicorn waits indefinitely for in-flight requests**, which can hang a deploy until the platform sends `SIGKILL`. Set an explicit bound (e.g. `--timeout-graceful-shutdown 10`) and give the platform a drain window ≥ that value (e.g. ≥ 15s).

---

## A simple path to a host

All four options build your `Dockerfile` (or auto-detect a buildpack), inject a `PORT`, and poll an HTTP health path. Wire `/health` to their check and you get automatic rollback on bad deploys. Before any deploy, clear the **deploy checklist** at the bottom — the same items each platform expects.

### Fly.io — `fly.toml`

Runs your container on Machines globally. Define the health check under `http_service`:

```toml
app = "my-backend"
primary_region = "iad"

[build]
  # Uses your Dockerfile by default.

[env]
  PORT = "8080"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "stop"   # scale to zero when idle (small apps / cost)
  auto_start_machines = true
  min_machines_running = 0

  [[http_service.checks]]
    method = "get"
    path = "/health"
    interval = "15s"
    timeout = "2s"
    grace_period = "5s"         # startup slack before checks count
```

- Deploy: `fly deploy`. Fly does a rolling replace and won't cut over if `/health` fails.
- Secrets: `fly secrets set DATABASE_URL=...` (encrypted, injected as env).
- `internal_port` MUST equal the port your app binds. Mismatch = health check can't connect.

### Render — `render.yaml` (Blueprint) or dashboard

```yaml
services:
  - type: web
    name: my-backend
    runtime: docker
    plan: starter
    healthCheckPath: /health     # enables zero-downtime deploys
    envVars:
      - key: DATABASE_URL
        sync: false              # set the value in the dashboard (secret)
```

- Render's default `PORT` is **`10000`** — read `PORT` from env; do not assume `8080`/`3000`.
- With `healthCheckPath` set, Render starts the new instance, polls `/health`, and only shifts traffic on a successful response (return `200`). If it keeps failing, the deploy is **canceled** and the old version stays live.
- Without `healthCheckPath`, Render only checks that you bound to the port — weaker. Always set it.

### Railway — `railway.json` (or `railway.toml`)

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "deploy": {
    "healthcheckPath": "/health",
    "healthcheckTimeout": 300,
    "restartPolicyType": "ON_FAILURE"
  }
}
```

- Railway injects `PORT`; bind to it. The health check runs against that same port.
- `healthcheckTimeout` is in **seconds** (default `300`).
- **Set `RAILWAY_DEPLOYMENT_DRAINING_SECONDS`** (e.g. `15`) as a service variable — default drain is `0s`, so without it your `SIGTERM` handler is `SIGKILL`ed instantly.
- The new deploy must pass `/health` before the old one is torn down (overlapping deploy = zero downtime).

### Plain container host (Kubernetes)

The reference probe model the PaaS options abstract:

```yaml
containers:
  - name: backend
    image: registry/my-backend:1.4.0
    ports: [{ containerPort: 8080 }]
    livenessProbe:                       # dependency-free; failure restarts the pod
      httpGet: { path: /health, port: 8080 }
      initialDelaySeconds: 5
      periodSeconds: 10
    readinessProbe:                      # deps + shutdown flag; failure drains, no restart
      httpGet: { path: /ready, port: 8080 }
      periodSeconds: 5
    lifecycle:
      preStop:
        exec: { command: ["sleep", "5"] }  # bridge async endpoint-removal race
terminationGracePeriodSeconds: 30          # MUST exceed app shutdown timeout, or SIGKILL mid-drain
```

---

## Deploy checklist (paste into your PR)

- [ ] `GET /health` returns exactly `200`, no auth, no dependencies, < 50ms.
- [ ] App binds `0.0.0.0:$PORT` (env-driven; not hardcoded; not `127.0.0.1`).
- [ ] `/ready` checks real deps and returns `503` when a dep is down or during shutdown.
- [ ] Config loaded and validated at boot; process exits non-zero on missing required env.
- [ ] Secrets only in the platform secret store; `.env` gitignored + dockerignored.
- [ ] Multi-stage Dockerfile, non-root `USER`, pinned base, exec-form `CMD`.
- [ ] `SIGTERM` handler: flip readiness → stop new conns → drain → close deps → `exit`.
- [ ] App started directly (no `npm start`/PM wrapper swallowing signals).
- [ ] Platform drain window ≥ app shutdown timeout (Railway: set `RAILWAY_DEPLOYMENT_DRAINING_SECONDS`; uvicorn: set `--timeout-graceful-shutdown`, which is otherwise unbounded).
- [ ] JSON logs to stdout; `/health` + `/ready` access logs suppressed; no secrets/PII logged.
- [ ] Verified locally: `docker run -e PORT=8080 -p 8080:8080 img` then `curl -f localhost:8080/health` → `200`.
