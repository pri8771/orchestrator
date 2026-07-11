<!-- keywords: node api server, express typescript, fastify typescript, zod validation, async error handling express, request validation middleware, env config validation, typescript backend structure, express 5, fastify 5, zod 4, error handling middleware, graceful shutdown node, typed request handler, api project layout, express router, fastify plugin, fastify-type-provider-zod, asyncHandler, route validation zod, http error class, pino logging, cors helmet, typescript esm node, req.query read-only express 5, req.params string array, exactOptionalPropertyTypes, noUncheckedIndexedAccess, withTypeProvider, typed env schema, production node server -->

# Node.js API Servers in TypeScript (Express 5 / Fastify 5)

Reference for building production-shaped HTTP API servers. Targets **Node 22+ LTS**, **TypeScript 5.6+**, **Express 5.x**, **Fastify 5.x**, **Zod 4.x**, **pino 10.x**, native ESM. Every snippet compiles under the strict `tsconfig` below (including `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes`) — verified against Express 5.2, Fastify 5.9, Zod 4.4, `fastify-type-provider-zod` 7.

## Decision: Express vs Fastify

- **Fastify** — default for new services. ~2-3x throughput, schema-first (JSON Schema validation + fast serialization built in), first-class TS via type providers, structured `pino` logging out of the box, encapsulated plugin system. Prefer unless a hard dependency forces Express.
- **Express 5** — pick when the middleware you need is Express-only, or the team already knows it. v5 fixed the biggest historical wart: **async handlers that throw/reject now propagate to error middleware automatically** (no more silent hangs). Still no built-in validation/serialization — you bolt on Zod.
- Do **not** start new projects on Express 4. Skip Koa/Hapi for greenfield without a specific reason.

## Baseline tooling & tsconfig

- Native ESM (`"type": "module"`). Use `.js` extensions in relative import specifiers under `NodeNext`, or run TS directly with `tsx` / Node's native type stripping (stable since Node 23; `--experimental-strip-types` on 22.6+).
- Compile with `tsc` for prod; `tsx watch` for dev. `ts-node` is legacy — avoid.

```jsonc
// tsconfig.json
{
  "compilerOptions": {
    "target": "ES2023",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "lib": ["ES2023"],
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "noImplicitOverride": true,
    "verbatimModuleSyntax": true,
    "skipLibCheck": true,
    "sourceMap": true,
    "declaration": false,
    "resolveJsonModule": true
  },
  "include": ["src"]
}
```

- `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes` catch a large class of real API bugs, but they change how you write two things: (1) you cannot assign `undefined` to an optional property — **omit the key** instead (conditionally spread it); (2) index/param access widens to include `undefined`. Both show up below.
- `verbatimModuleSyntax` forces `import type` for type-only imports — prevents runtime import of type-only modules.

## Project structure

Feature-first (vertical slice), not layer-first. Group by domain, not technical role.

```
src/
  app.ts            # builds & wires the app instance (no listen) — importable by tests
  server.ts         # entrypoint: env load, create app, listen, signal handlers
  config/env.ts     # zod-validated process.env -> typed `env` object
  lib/
    http-error.ts   # AppError class + factories
    async-handler.ts# (Express only) wraps async route fns
  middleware/
    error-handler.ts
    validate.ts     # (Express only) zod validation middleware
  modules/users/
    users.routes.ts
    users.service.ts
    users.schema.ts # zod schemas + inferred types
    users.repo.ts
  types/express.d.ts# module augmentation (req.id, req.user, req.valid)
```

- Keep `app.ts` free of `listen()` / `process.exit()` so tests import it and drive it with `supertest` / `app.inject()`.
- Business logic lives in `*.service.ts` and must not import `express`/`fastify` or touch `req`/`res`. Handlers are thin adapters: parse input → call service → shape response.

## Config via env (Zod-validated, fail-fast)

Validate `process.env` **once at boot**; export a typed, frozen object. A missing/invalid var should crash immediately, not at first request.

