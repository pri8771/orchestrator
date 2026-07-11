<!-- keywords: postgres, schema design, normalization, denormalization, indexing, b-tree index, gin index, partial index, covering index, transactions, isolation levels, serializable, mvcc, migrations, prisma, sqlalchemy, orm, redis, caching, connection pooling, pgbouncer, jsonb, uuid, uuidv7, foreign keys, deadlocks, n+1 query, upsert, keyset pagination, advisory locks, materialized views, full text search, cache invalidation, optimistic locking, select for update -->

# Server-Side Data & Persistence

Reference for building correct, performant data layers on Postgres 16/17/18 with Prisma (TypeScript) or SQLAlchemy 2.0 (Python), plus when to reach for Redis. Default to Postgres; add Redis only for a concrete, measured need.

## Choosing a Data Store

- **Default to Postgres.** It handles relational data, JSON (`jsonb`), full-text search, arrays, geospatial (PostGIS), pub/sub (`LISTEN/NOTIFY`), and even queues (`SELECT ... FOR UPDATE SKIP LOCKED`). Don't add a second store until Postgres is a proven bottleneck.
- **Redis** for: caching, rate limiting, session stores, ephemeral counters, leaderboards (sorted sets), distributed locks, and low-latency job queues. Redis is not your source of truth — treat every key as reconstructable from Postgres.
- **Managed Postgres** (Neon, Supabase, RDS/Aurora, Cloud SQL) is the 2026 default. Serverless Postgres (e.g. Neon) requires a pooler — never open a raw connection per request from a serverless/edge function.

## Schema Design Fundamentals

### Types — pick the narrow, correct one
- **Primary keys:** prefer `uuid` for public/distributed IDs, or `bigint GENERATED ALWAYS AS IDENTITY` for internal sequential keys. Avoid `serial` — identity columns are the modern replacement. For UUIDs, prefer **UUIDv7** (time-ordered) to preserve index locality: native `uuidv7()` in Postgres 18+, or generate app-side on older versions. `gen_random_uuid()` (random v4) is built into Postgres 13+ (no extension) and fine when locality doesn't matter.
- **Text:** use `text`. `varchar(n)` gives no performance benefit over `text`; add a length limit only as a real business constraint via `CHECK (length(col) <= n)`.
- **Money:** `numeric(19,4)`, or store integer minor units (cents) as `bigint`. Never `float`/`double` for money.
- **Timestamps:** always `timestamptz` (stored as UTC), never `timestamp`. Set `DEFAULT now()`.
- **Enums:** Postgres native `enum` is compact but painful to alter (adding a value is fine; removing/reordering is not). A lookup table with an FK is the most flexible; a `text` column + `CHECK` constraint is a lightweight middle ground.
- **Booleans that are really states:** model lifecycle as a `status text` with a `CHECK` (`'draft' | 'active' | 'archived'`), not three separate booleans.

### Constraints are correctness, not decoration
- `NOT NULL` by default; make nullability a deliberate choice.
- Foreign keys with explicit `ON DELETE` behavior (`CASCADE`, `RESTRICT`, `SET NULL`). Postgres does **not** auto-index FK columns — add indexes yourself (see Indexing).
- `UNIQUE` constraints for natural keys (email, slug). Use partial unique indexes for "unique among non-deleted rows."
- `CHECK` constraints for invariants (`price >= 0`, valid status values).

```sql
CREATE TABLE users (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),  -- or uuidv7() on PG18+
  email       text NOT NULL,
  status      text NOT NULL DEFAULT 'active'
              CHECK (status IN ('active', 'suspended', 'deleted')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Case-insensitive uniqueness, only among non-deleted users
CREATE UNIQUE INDEX users_email_active_uniq
  ON users (lower(email))
  WHERE status <> 'deleted';

CREATE TABLE orders (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  total_cents bigint NOT NULL CHECK (total_cents >= 0),
  status      text NOT NULL DEFAULT 'pending',
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX orders_user_id_idx ON orders (user_id);  -- FKs need manual indexes
```

