<!-- keywords: fastapi, pydantic, pydantic v2, uvicorn, dependency injection, Depends, APIRouter, async endpoints, background tasks, lifespan, request validation, response_model, BaseModel, Annotated, Field, model_validator, field_validator, HTTPException, status codes, query parameters, path parameters, request body, form data, file upload, streaming response, middleware, CORS, pagination, pydantic-settings, SQLAlchemy async, asyncpg, OAuth2, JWT, bearer auth, exception handlers, testing, httpx AsyncClient, python api server, rest api, openapi, response validation, gunicorn, uvicorn workers, ORJSONResponse, email-validator, PyJWT -->

# FastAPI Backend Reference (2026)

Current stack: **FastAPI >= 0.115**, **Pydantic v2 (>= 2.9)**, **Starlette >= 0.40**, **Uvicorn >= 0.32**, **Python 3.11-3.13**. Everything below assumes Pydantic **v2** and the `Annotated` style for parameters and dependencies, which is the modern, non-deprecated form. Prefer `Annotated[...]` over default-value function args (`= Query(...)`, `= Depends(...)`) everywhere.

## Install & Project Layout

```bash
pip install "fastapi[standard]"   # pulls uvicorn, httpx, jinja2, python-multipart, email-validator
pip install pydantic-settings "sqlalchemy[asyncio]" asyncpg  # common extras
```

- `fastapi[standard]` provides the `fastapi` CLI (`fastapi dev`, `fastapi run`) and installs `email-validator` (required for `EmailStr`). If you install the base `fastapi` package instead, add `pip install "pydantic[email]"` before using `EmailStr`.
- Recommended layout for a real service:

```
app/
  __init__.py
  main.py            # app factory + lifespan + router includes
  config.py          # Settings (pydantic-settings)
  deps.py            # shared dependencies (db session, current user)
  db.py              # engine / sessionmaker
  routers/
    items.py
    users.py
  models.py          # SQLAlchemy ORM models
  schemas.py         # Pydantic request/response models
```

## Minimal App & Running

```python
# app/main.py
from fastapi import FastAPI

app = FastAPI(title="My API", version="1.0.0")

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
```

```bash
fastapi dev app/main.py     # dev: reload on, one worker
fastapi run app/main.py     # prod-ish: reload off
# equivalent explicit uvicorn:
uvicorn app.main:app --host 0.0.0.0 --port 8000
uvicorn app.main:app --reload   # dev only
```

- **Never** run `--reload` in production. `--reload` forces a single worker.
- Interactive docs at `/docs` (Swagger UI) and `/redoc`. OpenAPI JSON at `/openapi.json`.

## Pydantic v2 Models (schemas)

```python
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, EmailStr, Field, ConfigDict, field_validator, model_validator

class Role(str, Enum):
    admin = "admin"
    user = "user"

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    age: int | None = Field(default=None, ge=0, le=150)
    role: Role = Role.user

    @field_validator("password")
    @classmethod
    def no_spaces(cls, v: str) -> str:
        if " " in v:
            raise ValueError("password must not contain spaces")
        return v

class UserOut(BaseModel):
    # v2: read attributes off ORM objects (replaces orm_mode)
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: EmailStr
    role: Role
    created_at: datetime
```

**Pydantic v2 essentials (do not use v1 idioms):**

- `model_config = ConfigDict(...)` instead of an inner `class Config`.
- `from_attributes=True` replaces `orm_mode`. Build with `UserOut.model_validate(orm_obj)`.
- `field_validator` (per-field) and `model_validator` (whole model) replace `validator`/`root_validator`. Field validators need `@classmethod`.
- Serialize with `model_dump()` / `model_dump_json()`; parse with `model_validate()` / `model_validate_json()`. (`.dict()`/`.json()`/`.parse_obj()` are deprecated.)
- `Field(default=...)` for defaults; for mutable defaults use `Field(default_factory=list)`.
- `model_config = ConfigDict(extra="forbid")` rejects unknown keys (great for strict request bodies).