```ts
// src/config/env.ts
import { z } from "zod";

const EnvSchema = z.object({
  NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
  PORT: z.coerce.number().int().positive().default(3000),
  // z.url() accepts any well-formed URL, including non-http schemes (postgres://, redis://).
  DATABASE_URL: z.url(),
  LOG_LEVEL: z
    .enum(["fatal", "error", "warn", "info", "debug", "trace"])
    .default("info"),
  JWT_SECRET: z.string().min(32), // secrets: require length, never a default
  CORS_ORIGIN: z.string().default("*"),
});

// z.coerce.number() turns the string "3000" into a number — env vars are always strings.
const parsed = EnvSchema.safeParse(process.env);
if (!parsed.success) {
  // z.prettifyError (Zod 4) gives a readable multi-line report.
  console.error("Invalid environment variables:\n", z.prettifyError(parsed.error));
  process.exit(1);
}

export const env = Object.freeze(parsed.data);
export type Env = typeof env;
```

- Load `.env` before this runs. Node 20.6+ supports `node --env-file=.env`; prefer it over `dotenv` for the entrypoint. Dev: `tsx watch --env-file=.env src/server.ts`.
- Never log the whole `env` object — redact in the logger config instead.

## Zod schemas & inferred types (single source of truth)

Define the schema, infer the type — never hand-write a parallel `interface`.

```ts
// src/modules/users/users.schema.ts
import { z } from "zod";

export const CreateUserBody = z.object({
  email: z.email().max(320),
  name: z.string().min(1).max(200),
  age: z.number().int().min(0).max(150).optional(),
  role: z.enum(["admin", "member"]).default("member"),
});
export type CreateUserBody = z.infer<typeof CreateUserBody>;

export const UserIdParams = z.object({ id: z.uuid() });
export type UserIdParams = z.infer<typeof UserIdParams>;

export const ListUsersQuery = z.object({
  // Query params arrive as strings — coerce.
  limit: z.coerce.number().int().min(1).max(100).default(20),
  cursor: z.string().optional(),
});
export type ListUsersQuery = z.infer<typeof ListUsersQuery>;
```

- **Zod 4 idioms**: prefer top-level string formats — `z.email()`, `z.uuid()`, `z.url()`, `z.iso.datetime()`, `z.ipv4()` — over the chained `z.string().email()` forms (still functional but legacy-leaning; some checks like `.ip()` were removed in favor of `z.ipv4()`/`z.ipv6()`). Error handling changed from v3: use `z.treeifyError(err)` / `z.prettifyError(err)` / `err.issues`; `.format()`/`.flatten()` ergonomics are gone.
- Object schemas **strip** unknown keys by default. Use `.strict()` to reject them, `.loose()` (v4 rename of `.passthrough()`) to forward them.
- Keep client-input schemas separate from stored-entity schemas rather than deriving one from the other with drifting `.partial()`/`.omit()` chains.

## HTTP error model

One error class carrying an HTTP status + a stable machine-readable code. Everything the client should see is an `AppError`; anything else is an unexpected 500.

```ts
// src/lib/http-error.ts
export class AppError extends Error {
  readonly statusCode: number;
  readonly code: string;
  readonly details?: unknown;
  readonly expose: boolean; // safe to send message to client?

  constructor(opts: {
    statusCode: number;
    code: string;
    message: string;
    details?: unknown;
    expose?: boolean;
    cause?: unknown;
  }) {
    super(opts.message, { cause: opts.cause }); // native cause (ES2022)
    this.name = "AppError";
    this.statusCode = opts.statusCode;
    this.code = opts.code;
    if (opts.details !== undefined) this.details = opts.details;
    this.expose = opts.expose ?? opts.statusCode < 500;
    Error.captureStackTrace?.(this, AppError);
  }
}

export const NotFound = (message = "Not found", details?: unknown) =>
  new AppError({ statusCode: 404, code: "NOT_FOUND", message, details });
export const BadRequest = (message = "Bad request", details?: unknown) =>
  new AppError({ statusCode: 400, code: "BAD_REQUEST", message, details });
export const Unauthorized = (message = "Unauthorized") =>
  new AppError({ statusCode: 401, code: "UNAUTHORIZED", message });
export const Conflict = (message = "Conflict", details?: unknown) =>
  new AppError({ statusCode: 409, code: "CONFLICT", message, details });
```