## Normalization vs Denormalization

### Normalize by default (aim for 3NF)
- Every non-key column depends on the key, the whole key, and nothing but the key.
- One fact in one place → no update anomalies. This is the correct starting point for almost every OLTP schema.
- Use join tables for many-to-many; never comma-separated strings or parallel arrays.

### Denormalize deliberately, for measured reasons
Denormalization trades write complexity and consistency risk for read speed. Justify each instance.

- **Computed/cached aggregates:** store `order_count` on `users` when recomputing via `COUNT(*)` is hot. Keep it correct with a trigger or transactional update, and provide a reconciliation job.
- **Point-in-time snapshots:** copy `product_name`/`unit_price` onto `order_items` so a historical order reflects the name and price at purchase time. This isn't denormalization for speed — it's **capturing a point-in-time fact**, and is correct even in a normalized design.
- **`jsonb` columns** for sparse, schemaless, or rarely-queried attributes (settings, metadata, third-party payloads). Don't use `jsonb` to dodge schema design for data you filter/join on regularly.

```sql
-- jsonb with a GIN index for containment queries
ALTER TABLE users ADD COLUMN preferences jsonb NOT NULL DEFAULT '{}';
-- jsonb_path_ops is smaller/faster but only supports the @> operator
CREATE INDEX users_prefs_gin ON users USING gin (preferences jsonb_path_ops);
-- Query: users who opted into email
SELECT id FROM users WHERE preferences @> '{"notifications": {"email": true}}';
```

> Rule of thumb: normalize until it hurts, denormalize until it works. Measure with `EXPLAIN (ANALYZE, BUFFERS)` before adding redundancy.

## Indexing

Indexes make reads fast and writes slightly slower. Index for your actual query patterns; every unused index is dead weight (and slows writes).

### Index types
- **B-tree** (default): equality and range (`=`, `<`, `>`, `BETWEEN`, `ORDER BY`, `LIKE 'prefix%'`). 95% of indexes.
- **GIN:** `jsonb` containment, arrays, and full-text search (`tsvector`).
- **GiST/SP-GiST:** geometric, ranges, nearest-neighbor.
- **BRIN:** huge append-only tables where physical order tracks a column (time-series) — tiny and cheap.
- **Hash:** equality only; rarely worth it over B-tree.

### Rules
- **Index FK columns** and any column in a `WHERE`, `JOIN`, or `ORDER BY` on a hot path.
- **Composite index column order matters:** put equality columns first, then the range/sort column. `(user_id, created_at)` serves `WHERE user_id = ? ORDER BY created_at`, but not a query filtering only on `created_at`.
- **Partial indexes** shrink the index and speed writes when queries always filter a subset (`WHERE status = 'active'`).
- **Covering indexes** (`INCLUDE`) enable index-only scans by carrying non-key columns.
- **Expression indexes** for functional queries (`lower(email)`).
- Build indexes on live tables with `CREATE INDEX CONCURRENTLY` to avoid blocking writes. It cannot run inside a transaction block, so migration tooling must run it outside the wrapping transaction.

```sql
-- Composite for "a user's recent orders", covering total so it's index-only
CREATE INDEX orders_user_recent_idx
  ON orders (user_id, created_at DESC)
  INCLUDE (total_cents);

-- Partial index: only index rows we actually query
CREATE INDEX orders_pending_idx
  ON orders (created_at)
  WHERE status = 'pending';

-- Full-text search via a generated tsvector column
ALTER TABLE products ADD COLUMN search_tsv tsvector
  GENERATED ALWAYS AS (
    to_tsvector('english', coalesce(name, '') || ' ' || coalesce(description, ''))
  ) STORED;
CREATE INDEX products_search_idx ON products USING gin (search_tsv);
-- Query
SELECT id FROM products WHERE search_tsv @@ websearch_to_tsquery('english', 'wireless mouse');
```