**Cross-field validation** with `model_validator`:

```python
from typing import Self

class PasswordReset(BaseModel):
    password: str = Field(min_length=8)
    password_confirm: str

    @model_validator(mode="after")
    def passwords_match(self) -> Self:
        if self.password != self.password_confirm:
            raise ValueError("passwords do not match")
        return self
```

- `mode="after"` runs post-parsing on the validated model instance (return `self`).
- `mode="before"` runs on the raw input (usually a dict); use it for normalization/aliasing.

**Aliases & camelCase APIs:**

```python
from pydantic import ConfigDict
from pydantic.alias_generators import to_camel

class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    first_name: str   # accepts "firstName" in, emits "firstName" out (with by_alias=True)
```

## Path, Query, and Body Parameters

Use `Annotated` + `Path`/`Query`/`Body` for metadata and validation.

```python
from typing import Annotated
from fastapi import FastAPI, Path, Query

app = FastAPI()

@app.get("/items/{item_id}")
async def read_item(
    item_id: Annotated[int, Path(ge=1)],
    q: Annotated[str | None, Query(max_length=50)] = None,
    tags: Annotated[list[str], Query()] = [],       # ?tags=a&tags=b
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return {"item_id": item_id, "q": q, "tags": tags, "limit": limit}
```

- Path params come from the URL template; declared function args not in the path/body become **query** params.
- A `pydantic.BaseModel` typed arg becomes the **request body** (JSON) automatically.
- Multiple body models -> FastAPI nests them under keys named after the params.
- Scalar as body: `x: Annotated[int, Body()]`.
- Validate query params as a group with a model: `filters: Annotated[FilterModel, Query()]`.

```python
from pydantic import BaseModel

class Item(BaseModel):
    name: str
    price: float
    is_offer: bool = False

@app.post("/items", status_code=201)
async def create_item(item: Item) -> Item:      # return type IS the response_model
    return item
```

- **Prefer a return type annotation** (`-> Item`) over `response_model=`; it both validates the response and types your code. Use `response_model=` only when the return type must differ from the declared model (e.g. returning an ORM object filtered to `UserOut`).
- `response_model_exclude_none=True`, `response_model_exclude_unset=True` trim output.

## Routers (APIRouter)

Split endpoints into modules and mount them with prefixes, tags, and shared dependencies.

```python
# app/routers/items.py
from fastapi import APIRouter, HTTPException, status

router = APIRouter(prefix="/items", tags=["items"])

@router.get("/{item_id}")
async def get_item(item_id: int):
    if item_id > 1000:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Item not found")
    return {"item_id": item_id}
```

```python
# app/main.py
from fastapi import FastAPI
from app.routers import items, users

app = FastAPI()
app.include_router(items.router)
app.include_router(users.router, prefix="/v1")   # extra prefix stacks: /v1/users/...
```

- Router-level knobs: `APIRouter(dependencies=[Depends(verify_token)], responses={404: {"description": "Not found"}})`.
- Per-route metadata: `@router.get(..., summary=..., description=..., deprecated=True, response_description=...)`.

## Dependency Injection (Depends)

DI is FastAPI's core pattern: shared logic, resource acquisition/teardown, and auth. Declare with `Annotated[T, Depends(func)]`.

```python
from typing import Annotated
from fastapi import Depends, Query

async def pagination(
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, int]:
    return {"skip": skip, "limit": limit}

Pagination = Annotated[dict[str, int], Depends(pagination)]  # reusable alias

@app.get("/items")
async def list_items(page: Pagination):
    return page
```

**Dependencies with yield** (setup/teardown, the correct way to manage DB sessions):

```python
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session          # code after yield runs on the way out (teardown)

DbSession = Annotated[AsyncSession, Depends(get_db)]
```