- `expose` gates whether `message` reaches the client. 4xx → yes; 5xx → generic message to client, log the real one. Never leak internal error text or stack traces on 500s.
- Return a **consistent envelope** everywhere: `{ error: { code, message, details? } }`. Clients parse `code`, not prose.

## Logging (pino)

Structured JSON, fast, ecosystem standard. Fastify creates its own instance (see below); for Express construct one and share it.

```ts
// src/lib/logger.ts
import { pino, type LoggerOptions } from "pino";
import { env } from "../config/env.js";

const options: LoggerOptions = {
  level: env.LOG_LEVEL,
  // Redact secrets/PII from logged objects.
  redact: ["req.headers.authorization", "req.headers.cookie", "*.password"],
  // exactOptionalPropertyTypes: you cannot set `transport: undefined` — omit the key.
  // Pretty transport in dev only; raw JSON to stdout in prod.
  ...(env.NODE_ENV === "development"
    ? { transport: { target: "pino-pretty", options: { colorize: true } } }
    : {}),
};

export const logger = pino(options);
```

---

# Fastify path

Fastify does most of the work: request-id, logging, schema validation, and fast serialization are built in.

## Type-safe Fastify with the Zod type provider

`fastify-type-provider-zod` (v7) lets route `body`/`params`/`querystring`/`reply` schemas be Zod, with handlers fully typed and **zero casts**.

```ts
// src/app.ts
import Fastify from "fastify";
import {
  serializerCompiler,
  validatorCompiler,
  type ZodTypeProvider,
} from "fastify-type-provider-zod";
import { randomUUID } from "node:crypto";
import { env } from "./config/env.js";
import { registerErrorHandler } from "./middleware/error-handler.js";
import { usersRoutes } from "./modules/users/users.routes.js";
import { healthRoutes } from "./modules/health/health.routes.js";

// Do NOT annotate the return type: .withTypeProvider() changes the instance type,
// and writing `: FastifyInstance` erases the provider and mistypes every route.
export function buildApp() {
  const app = Fastify({
    // Let Fastify own the pino instance via `logger` config — passing a raw
    // `loggerInstance` narrows the logger type param and breaks helpers typed
    // against FastifyBaseLogger under exactOptionalPropertyTypes.
    logger: {
      level: env.LOG_LEVEL,
      redact: ["req.headers.authorization", "req.headers.cookie", "*.password"],
      ...(env.NODE_ENV === "development"
        ? { transport: { target: "pino-pretty", options: { colorize: true } } }
        : {}),
    },
    requestIdHeader: "x-request-id", // trust upstream proxy's id if present
    genReqId: () => randomUUID(),
    disableRequestLogging: false,
  }).withTypeProvider<ZodTypeProvider>();

  // Wire Zod as validator + serializer.
  app.setValidatorCompiler(validatorCompiler);
  app.setSerializerCompiler(serializerCompiler);

  registerErrorHandler(app);

  // Route groups are plugins; prefix scopes them.
  app.register(healthRoutes);
  app.register(usersRoutes, { prefix: "/api/v1/users" });

  return app;
}
```

```ts
// src/server.ts
import { buildApp } from "./app.js";
import { env } from "./config/env.js";

const app = buildApp();

const start = async () => {
  try {
    await app.listen({ port: env.PORT, host: "0.0.0.0" }); // 0.0.0.0 required in containers
  } catch (err) {
    app.log.error(err);
    process.exit(1);
  }
};

// Graceful shutdown: stop accepting, drain, exit.
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, async () => {
    app.log.info({ signal }, "shutting down");
    await app.close();
    process.exit(0);
  });
}

void start();
```

## Fastify routes (schema-first, typed handlers)