> Note: `to_tsvector` treats `NULL` inputs as `NULL`, so `coalesce` the columns — otherwise one NULL column makes the whole vector NULL.

### Diagnosing
- `EXPLAIN (ANALYZE, BUFFERS) SELECT ...` — look for `Seq Scan` on large tables in hot queries (bad) vs `Index Scan`/`Index Only Scan` (good).
- Find unused indexes: query `pg_stat_user_indexes` for `idx_scan = 0`.
- Find candidate missing indexes: high `seq_scan` / `seq_tup_read` in `pg_stat_user_tables`.

## Transactions & Concurrency

### ACID and isolation levels
Postgres default is **Read Committed**. Two stronger levels:
- **Repeatable Read:** snapshot isolation; prevents non-repeatable and phantom reads. Serialization failures (`40001`) possible under write conflicts — retry.
- **Serializable:** strongest; emulates serial execution via SSI. Can raise `40001 serialization_failure` — you **must** retry the whole transaction.

Choose per-transaction based on invariants. Money movement and inventory decrements often want Serializable or explicit locking.

```sql
BEGIN ISOLATION LEVEL SERIALIZABLE;
-- ... reads and writes that must be mutually consistent ...
COMMIT;  -- on 40001, retry the entire block
```

### Locking patterns
- **`SELECT ... FOR UPDATE`** — pessimistic row lock; the canonical "read a balance, then update it" pattern. Add `SKIP LOCKED` for queue workers so concurrent consumers grab different rows.
- **Optimistic locking** — add a `version` column; `UPDATE ... WHERE id = ? AND version = ?` (incrementing `version`) and check the affected-row count. Best under low contention.
- **Advisory locks** (`pg_advisory_xact_lock(key)`) — app-level mutex without a row to lock (e.g., "only one cron worker runs this job"). The `_xact_` variant auto-releases at transaction end.

```sql
-- Queue consumer: each worker claims distinct rows, no blocking
BEGIN;
SELECT id, payload FROM jobs
  WHERE status = 'queued'
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 10;
-- mark them running, process, commit
COMMIT;
```

### Deadlock avoidance
- Acquire locks in a **consistent order** across all code paths (e.g., always lock the lower `id` first).
- Keep transactions short; never do network calls or wait on user input inside a transaction.
- Postgres auto-detects deadlocks and aborts one victim with `40P01` — retry it.

### Keep transactions small
Long-running transactions hold back the snapshot horizon, blocking `VACUUM` and causing table/index bloat. Do heavy computation outside the transaction; open it only to write.

## Connection Pooling

- Postgres connections are heavyweight (each is a backend process). A single instance handles low hundreds, not thousands.
- Use a pooler: **PgBouncer** (transaction mode), Supavisor, or a provider pooler (Neon/Supabase, RDS Proxy).
- **Transaction-mode pooling breaks session features** — session-level prepared statements, `SET`, session advisory locks, `LISTEN/NOTIFY`. Prisma: append `?pgbouncer=true` to the pooled URL (disables prepared statements). SQLAlchemy: use `poolclass=NullPool` and disable statement caching (`connect_args={"prepare_threshold": None}` on psycopg 3) so the app doesn't fight the external pooler.
- Serverless: cap the app-side pool to a small number (1–5) per instance and rely on the external pooler.

## Migrations

Migrations must be **versioned, reviewed, forward-only in production, and backwards-compatible during deploy**.

### Safe migration principles
- **Expand/contract (multi-step) for breaking changes.** To rename a column: (1) add new column, (2) backfill + dual-write, (3) switch reads, (4) drop old column in a later deploy. Never rename or drop in the same release that ships the new code.
- **Adding a column with a constant `DEFAULT`** is metadata-only (fast) in Postgres 11+, including `NOT NULL`. But backfilling an existing huge table with a computed value should be batched.
- **`CREATE INDEX CONCURRENTLY`** avoids write locks; it can't run in a transaction, so mark the migration non-transactional.
- **Avoid long `ACCESS EXCLUSIVE` locks.** Set `lock_timeout` so a migration fails fast instead of queueing behind a long query and blocking all traffic behind it.
- Test the **down/rollback** path, or adopt forward-only migrations with feature flags.