- Code after `yield` runs *after* the response is sent. If the request raised, the exception is re-raised inside the dependency, so wrap commits/rollbacks in try/except/finally as needed.
- Dependencies are **cached per-request** by default: the same dependency called in multiple places runs once. Disable with `Depends(func, use_cache=False)`.
- **Sub-dependencies**: a dependency can itself declare `Depends(...)`; FastAPI resolves the whole graph.
- **Class dependencies**: any callable works, including a class (`Depends(MyDep)` injects `MyDep(...)`), or an instance with `__call__` for parametrized deps.
- **Global dependencies**: `FastAPI(dependencies=[Depends(verify_api_key)])`.
- **Side-effect-only deps** (no return needed): put them in the decorator's `dependencies=[...]` list, e.g. `@app.get("/admin", dependencies=[Depends(require_admin)])`.

## Async vs Sync Endpoints

- Use `async def` and **await** async libraries (asyncpg, httpx, aioboto3, async SQLAlchemy). Never call blocking I/O inside `async def` without offloading it.
- Use plain `def` for endpoints that call **blocking** libraries (sync DB drivers, `requests`, heavy CPU). FastAPI runs `def` endpoints in a threadpool automatically, keeping the event loop free.
- Offload occasional blocking calls from async code:

```python
from starlette.concurrency import run_in_threadpool
result = await run_in_threadpool(blocking_fn, arg1, arg2)
```

- For CPU-bound work, use a `ProcessPoolExecutor` or an external worker (Celery, ARQ, Dramatiq) - not the threadpool.
- Rule of thumb: **one wrong `time.sleep()` / blocking `requests.get()` inside `async def` stalls every concurrent request**.

## Background Tasks

For lightweight fire-and-forget work that runs **after** the response is returned, in the same process.

```python
from fastapi import BackgroundTasks

def write_log(message: str) -> None:
    with open("log.txt", "a") as f:
        f.write(message + "\n")

@app.post("/notify")
async def notify(email: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(write_log, f"queued {email}")
    return {"status": "accepted"}   # response returns immediately; task runs after
```

- Tasks run in the same process/event loop. Async task functions are awaited; sync ones run in the threadpool.
- Exceptions in background tasks do **not** affect the already-sent response but will surface in logs. Wrap in try/except.
- Background tasks are **not durable** - lost on crash/restart. For retries, durability, or scheduling use a real queue (ARQ, Celery, Dramatiq, or a DB-backed outbox).

## Error Handling

```python
from fastapi import HTTPException, status

raise HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Not enough permissions",
    headers={"WWW-Authenticate": "Bearer"},
)
```

**Custom exceptions + handlers** keep domain logic clean:

```python
from fastapi import Request
from fastapi.responses import JSONResponse

class ItemNotFound(Exception):
    def __init__(self, item_id: int):
        self.item_id = item_id

@app.exception_handler(ItemNotFound)
async def item_not_found_handler(request: Request, exc: ItemNotFound):
    return JSONResponse(status_code=404, content={"detail": f"Item {exc.item_id} not found"})
```

**Override validation errors** (422) for a consistent error envelope:

```python
from fastapi.exceptions import RequestValidationError

@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"errors": exc.errors()})
```

- Use `4xx` for client errors, `5xx` for server errors. Don't leak internal exception text; log it, return a generic message.
- Import `status` from `fastapi` for named codes (`status.HTTP_201_CREATED`).

## Middleware & CORS

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.example.com"],   # never "*" with allow_credentials=True
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Custom ASGI-style middleware (runs per request, wraps the call):

```python
import time
from fastapi import Request

@app.middleware("http")
async def add_process_time(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time"] = f"{time.perf_counter() - start:.4f}"
    return response
```

- Middleware runs in reverse order of registration for the request phase. Keep it fast and non-blocking.
- Prefer dependencies over middleware when you only need per-route logic or return values.

## Lifespan (startup/shutdown)

Use the **lifespan context manager** (the `@app.on_event` decorators are deprecated).

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup: create pools, warm caches
    app.state.pool = await create_pool()
    yield
    # shutdown: close resources
    await app.state.pool.close()