```ts
// src/modules/users/users.routes.ts
import type { FastifyPluginAsyncZod } from "fastify-type-provider-zod";
import { z } from "zod";
import { CreateUserBody, UserIdParams, ListUsersQuery } from "./users.schema.js";
import * as usersService from "./users.service.js";
import { NotFound } from "../../lib/http-error.js";

const UserResponse = z.object({
  id: z.uuid(),
  email: z.string(),
  name: z.string(),
  role: z.enum(["admin", "member"]),
});

export const usersRoutes: FastifyPluginAsyncZod = async (app) => {
  app.get(
    "/",
    { schema: { querystring: ListUsersQuery, response: { 200: z.array(UserResponse) } } },
    async (req) => usersService.list(req.query), // req.query typed as ListUsersQuery — no cast
  );

  app.get(
    "/:id",
    { schema: { params: UserIdParams, response: { 200: UserResponse } } },
    async (req) => {
      const user = await usersService.getById(req.params.id);
      if (!user) throw NotFound(`User ${req.params.id} not found`);
      return user;
    },
  );

  app.post(
    "/",
    { schema: { body: CreateUserBody, response: { 201: UserResponse } } },
    async (req, reply) => {
      const user = await usersService.create(req.body);
      reply.code(201);
      return user;
    },
  );
};
```

- **Response schemas are not just validation** — Fastify compiles them into a fast serializer that **strips fields not in the schema**, preventing leaks (password hashes, tokens). Define them deliberately.
- Throwing inside an async handler/hook is caught and routed to the error handler. No wrapper needed.
- `return value` becomes the reply. Use `reply.code(201)` (`.status` is an alias) to set a non-200 status, then return the body.

## Fastify error handler

```ts
// src/middleware/error-handler.ts (Fastify)
import type { FastifyInstance, FastifyBaseLogger, RawServerDefault } from "fastify";
import type { IncomingMessage, ServerResponse } from "node:http";
import {
  hasZodFastifySchemaValidationErrors,
  type ZodTypeProvider,
} from "fastify-type-provider-zod";
import { AppError } from "../lib/http-error.js";

// Match the type-provider-augmented instance produced by buildApp().
type App = FastifyInstance<
  RawServerDefault,
  IncomingMessage,
  ServerResponse<IncomingMessage>,
  FastifyBaseLogger,
  ZodTypeProvider
>;

export function registerErrorHandler(app: App): void {
  app.setErrorHandler((err, req, reply) => {
    // Zod validation failures from the type provider.
    if (hasZodFastifySchemaValidationErrors(err)) {
      return reply.code(400).send({
        error: {
          code: "VALIDATION_ERROR",
          message: "Request validation failed",
          details: err.validation,
        },
      });
    }

    if (err instanceof AppError) {
      if (err.statusCode >= 500) req.log.error({ err }, err.code);
      return reply.code(err.statusCode).send({
        error: {
          code: err.code,
          message: err.expose ? err.message : "Internal server error",
          ...(err.details !== undefined ? { details: err.details } : {}),
        },
      });
    }

    req.log.error({ err }, "unhandled_error"); // unexpected: log full, return opaque 500
    return reply.code(500).send({
      error: { code: "INTERNAL", message: "Internal server error" },
    });
  });

  app.setNotFoundHandler((req, reply) => {
    reply.code(404).send({
      error: { code: "NOT_FOUND", message: `Route ${req.method} ${req.url} not found` },
    });
  });
}
```

## Fastify plugins for production

- `@fastify/helmet` — security headers.
- `@fastify/cors` — CORS (set `origin` from env; never blanket `*` with credentials).
- `@fastify/rate-limit` — per-IP throttling.
- `@fastify/under-pressure` — sheds load / fails health check on event-loop lag or memory pressure.
- `@fastify/sensible` — `httpErrors` helpers and useful defaults.
- Register security plugins **before** routes — order matters in Fastify's encapsulation model.

---

# Express 5 path

Express gives you a bare router; you supply validation, typing, and error plumbing.

## App wiring