```sql
-- Safe: fail fast rather than blocking the table indefinitely
SET lock_timeout = '3s';
ALTER TABLE orders ADD COLUMN coupon_code text;  -- nullable add is instant
```

### Batched backfill (avoid one giant UPDATE)
```sql
-- Run in a loop from app/migration code until 0 rows are affected
UPDATE orders o
SET total_cents = src.amount * 100
FROM (
  SELECT id FROM orders
  WHERE total_cents IS NULL
  LIMIT 5000
  FOR UPDATE SKIP LOCKED
) AS src
WHERE o.id = src.id;
```

## Prisma (TypeScript)

Prisma 6+ (2026) generates a fully typed client. Prefer it for TS backends wanting strong types with low boilerplate.

### Schema
```prisma
// schema.prisma
generator client {
  provider = "prisma-client"          // current ESM generator (replaces prisma-client-js)
  output   = "../src/generated/prisma" // required for prisma-client
}

datasource db {
  provider  = "postgresql"
  url       = env("DATABASE_URL")        // pooled (PgBouncer/Neon); add ?pgbouncer=true
  directUrl = env("DIRECT_DATABASE_URL") // direct, for migrations/introspection
}

model User {
  id        String   @id @default(uuid(7)) @db.Uuid  // UUIDv7, time-ordered
  email     String   @unique
  status    Status   @default(ACTIVE)
  orders    Order[]
  createdAt DateTime @default(now()) @map("created_at")
  updatedAt DateTime @updatedAt @map("updated_at")

  @@map("users")
}

model Order {
  id         String   @id @default(uuid(7)) @db.Uuid
  user       User     @relation(fields: [userId], references: [id], onDelete: Cascade)
  userId     String   @map("user_id") @db.Uuid
  totalCents BigInt   @map("total_cents")
  createdAt  DateTime @default(now()) @map("created_at")

  @@index([userId, createdAt(sort: Desc)])  // FK relations are NOT auto-indexed; declare it
  @@map("orders")
}

enum Status {
  ACTIVE
  SUSPENDED
  DELETED
}
```

### Queries — avoid N+1 with relation loading
```ts
import { PrismaClient } from "../src/generated/prisma";
const prisma = new PrismaClient();

// GOOD: eager-loads orders in a batched query. No N+1.
const users = await prisma.user.findMany({
  where: { status: "ACTIVE" },
  include: { orders: { orderBy: { createdAt: "desc" }, take: 5 } },
  take: 20,
});

// Keyset pagination (scales; OFFSET does not on large tables)
const page = await prisma.order.findMany({
  take: 20,
  ...(cursorId && { skip: 1, cursor: { id: cursorId } }),
  orderBy: { createdAt: "desc" },
});
```

### Transactions
```ts
// Interactive transaction: read-modify-write atomically.
// NOTE: Prisma does NOT auto-retry serialization failures — the CALLER must
// catch P2034 / 40001 and re-run this block in a retry loop.
await prisma.$transaction(
  async (tx) => {
    const acct = await tx.account.findUniqueOrThrow({ where: { id } });
    if (acct.balanceCents < amountCents) throw new Error("INSUFFICIENT_FUNDS");
    await tx.account.update({
      where: { id },
      data: { balanceCents: { decrement: amountCents } },
    });
  },
  { isolationLevel: "Serializable", timeout: 5000 }
);

// Batch (all-or-nothing, non-interactive)
await prisma.$transaction([
  prisma.order.create({ data: order }),
  prisma.user.update({ where: { id: userId }, data: { orderCount: { increment: 1 } } }),
]);
```