app = FastAPI(lifespan=lifespan)
```

- Access shared resources via `request.app.state.pool` in endpoints/deps.
- Good for DB engines, HTTP clients (`httpx.AsyncClient`), Redis pools, ML models.

## Config with pydantic-settings

```python
# app/config.py
from functools import lru_cache
from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="APP_", extra="ignore")
    database_url: PostgresDsn
    jwt_secret: str
    debug: bool = False

@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from env, not literal args

# In routes/deps:
from typing import Annotated
from fastapi import Depends
SettingsDep = Annotated[Settings, Depends(get_settings)]
```

- `pydantic-settings` is a **separate package** in v2 (no longer part of pydantic core).
- `@lru_cache` makes settings a singleton and keeps them injectable/overridable in tests.
- Reads from env vars (respecting `env_prefix`) and `.env`. Secrets should come from the environment, not committed files.

## Async Database (SQLAlchemy 2.0 + asyncpg)

```python
# app/db.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

engine = create_async_engine("postgresql+asyncpg://user:pw@localhost/db", pool_pre_ping=True)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)
```

```python
# router usage
from sqlalchemy import select

@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(payload: UserCreate, db: DbSession):
    user = User(email=payload.email, hashed_password=hash_pw(payload.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user   # UserOut(from_attributes=True) serializes the ORM object

@router.get("/users", response_model=list[UserOut])
async def list_users(db: DbSession, page: Pagination):
    rows = await db.execute(select(User).offset(page["skip"]).limit(page["limit"]))
    return rows.scalars().all()
```

- `expire_on_commit=False` avoids lazy-load-after-commit errors during serialization.
- Never share a session across requests; acquire per-request via the `get_db` yield dependency.

## Auth: OAuth2 Password + JWT (Bearer)

```python
from datetime import datetime, timedelta, timezone
from typing import Annotated
import jwt                       # PyJWT
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
ALGORITHM = "HS256"

def create_access_token(sub: str, secret: str, minutes: int = 30) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return jwt.encode({"sub": sub, "exp": exp}, secret, algorithm=ALGORITHM)

@app.post("/token")
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], s: SettingsDep):
    user = await authenticate(form.username, form.password)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad credentials",
                            headers={"WWW-Authenticate": "Bearer"})
    return {"access_token": create_access_token(user.email, s.jwt_secret), "token_type": "bearer"}

async def current_user(token: Annotated[str, Depends(oauth2_scheme)], s: SettingsDep):
    creds_exc = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token",
                             headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[ALGORITHM])
    except jwt.InvalidTokenError:   # base class for expired/invalid/malformed tokens
        raise creds_exc
    email = payload.get("sub")
    if email is None:
        raise creds_exc
    return await get_user_by_email(email)

CurrentUser = Annotated[User, Depends(current_user)]

@app.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    return user
```

- Hash passwords with `bcrypt`/`argon2` (via `pwdlib` or `passlib`) - never store plaintext. `pwdlib` is the actively maintained choice for new projects.
- Always set `exp`; use timezone-aware UTC (`datetime.now(timezone.utc)`). `jwt.decode` validates `exp` and raises `ExpiredSignatureError` (a subclass of `InvalidTokenError`).
- `OAuth2PasswordRequestForm` requires `python-multipart` (included in `fastapi[standard]`).

## Forms, Files, Uploads

```python
from typing import Annotated
from fastapi import Form, File, UploadFile

@app.post("/upload")
async def upload(
    name: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
):
    data = await file.read()          # UploadFile is async; spools large files to disk
    return {"name": name, "filename": file.filename, "size": len(data)}
```

- Use `UploadFile` (not `bytes`) for large files - it streams and exposes `.read()`, `.seek()`, `.close()`.
- Form + JSON body can't coexist in one request; a request is either JSON or form-encoded.

## Responses: status codes, custom & streaming

```python
from fastapi import Response
from fastapi.responses import ORJSONResponse, StreamingResponse

app = FastAPI(default_response_class=ORJSONResponse)   # faster JSON globally