```ts
// src/app.ts (Express)
import express, { type Express } from "express";
import helmet from "helmet";
import cors from "cors";
import { randomUUID } from "node:crypto";
import { env } from "./config/env.js";
import { errorHandler, notFoundHandler } from "./middleware/error-handler.js";
import { usersRouter } from "./modules/users/users.routes.js";
import { healthRouter } from "./modules/health/health.routes.js";

export function buildApp(): Express {
  const app = express();

  // Trust the first proxy hop (correct req.ip / rate limiting behind a LB).
  app.set("trust proxy", 1);

  app.use((req, _res, next) => {
    req.id = req.header("x-request-id") ?? randomUUID();
    next();
  });

  app.use(helmet());
  app.use(cors({ origin: env.CORS_ORIGIN === "*" ? true : env.CORS_ORIGIN.split(",") }));
  app.use(express.json({ limit: "1mb" })); // cap body size — unbounded JSON is a DoS vector

  app.use("/health", healthRouter);
  app.use("/api/v1/users", usersRouter);

  app.use(notFoundHandler); // no route matched
  app.use(errorHandler);    // must be LAST and have 4 args
  return app;
}
```

- The error handler must be registered **last** and must have the 4-arg signature `(err, req, res, next)` — Express identifies error middleware by arity.
- `express.json()` / `express.urlencoded()` are built in — do not add the separate `body-parser` package.

## Module augmentation for `req.id` / `req.user` / `req.valid`

Extend Express's types instead of casting `req as any`.

```ts
// src/types/express.d.ts
import "express";

declare global {
  namespace Express {
    interface Request {
      id: string;
      user?: { id: string; role: "admin" | "member" };
      // Parsed, typed inputs written by the validate() middleware.
      valid: { body?: unknown; params?: unknown; query?: unknown };
    }
  }
}
export {};
```

## Zod validation middleware (Express)

Express has no built-in validation. One middleware validates `body`/`params`/`query` and stashes the parsed values on `req.valid` — **never** reassign `req.query` (read-only in Express 5, see gotcha) or `req.body`/`req.params` (values are `string | string[]`, so raw reads are awkwardly typed under `noUncheckedIndexedAccess`).

```ts
// src/middleware/validate.ts
import type { RequestHandler } from "express";
import { z, type ZodType } from "zod";
import { AppError } from "../lib/http-error.js";

interface Schemas {
  body?: ZodType;
  params?: ZodType;
  query?: ZodType;
}

export function validate(schemas: Schemas): RequestHandler {
  return (req, _res, next) => {
    try {
      req.valid ??= {};
      if (schemas.body) req.valid.body = schemas.body.parse(req.body);
      if (schemas.params) req.valid.params = schemas.params.parse(req.params);
      if (schemas.query) req.valid.query = schemas.query.parse(req.query);
      next();
    } catch (err) {
      if (err instanceof z.ZodError) {
        return next(
          new AppError({
            statusCode: 400,
            code: "VALIDATION_ERROR",
            message: "Request validation failed",
            details: z.treeifyError(err),
          }),
        );
      }
      next(err);
    }
  };
}
```

- **Express 5 gotcha**: `req.query` is a **read-only getter** — assigning to it throws `TypeError: Cannot set property query ... which has only a getter` under strict mode (i.e. all ESM/TS). This is the #1 Express-4→5 break. Stashing on `req.valid` sidesteps it entirely. (`req.body`/`req.params` remain writable if you must mutate them.)
- Parse at the edge so handlers receive already-coerced, trusted, correctly-typed data.

## Async handler wrapper

Express 5 forwards rejected promises from async route handlers to error middleware automatically, so a bare `async (req, res) => { ... }` that throws is handled. This wrapper is still useful for older callback-style middleware and crisp types; on pure Express 5 async handlers you may drop it.

```ts
// src/lib/async-handler.ts
import type { Request, Response, NextFunction, RequestHandler } from "express";

type AsyncHandler = (req: Request, res: Response, next: NextFunction) => Promise<unknown>;

export const asyncHandler =
  (fn: AsyncHandler): RequestHandler =>
  (req, res, next) => {
    fn(req, res, next).catch(next);
  };
```

## Express routes