### Upsert and raw escape hatch
```ts
await prisma.user.upsert({
  where: { email },
  update: { status: "ACTIVE" },
  create: { email, status: "ACTIVE" },
});

// Raw SQL is parameterized (safe from injection) via tagged template
const rows = await prisma.$queryRaw<{ id: string }[]>`
  SELECT id FROM users WHERE lower(email) = ${email.toLowerCase()}
`;
```

- Instantiate `PrismaClient` **once** per process (singleton). In serverless/Next.js dev, cache it on `globalThis` to survive hot reloads.

## SQLAlchemy 2.0 (Python)

Use the **2.0 style**: typed `Mapped[]` declarative models and the `select()` API. Avoid the legacy `Query` object.

### Models
```python
from datetime import datetime
from enum import Enum
import uuid

from sqlalchemy import BigInteger, Enum as SAEnum, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Status(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(String, unique=True)
    # native_enum=False stores as text + CHECK, matching the SQL schema above and
    # avoiding painful ALTER TYPE migrations.
    status: Mapped[Status] = mapped_column(
        SAEnum(Status, native_enum=False, length=16), default=Status.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    orders: Mapped[list["Order"]] = relationship(back_populates="user")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("orders_user_recent_idx", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    total_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    user: Mapped["User"] = relationship(back_populates="orders")
```

### Engine, session, and pooling
```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

engine = create_engine(
    "postgresql+psycopg://user:pass@host/db",  # psycopg 3 driver
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,  # detect dropped connections before use
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)
```

### Queries — 2.0 select() + eager loading to kill N+1
```python
from sqlalchemy import select
from sqlalchemy.orm import selectinload

with SessionLocal() as session:
    # selectinload = second query with IN (...), best for collections. No N+1.
    stmt = (
        select(User)
        .where(User.status == Status.ACTIVE)
        .options(selectinload(User.orders))
        .limit(20)
    )
    users = session.scalars(stmt).all()

    # Keyset pagination
    stmt = (
        select(Order)
        .where(Order.created_at < last_seen_ts)
        .order_by(Order.created_at.desc())
        .limit(20)
    )
    page = session.scalars(stmt).all()
```
- `selectinload` for one-to-many collections; `joinedload` for many-to-one/one-to-one. Never lazy-load in a loop.

### Transactions and row locking
```python
# sessionmaker.begin() opens a session AND a transaction: commit on success,
# rollback on exception.
with SessionLocal.begin() as session:
    acct = session.scalars(
        select(Account).where(Account.id == acct_id).with_for_update()
    ).one()
    if acct.balance_cents < amount:
        raise ValueError("INSUFFICIENT_FUNDS")
    acct.balance_cents -= amount
    # commit happens automatically at block exit
```

### Upsert (Postgres ON CONFLICT)
```python
from sqlalchemy.dialects.postgresql import insert

stmt = (
    insert(User)
    .values(email=email, status=Status.ACTIVE)
    .on_conflict_do_update(
        index_elements=["email"],           # column name(s) of the unique index
        set_={"status": Status.ACTIVE},
    )
)
with SessionLocal.begin() as session:
    session.execute(stmt)
```

- Use **Alembic** for migrations. Autogenerate is a starting point, not a final answer — review every generated migration, and hand-write data migrations and concurrent index creation.

## ORM Pitfalls (both ecosystems)

- **N+1 queries** are the #1 ORM performance bug. Eager-load relations you'll access, and log emitted SQL in dev to catch them.
- **`OFFSET` pagination degrades linearly** — Postgres still scans and discards skipped rows. Use keyset (cursor) pagination for anything past a few pages.
- **Don't fetch whole rows to count** — use a `COUNT` query or a maintained counter.
- **Bulk operations:** ORMs do per-row round trips by default. Use bulk insert/update APIs (`createMany` / `executemany` via `session.execute(insert(...), [rows])`) or server-side `COPY` for large loads.
- **ORM-autogenerated migrations are not automatically safe** — they may take blocking locks. Review for `CONCURRENTLY`, batching, and `lock_timeout`.
- Drop to raw SQL for reporting/analytics — ORMs generate poor SQL for complex aggregations and window functions.