@app.get("/download")
async def download():
    async def gen():
        async for chunk in produce_chunks():   # async generator
            yield chunk
    return StreamingResponse(gen(), media_type="application/octet-stream")
```

- `ORJSONResponse` (needs `orjson`) is the fastest JSON encoder; set app-wide as above.
- `status_code=` on the decorator sets the default success code; `HTTPException` overrides on error.
- For no content, return `Response(status_code=204)`.
- Directly returning a `Response` subclass bypasses `response_model` validation.

## Pagination Pattern

```python
from pydantic import BaseModel

class Page[T](BaseModel):        # PEP 695 generic (Python 3.12+)
    items: list[T]
    total: int
    skip: int
    limit: int
```

- Return `Page[UserOut]` as the response model for a typed, self-documenting envelope.
- On < 3.12 use `typing.Generic` + `TypeVar`.

## Testing

Use httpx `ASGITransport` with `AsyncClient` for async tests (recommended), or `TestClient` (sync, wraps httpx) for quick checks.

```python
# pip install pytest httpx anyio
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app

@pytest.fixture
def anyio_backend():
    return "asyncio"

@pytest.mark.anyio
async def test_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

**Override dependencies in tests** (the killer feature - swap DB, auth, settings):

```python
def fake_db():
    yield test_session

app.dependency_overrides[get_db] = fake_db
# ... run tests ...
app.dependency_overrides.clear()
```

- `ASGITransport(app=app)` does **not** trigger lifespan; run startup/shutdown yourself (e.g. `async with LifespanManager(app)` from `asgi-lifespan`) if a test needs pooled resources.
- Sync alternative: `from fastapi.testclient import TestClient; client = TestClient(app)` then `client.get(...)`. `TestClient` **does** run lifespan when used as a context manager: `with TestClient(app) as client:`.

## Production Deployment

```bash
# Uvicorn with multiple workers (each worker = 1 process)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

- Workers: start with `(2 * CPU cores) + 1` as a baseline for I/O-bound apps; measure and tune. All workers are async - concurrency within a worker comes from the event loop, not threads.
- Run behind a reverse proxy (nginx / Traefik / cloud LB) for TLS, timeouts, and buffering. Pass `--proxy-headers --forwarded-allow-ips="*"` (or a specific range) so client IPs and scheme are correct.
- The legacy `gunicorn -k uvicorn.workers.UvicornWorker` pattern still works but is no longer necessary - modern `uvicorn --workers` manages workers directly. Prefer container-orchestrated replicas (K8s, ECS) over many in-process workers when possible: one process per container scales/observes cleaner.
- Set explicit timeouts (`--timeout-keep-alive`), health/readiness probes hitting `/health`, and structured JSON logging.
- Do not enable `--reload` or `debug=True` in production.

## Correctness Checklist (common mistakes to avoid)

- Blocking I/O inside `async def` -> stalls the loop. Use async clients or `run_in_threadpool`, or make the endpoint plain `def`.
- Pydantic v1 idioms (`orm_mode`, `.dict()`, `@validator`, inner `Config`) -> use v2 equivalents.
- `allow_origins=["*"]` together with `allow_credentials=True` -> browsers reject it; specify exact origins.
- Returning ORM objects without `from_attributes=True` on the response model -> serialization errors.
- Using `EmailStr` without `email-validator` installed -> import error; use `fastapi[standard]` or `pydantic[email]`.
- Relying on background tasks for critical/durable work -> use a real queue.
- Mutable default args in Pydantic (`= []`) -> use `Field(default_factory=list)`.
- Forgetting `expire_on_commit=False` with async SQLAlchemy -> lazy-load errors during response serialization.
- Assuming `ASGITransport` runs lifespan in tests -> it doesn't; use `TestClient` as a context manager or `asgi-lifespan`.
- Using `= Depends()`/`= Query()` default-arg style -> prefer `Annotated[...]`; it's the current idiom and avoids default-value pitfalls.