```ts
// src/modules/users/users.routes.ts (Express)
import { Router } from "express";
import { validate } from "../../middleware/validate.js";
import { asyncHandler } from "../../lib/async-handler.js";
import {
  CreateUserBody,
  UserIdParams,
  ListUsersQuery,
  type ListUsersQuery as ListUsersQueryT,
  type UserIdParams as UserIdParamsT,
  type CreateUserBody as CreateUserBodyT,
} from "./users.schema.js";
import * as usersService from "./users.service.js";
import { NotFound } from "../../lib/http-error.js";

export const usersRouter: Router = Router();

usersRouter.get(
  "/",
  validate({ query: ListUsersQuery }),
  asyncHandler(async (req, res) => {
    // Read the parsed, typed value — req.query itself is read-only in Express 5.
    const query = req.valid.query as ListUsersQueryT;
    res.json(await usersService.list(query));
  }),
);

usersRouter.get(
  "/:id",
  validate({ params: UserIdParams }),
  asyncHandler(async (req, res) => {
    // req.params.id is typed string | string[] — read the narrowed schema output.
    const { id } = req.valid.params as UserIdParamsT;
    const user = await usersService.getById(id);
    if (!user) throw NotFound(`User ${id} not found`);
    res.json(user);
  }),
);

usersRouter.post(
  "/",
  validate({ body: CreateUserBody }),
  asyncHandler(async (req, res) => {
    const user = await usersService.create(req.valid.body as CreateUserBodyT);
    res.status(201).json(user);
  }),
);
```

- **Express 5 path matching** upgraded to `path-to-regexp` v8. **Wildcards must be named** — `'/files/*'` is invalid; use `'/files/*splat'`. Optional params use `'/users{/:id}'`, not `'/users/:id?'`. Update old route strings.

## Express error handler

```ts
// src/middleware/error-handler.ts (Express)
import type { ErrorRequestHandler, RequestHandler } from "express";
import { AppError } from "../lib/http-error.js";
import { logger } from "../lib/logger.js";

export const notFoundHandler: RequestHandler = (req, res) => {
  res.status(404).json({
    error: { code: "NOT_FOUND", message: `Route ${req.method} ${req.path} not found` },
  });
};

export const errorHandler: ErrorRequestHandler = (err, req, res, _next) => {
  const requestId = req.id;

  if (err instanceof AppError) {
    if (err.statusCode >= 500) logger.error({ err, requestId }, err.code);
    res.status(err.statusCode).json({
      error: {
        code: err.code,
        message: err.expose ? err.message : "Internal server error",
        ...(err.details !== undefined ? { details: err.details } : {}),
        requestId,
      },
    });
    return;
  }

  logger.error({ err, requestId }, "unhandled_error");
  res.status(500).json({
    error: { code: "INTERNAL", message: "Internal server error", requestId },
  });
};
```

- If `res.headersSent` is already true, Express delegates to its default handler. The single-terminal-handler pattern (handlers `throw` rather than write-then-throw) avoids that case.

## Express server entrypoint & graceful shutdown

```ts
// src/server.ts (Express)
import { buildApp } from "./app.js";
import { env } from "./config/env.js";
import { logger } from "./lib/logger.js";

const app = buildApp();
const server = app.listen(env.PORT, () => {
  logger.info({ port: env.PORT }, "listening");
});

const shutdown = (signal: string) => {
  logger.info({ signal }, "shutting down");
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 10_000).unref(); // force-exit if drain stalls
};

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));

// Last-resort safety nets: log and crash (let the orchestrator restart).
process.on("unhandledRejection", (reason) => {
  logger.fatal({ reason }, "unhandledRejection");
  process.exit(1);
});
process.on("uncaughtException", (err) => {
  logger.fatal({ err }, "uncaughtException");
  process.exit(1);
});
```

- On `uncaughtException` the process is in an undefined state — **log and exit**, don't keep serving. Let Kubernetes/systemd/pm2 restart it.
- `.unref()` the force-exit timer so it doesn't keep the process alive on a clean shutdown.

---

# Cross-cutting rules

## Async error handling

- **Fastify**: throw anywhere in an async handler/hook → routed to `setErrorHandler`. Nothing to wire.
- **Express 5**: rejected async handlers are forwarded automatically. For non-async callbacks (streams, event emitters) you must still `next(err)` manually.
- Never mix `res.json()`/`return reply.send()` **and** `throw` in one path — writing a response then throwing causes `ERR_HTTP_HEADERS_SENT`. Send once, or throw and let the handler respond.
- Wrap third-party callback APIs in a promise at the service layer so handlers stay `async/await`.

## Validation discipline