## Redis — When and How

### Use cases
- **Cache** (cache-aside): check Redis → on miss, read Postgres → write to Redis with a TTL.
- **Rate limiting:** `INCR` + `EXPIRE`, or a sliding window with sorted sets.
- **Sessions:** hash per session, TTL-based expiry.
- **Queues:** `LPUSH`/`BRPOP` lists, or Redis Streams with consumer groups for at-least-once delivery.
- **Distributed lock:** `SET key token NX PX 30000`; release only if the token matches (Lua compare-and-delete). Use Redlock only when you truly need cross-node mutual exclusion.
- **Leaderboards / rankings:** sorted sets (`ZADD`, `ZREVRANGE`).

### Cache-aside pattern (correct invalidation)
```python
import json

def get_user(session, redis, user_id: str) -> dict:
    key = f"user:{user_id}"
    if cached := redis.get(key):
        return json.loads(cached)
    user = session.get(User, user_id)
    data = {"id": str(user.id), "email": user.email}
    redis.set(key, json.dumps(data), ex=300)  # 5 min TTL
    return data

def update_user_email(session, redis, user_id: str, email: str):
    user = session.get(User, user_id)
    user.email = email
    session.commit()
    redis.delete(f"user:{user_id}")  # invalidate AFTER the DB commit succeeds
```

### Cache correctness rules
- **Always set a TTL.** A missing TTL turns a cache into an unbounded, stale store.
- **Invalidate (delete), don't update, on write.** Delete-after-commit avoids caching a value from a transaction that later rolls back. Updating the cache in place invites write-write races.
- **Guard against thundering herd/stampede** on hot-key expiry: add small TTL jitter, use a per-key lock so one request repopulates, or serve-stale-while-revalidating.
- **Never make Redis the source of truth.** Assume it can be flushed at any moment; the app must rebuild from Postgres.
- **Fixed-window rate limiting** allows up to 2x burst at the window boundary; use a sliding window if precision matters.

```lua
-- Fixed-window rate limit, atomic. Set the TTL only when the key is created,
-- otherwise every request would slide the expiry forward.
-- EVAL with KEYS[1]=ratelimit:{user}:{minute}, ARGV[1]=window_seconds
local n = redis.call('INCR', KEYS[1])
if n == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return n  -- reject if n > limit
```

### When NOT to use Redis
- As your primary database (durability/consistency guarantees are weaker; RAM is expensive).
- For data you always read fresh from Postgres anyway (adds a consistency surface for no gain).
- When Postgres, with proper indexes, already meets latency targets — an unnecessary cache is a bug source, not a win.

## Quick Checklist Before Shipping a Schema

- [ ] Every FK column has an index.
- [ ] All timestamps are `timestamptz`; money is integer cents or `numeric`.
- [ ] `NOT NULL` and `CHECK` constraints encode real invariants.
- [ ] Hot queries verified with `EXPLAIN (ANALYZE, BUFFERS)` (no unexpected `Seq Scan`).
- [ ] Read-modify-write paths use `FOR UPDATE` or optimistic version columns.
- [ ] Serialization/deadlock errors (`40001`, `40P01`, Prisma `P2034`) are retried.
- [ ] Migrations create indexes `CONCURRENTLY`, batch large backfills, and set `lock_timeout`.
- [ ] Breaking schema changes follow expand/contract across two deploys.
- [ ] Connection pool sized for the environment; serverless goes through a pooler.
- [ ] Every cache key has a TTL and a clear invalidation trigger.

KEYWORDS: postgres, schema design, normalization, denormalization, indexing, b-tree index, gin index, partial index, covering index, transactions, isolation levels, serializable, mvcc, migrations, prisma, sqlalchemy, orm, redis, caching, connection pooling, pgbouncer, jsonb, uuid, uuidv7, foreign keys, deadlocks, n+1 query, upsert, keyset pagination, advisory locks, materialized views, full text search, cache invalidation, optimistic locking, select for update