- Validate every external input at the boundary: `body`, `params`, `query`, and **relevant headers**. Treat everything from the network as `unknown` until parsed.
- Coerce explicitly (`z.coerce.number()`, `z.coerce.boolean()`) for `params`/`query` — they arrive as strings.
- Use response schemas/serializers to control output and prevent field leakage. Never `res.json(entityFromDb)` if the entity has secret fields.
- Enforce body-size limits (`express.json({ limit })` / Fastify `bodyLimit`). Reject oversized payloads before parsing.

## Handlers vs services

- Handlers: parse request → call service → set status/serialize. No business logic, no inline DB calls.
- Services: pure-ish domain logic on plain typed objects; throw `AppError` for domain failures. No `req`/`res`.
- Repos: data access only. This layering keeps services unit-testable without HTTP.

## Health & readiness

```ts
// GET /health/live  -> process is up (always 200 if reachable)
// GET /health/ready -> dependencies (DB, cache) reachable; 503 with details if not
```

- `live` = "am I running" (orchestrator restart policy). `ready` = "can I serve traffic" (LB routing). Keep them separate.
- `ready` should ping real dependencies (`SELECT 1`) with a short timeout.

## Security defaults (checklist)

- `helmet()` / `@fastify/helmet` — sane security headers.
- CORS: explicit origin allowlist from env; never `origin: true` **with** `credentials: true` in production.
- Rate limiting per IP (`@fastify/rate-limit` / `express-rate-limit`).
- `trust proxy` set correctly so `req.ip` and rate limiting see the real client behind a LB.
- Never return stack traces or raw error messages on 5xx — gate via `AppError.expose`.
- Validate and cap all input sizes; set request timeouts.
- Redact `authorization`, `cookie`, and PII from logs.

## Testing the app

- Import `buildApp()` — never boot a real port in tests.
  - **Fastify**: `const res = await app.inject({ method: "POST", url: "/api/v1/users", payload })` — no network, fully typed.
  - **Express**: `import request from "supertest"; await request(buildApp()).post("/api/v1/users").send(payload)`.
- Assert on `statusCode`, the error `code`, and the response envelope — not on prose.
- Unit-test services directly with plain objects; reserve HTTP-level tests for routing/validation/serialization.

## Common pitfalls

- **Express 5 `req.query` is a read-only getter** — reassigning it throws under strict mode. Stash parsed values on `req.valid`.
- **Express 5 `req.params` values are `string | string[]`** — with `noUncheckedIndexedAccess`, raw `req.params.id` is `string | string[] | undefined`. Read the narrowed Zod output instead of the raw param.
- **Express 5 wildcards must be named** (`*splat`, not bare `*`); optional segments use `{/:id}`.
- **`exactOptionalPropertyTypes`**: never assign `undefined` to an optional prop (e.g. pino `transport`, error `details`) — conditionally spread the key instead.
- **Fastify**: don't annotate `buildApp()`'s return type after `.withTypeProvider()`, and prefer `logger: {…}` config over a custom `loggerInstance` (avoids logger-type-param mismatches in helpers).
- Forgetting `host: "0.0.0.0"` in Fastify `listen` inside Docker → server unreachable from outside the container.
- Reading `process.env` deep in the code instead of the validated `env` object → untyped `string | undefined` and runtime surprises.
- Reusing one Zod schema for input and DB entity → over-posting / field leakage. Keep input and output schemas distinct.
- Zod 4: `.format()`/`.flatten()` are gone — use `z.treeifyError` / `z.prettifyError` / `err.issues`.
- Not handling `SIGTERM` → dropped in-flight requests on deploy. Always drain with `close()` + a force-exit timeout.

KEYWORDS: node api server, express typescript, fastify typescript, zod validation, async error handling express, request validation middleware, env config validation, typescript backend structure, express 5, fastify 5, zod 4, error handling middleware, graceful shutdown node, typed request handler, api project layout, express router, fastify plugin, fastify-type-provider-zod, asyncHandler, route validation zod, http error class, pino logging, cors helmet, typescript esm node, req.query read-only express 5, req.params string array, exactOptionalPropertyTypes, noUncheckedIndexedAccess, withTypeProvider, typed env schema, production node server
