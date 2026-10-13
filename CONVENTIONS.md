# Hangar API — conventions

These rules apply to everyone working in this repo, above all the five parallel feature
slices (H1–H5). If something here conflicts with your habits, this file wins. If you need
something it does not allow (a core change, a new dependency), **do not work around it**.
Write a *shared-file request* in your final report instead: what you need, why, and a
proposed diff.

**Spec precedence.** `_spec/hangar/SPEC_RESOLUTIONS.md` resolves 56 cross-document
inconsistencies. It **overrides** `SPEC.md`, `API.md` and `SCREENS.md` wherever they
conflict. Before you start, read the rows whose *Owner* column names your slice. This file
defines two things the spec leaves open:

- **The physical layout.** SPEC §2.2 says `hangar/slices/...`; here it is `app/modules/...`.
- **The concrete APIs** of the core contracts in SPEC §2.4.

## 1. Golden rules

1. **Stay in your package.** Slice `hN` edits only:
   - `app/modules/hN_*/**`;
   - its own migration files `alembic/versions/hN_*.py`;
   - the regenerated `openapi.json` (§3.12).

   §4 has the full list of files you must never touch.
2. **Register explicitly.** Everything a slice contributes is registered in
   `register(reg: SliceRegistry)` in the package `__init__.py`. Core never imports a slice
   module by name, and you never edit `app/main.py`.
3. **Change core rows only through core services.**

   | What changes | Use |
   |---|---|
   | SR or lease status | `TransitionService` |
   | Asset status or location | `AssetService` |
   | `appointment_*` | `set_appointment` / `clear_appointment` |
   | Region columns | `apply_region` / `set_region_override` |
   | Lease links | `link_lease_return(s)` |
   | VIP flags | `recompute_vip` |

   Never write raw `UPDATE`s on these columns.
4. **No cross-slice reads.** Read core tables and your own tables only (SPEC §2.5). React to
   other slices through core events and denormalized core columns.
5. **No new dependencies** (§8). **Fictional data only:**
   - companies: Acme IT Services, Globex Fleet AG, Initech Logistics;
   - hubs: Riverton, Hillford, Eastgate;
   - e-mail addresses: `example.com` only.

   Run `python -I D:/code/atrium-hangar/_spec/check_blocklist.py <your paths>`; it must print `HARD hits: 0`.
6. **Green before done.** All of these must pass:
   - `uv run ruff check`
   - `uv run ruff format --check`
   - `uv run pyright`
   - `uv run pytest`: the whole suite, not only your tests (§3.11).

## 2. Repository layout

```
app/
  main.py               create_app(): core routers + slice routers under /api/v1
  cli.py                `hangar` CLI (serve, db upgrade|reset, seed, demo-reset, worker, openapi)
  core/                 foundation — read freely, never edit
    config.py           Settings (env vars, SPEC §2.7), get_settings()
    db.py               Base, IdMixin, TimestampMixin, VersionMixin, str_enum, SessionDep,
                        open_session(), commit(), after_commit()
    enums.py            ALL enums incl. slice-owned ones + ENUM_META (labels/tones) for /meta/enums
    permissions.py      permission keys + role matrix (SPEC §3.2)
    auth.py             JWT, argon2, CurrentUser, require(...), can(), ensure()
    statemachine.py     Transition, TransitionMap, Guard/GuardFailure, Authorizer, registries
    transitions/        sr_map.py, lease_map.py, asset_map.py, guards.py (core guards), service.py
    actions.py          ActionRegistry, ActionBody, BulkBody
    assets.py           AssetService (sole writer of asset status/location + asset_movements)
    services.py         set/clear_appointment, link/unlink_lease_return(s), recompute_vip
    region_resolver.py  resolve_region, apply_region, set_region_override, apply_lease_campus
    stock_math.py       order_qty/below_min/above_max, recalculate()
    events.py           domain events + two-phase event bus
    notifications/      template registry, renderer (base layout), notify(), notifications.send
    files.py            FileStore, store_file, signed_url, read_upload
    jobs.py             background_jobs helpers: enqueue, tracked, job_progress, finish_job
    gateways/           protocols.py, fakes/ (bound by default), real/ (skeletons), mailers.py
    read_models/        builders for SR / lease / asset / contact / master-data DTOs
    search.py           global search + register_search_provider
    seed/               seed framework + core seed story (SPEC §11)
    demo.py             demo reset, seed.json export, table_exporter
    slices.py           SliceRegistry + discovery
    models/             core tables (SPEC §5.1)
    api/                core routers (auth, me, meta, reference, search, files, jobs,
                        saved-views, demo, generic action endpoints)
  jobs/                 procrastinate App (job_app), register_task, worker, schema bootstrap
  testing/              factories + test template (import them from slice tests)
  modules/
    h1_requests/ h2_imports/ h3_staging/ h4_field/ h5_stock/
alembic/versions/       0001_core.py + one branch per slice (hN_0001_*.py ...)
tests/                  core tests
conftest.py             shared fixtures for tests/ and app/modules/*/tests/
var/                    runtime data (files store, seed exports) — git-ignored
```

## 3. How to build a slice

### 3.1 Package files

```
app/modules/h3_staging/
  __init__.py      register(reg) — the ONLY entry point core calls
  README.md        what the slice owns (keep it current)
  models.py        slice tables (auto-imported for create_all/Alembic before register())
  schemas.py       ApiModel / ApiInput DTOs
  api.py           APIRouter(s)  (split into api/ package if large)
  services.py      business logic: takes (session, ..., actor), never commits
  actions.py       action payload models + handlers (optional)
  guards.py        guard / authorizer functions (optional)
  subscribers.py   event handlers (optional)
  tasks.py         procrastinate task functions (optional)
  search.py        search provider (optional)
  seed.py          seeder + exporter (optional)
  templates/       mail bodies <template_id>.html.j2 / .txt.j2
  tests/           test_*.py (+ optional conftest.py with slice fixtures)
```

Only two files have a fixed meaning: `__init__.py`, which defines `register`, and
`models.py`, which is auto-imported. The rest of the layout is a recommendation.

Import your submodules **inside** `register()`. That keeps importing `models.py` (for
Alembic or `create_all`) light and free of import cycles.

### 3.2 `register()` — one example per registry

```python
"""H3 Staging & Fulfilment slice (see README.md)."""

from __future__ import annotations

from pathlib import Path

from app.core.slices import SliceRegistry

TEMPLATES = Path(__file__).parent / "templates"


def register(reg: SliceRegistry) -> None:
    from app.core.demo import table_exporter
    from app.core.events import Transitioned
    from app.core.notifications import NotificationTemplate

    from . import actions, api, guards, search, seed, subscribers, tasks

    # HTTP — mounted under /api/v1; give each router its own prefix + tags
    reg.include_router(api.router)

    # Actions on the generic endpoints POST /<resource>/{id}/actions/{name} (+ /bulk/{name}).
    # Pure transition: permission, bulk flag and requiresComment come from the map.
    reg.register_action(resource="service_requests", name="claim")
    # With payload + handler (the handler does the work and calls TransitionService itself).
    reg.register_action(
        resource="service_requests",
        name="close_without_label",
        payload_model=actions.CloseWithoutLabelBody,
        handler=actions.close_without_label,
    )

    # Guards on core transitions: fn(session, entity, actor, payload) -> GuardFailure | None
    reg.register_guard(
        "service_request", "finish_personalization", guards.places_set, code="places_missing"
    )
    # Authorizer (SPEC_RESOLUTIONS #2): fn(session, entity, actor) -> bool.
    # False hides the action; calling it anyway → 403 <code>.
    reg.register_authorizer(
        "service_requests",
        ["complete_deployment", "reschedule_on_site", "cancel_on_site"],
        guards.owns_tour_stop,
        code="not_your_tour",
        message="This stop belongs to another technician's tour.",
    )

    # Events: in_transaction handlers get (session, event); after_commit handlers get (event)
    reg.subscribe(Transitioned, subscribers.on_transition, phase="in_transaction")
    reg.subscribe(Transitioned, subscribers.after_transition)  # phase="after_commit"

    # Mail template (body fragment; core wraps it in the base layout)
    reg.register_template(
        NotificationTemplate(
            id="sr.shipping_confirmation",
            owner="h3",
            name="Shipping confirmation",
            subject="Your new hardware is on its way ({{ sr_number }})",
            html=TEMPLATES / "sr.shipping_confirmation.html.j2",
            text=TEMPLATES / "sr.shipping_confirmation.txt.j2",
            description="Sent to the contact when a parcel shipment is created.",
            trigger="ship",
            recipients="Contact (cc team mailbox)",
            entity_type="service_request",
            sample_context={
                "sr_number": "SR0012345",
                "tracking_url": "https://carrier.example.com/t/1",
            },
        )
    )

    # Jobs (name = "<area>.<verb>", queue in default|imports|mail|sync; cron is UTC)
    reg.register_task(tasks.process, name="imports.process", queue="imports")
    reg.register_task(
        tasks.recalculate, name="stock.recalculate", lock="stock.recalculate", periodic="5 * * * *"
    )

    # Global search (⌘K)
    reg.register_search_provider(search.provider)

    # Seed: order > 0 (core is 0); exporter adds tables to seed.json; demo reset truncates
    reg.register_seeder(30, seed.seed_staging)
    reg.register_seed_exporter(table_exporter("shipments"))
    reg.register_demo_truncate(["shipments"])
```

Rules:
- Core calls `register()` exactly once per process: API, worker, CLI and tests. It must not
  touch the database.
- Register **every user action your slice owns** (last column of the SPEC §2.5 table).
  An unregistered action is never offered in `availableActions` and cannot be dispatched.
  `GET /api/v1/meta/transitions` shows the owner of each action.
- `internal` transitions cannot be registered as actions. Examples: `appointment_synced`,
  `import_create`, asset `receive`. Call `TransitionService` or `AssetService` from your
  code with the right trigger instead.
- Asset `deactivate` is never on the generic endpoint (#24).
- Some actions run through their own endpoint; do not register those. For example, `ship`
  runs through `POST /service-requests/{id}/shipments`.

### 3.3 Models and migrations

```python
# app/modules/h4_field/models.py
from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin, VersionMixin, str_enum
from app.core.enums import TourStatus  # slice enums live in core/enums.py (single source)


class Tour(IdMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "tours"

    status: Mapped[TourStatus] = mapped_column(str_enum(TourStatus))
    technician_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    published_at: Mapped[datetime | None]
```

- Inherit `Base` and add the mixins you need:

  | Mixin | Adds |
  |---|---|
  | `IdMixin` | int identity `id` |
  | `BigIdMixin` | bigint identity `id`, for high-volume tables |
  | `TimestampMixin` | `created_at` / `updated_at` |
  | `VersionMixin` | optimistic-locking `version` |

  With `VersionMixin`, clients send `version` in PATCH and action bodies. A mismatch returns
  `409 version_conflict`.
- Enums: use `str_enum(MyEnum)`. It creates VARCHAR(32) plus a CHECK named
  `ck_<table>_<column>`.
- All enums, including slice-owned ones, already live in `app/core/enums.py`. If one is
  missing or wrong, file a request.
- `Mapped[datetime]` maps to `timestamptz` (aware UTC); `dict`/`list` map to JSONB.
- Many-to-one relationships: use `lazy="selectin"` or `lazy="raise"`. An implicit lazy load
  raises `MissingGreenlet` in async code (§7).
- Slice tables may have FKs **into** core tables. Core never has FKs into slice tables.

**Migration branch recipe** (SPEC §2.6):
- Your branch starts at `0001_core` and has the branch label `hN`.
- The first revision ids are fixed:

  | Slice | First revision id |
  |---|---|
  | H1 | `h1_0001_requests` |
  | H2 | `h2_0001_imports` |
  | H3 | `h3_0001_staging` |
  | H4 | `h4_0001_field` |
  | H5 | `h5_0001_stock` |

- Further revisions chain inside your branch.

```powershell
# 1. an EMPTY scratch database (not your dev DB, which was built with create_all)
#    e.g. create hangar_h3_alembic in the shared Postgres, then:
$env:DATABASE_URL = "postgresql+psycopg://dev:dev@localhost:5432/hangar_h3_alembic"
uv run hangar db upgrade                      # applies 0001_core (+ your earlier revisions)

# 2. first revision of the branch
uv run alembic revision --autogenerate --head 0001_core --branch-label h3 `
    --rev-id h3_0001_staging -m "h3 staging tables"
#    later revisions:  --head h3@head --rev-id h3_0002_<what>   (no --branch-label)

# 3. review alembic/versions/h3_0001_staging.py, then prove it round-trips
uv run hangar db upgrade
uv run alembic check                          # must print "No new upgrade operations detected."
Remove-Item Env:DATABASE_URL
```

This recipe was dry-run against a throwaway model. It produced a correct branch revision,
`alembic heads` showed `h1_0001_requests (h1) (head)`, and `alembic check` was clean.

- `env.py` renders the enum CHECKs with convention names automatically. A post-write hook
  runs ruff on the generated file.
- Autogenerate skips **trigram/expression indexes** (names ending `_trgm`), so write them by
  hand:
  `op.execute("CREATE INDEX ix_<t>_<c>_trgm ON <t> USING gin (<c> gin_trgm_ops)")`.
  `0001_core` already creates the `pg_trgm` extension.
- Reference rows your slice needs in every environment go into the migration
  (`op.bulk_insert`). Demo data goes into your seeder.
- `hangar db upgrade` runs `alembic upgrade heads`. It therefore works both before and after
  QA adds `0002_merge_slices`.
- Never edit `0001_core.py` or another slice's revisions.
- Day-to-day development and tests use `create_all`: `uv run hangar db reset --yes --seed`
  for development, and the test DB is rebuilt every session. The migration only has to be
  correct; you do not run it day to day.

### 3.4 Endpoints

```python
# app/modules/h5_stock/api.py
# No `from __future__ import annotations` in router modules that build dependencies in
# closures: FastAPI resolves those annotations at runtime.
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.auth import require
from app.core.db import SessionDep
from app.core.errors import problem_responses
from app.core.models import User
from app.core.pagination import OrderBy, Page, PageParamsDep, fetch_page, sort_param

from .models import Inventory
from .schemas import InventoryOut, build_inventory

router = APIRouter(prefix="/inventories", tags=["inventories"])
SORT = sort_param(
    {"startedAt": Inventory.started_at}, default="-startedAt", tiebreaker=Inventory.id
)


@router.get("", response_model=Page[InventoryOut], responses=problem_responses(401, 403, 422))
async def list_inventories(
    session: SessionDep,
    _user: Annotated[User, Depends(require("inventory.read"))],
    paging: PageParamsDep,
    order: Annotated[list[OrderBy], Depends(SORT)],
) -> Page[InventoryOut]:
    rows, meta = await fetch_page(session, select(Inventory).order_by(*order), paging)
    return Page[InventoryOut](data=[build_inventory(r) for r in rows], meta=meta)
```

- **Contract.** Paths, params and payloads follow `API.md` exactly, as amended by
  SPEC_RESOLUTIONS. The frontend is built against them.
- **JSON shape.** JSON is camelCase: responses use `ApiModel`, request bodies use `ApiInput`
  (unknown fields → 422). Python attributes stay snake_case.
- **Auth** in routes:
  - `CurrentUser` for any signed-in user;
  - `Depends(require("perm"))` for one permission;
  - `require("a", "b", any_of=True)` for any of several.

  Inside services use `ensure(user, "perm")` or `can(...)`. Enforce row-level rules (own
  tour, own inventory) on the server.
- **Lists:**
  - Paged: `Page[T]` (`{data, meta}`) with `PageParamsDep` and `sort_param`.
  - Not paged: `ListResponse[T]` (`{data}`).
  - Multi-value filters: `csv_enum("status", MyEnum)` and `csv_ints("siteId", allow=("none",))`.
    Both accept `?status=a,b` **and** repeated keys (#39).
  - Ranges: `<field>From`/`<field>To` via `date_range_clause`.
  - Text search: `q` via `search_clause`.
- **SR, lease and asset DTOs: always use the core builders.** They compute
  `availableActions`/`blockedActions`, attention flags and labels consistently (#11).
  - SRs: `build_sr_list_items`, `build_sr_detail`
  - Leases: `build_lease_list_items`, `build_lease_detail`
  - Assets: `build_asset_list_items`, `build_asset_detail`
  - Contacts: `build_contact_summaries`
  - References: the `refs` helpers

  Extend by composition (`class MyRow(ApiModel): sr: ServiceRequestListItem`) rather than
  re-implementing.
- **Writes.** Services mutate; the endpoint calls `await commit(session)` once, at the end.
  One request is one transaction (§7 explains why it must be `commit`).
- **Errors.** Raise one of:
  - `NotFoundError`, `ForbiddenError`, `BadRequestError`;
  - `ConflictError(detail, code="...")`;
  - `ValidationProblem({"field": ["message"]})`;
  - `ApiError(detail, status=..., code="snake_case_code")`.

  Never return ad-hoc error bodies. Declare the statuses an endpoint can return with
  `responses=problem_responses(401, 403, 404, 409, 422)`.
- **Long-running work** answers `202 JobAccepted` (§3.7).

### 3.5 Actions, guards, authorizers

- `TransitionService.apply(session, entity, action, *, actor, trigger, payload=None, comment=None)`
  checks, in this order:
  1. the action exists;
  2. its `from` matches the current status; otherwise `409 transition_not_allowed` with
     `allowedActions`;
  3. the permission, for the `user` trigger; otherwise 403;
  4. the authorizers; otherwise `403 <code>`;
  5. the guards; otherwise `409 guard_failed` with `guard: {code, message}`.

  It then sets the status, `status_changed_at` and the chain-mail dates (#9), writes a
  `status_transitions` row and emits `Transitioned`. It never commits.
- Handlers have the signature `async def handler(session, entity, actor, payload) -> None`.
  Payload models extend `ActionBody` (flat fields + `comment` + `version`).
- Before calling your handler, the generic endpoint has already checked `version` and the
  required comment (422, #34). Afterwards it returns the refreshed detail DTO.
- Bulk (`POST /<resource>/bulk/{action}`, up to 200 ids) runs each id in its own savepoint.
  It returns `BulkResult {action, succeeded, failed[{id, code, message}]}` with HTTP 200.
- Non-status actions (`pause_communication`, `link_service_request`, ...) are rows with
  `to=None` in the map. Your handler sets the fields, then calls `apply` so that history and
  events are written.
- Guard functions read core columns and your own tables. They return
  `GuardFailure(code, message)` or `None`.
- Guards must have no side effects, because they also run to compute `blockedActions`.
- If a guard inspects the payload, register it with `needs_payload=True`. It is then skipped
  when computing availability.

### 3.6 Events

| Event | Emitted by | Typical subscriber |
|---|---|---|
| `Transitioned` | TransitionService | mails, side tables |
| `AssetMoved` | AssetService | stock, inventory |
| `NotificationQueued` (in-transaction) / `NotificationSent` / `NotificationFailed` | `notify()` / `notifications.send` | H1 `mail_log` |
| `AppointmentChanged`, `ImportFinished`, `StockRecalculated`, `UserDeactivated`, `VipListChanged` | owning slice / core | anyone |

Event fields:
- `Transitioned`: `entity_type`, `entity_id`, `action`, `from_status`, `to_status`,
  `actor_id`, `trigger`, `payload`, `comment`. In-transaction handlers also get `entity`
  and `payload_model`.
- `AssetMoved`: `movement_id`, `asset_id`, `action`, `from_status`, `to_status`,
  `service_request_id`, `trigger`.

Phases:
- **`in_transaction`**, handler `async def h(session, event)`:
  - runs inside the publisher's transaction;
  - may write rows and call core services;
  - an exception rolls everything back.
- **`after_commit`** (the default), handler `async def h(event)`:
  - runs after a successful `commit()`;
  - opens its own session (`async with open_session() as s:`) and commits it;
  - failures are logged, never raised.

  Use it for provider calls and anything else that must not roll back.

Filter early: `if event.entity_type != "service_request" or event.action != "ship": return`.

Emit core events with `await emit(session, ImportFinished(...))`. Slices do not define new
cross-slice event types; file a request instead.

### 3.7 Jobs

```python
# request side
from app.core.enums import JobKind
from app.core.jobs import enqueue

job = await enqueue(
    session,
    "imports.process",
    kind=JobKind.IMPORT,
    created_by=user,
    entity=("import_job", import_job.id),
    import_job_id=import_job.id,
)
await commit(session)  # the task is deferred AFTER the commit
return JobAccepted(job_id=job.id, import_job_id=import_job.id)

# worker side (tasks.py)
from app.core.db import commit, open_session
from app.core.jobs import tracked


async def process(job_id: int, import_job_id: int) -> None:
    async with tracked(job_id) as run:  # running → succeeded/failed, trigger "job"
        async with open_session() as session:
            await import_rows(session, import_job_id, run)  # testable core of the task
            await commit(session)
        run.result = {"kind": "import", "importJobId": import_job_id}
```

- Task arguments are JSON scalars (ids). Tasks are idempotent and open their own session.
- Progress is written in separate short transactions so that `GET /jobs/{id}` sees it.
  - Report progress with `await run.progress(processed, total, "message")`.
  - In loops, check `await run.cancel_requested()`.
- Periodic tasks: `register_task(fn, ..., periodic="<cron>")`.
  - procrastinate passes a `timestamp: int` argument.
  - The cron is in **UTC**. For a business-time schedule, run hourly and check
    `business_hour()`; `app/core/demo.py::demo_reset_nightly` shows the pattern.
- `queueing_lock=` prevents duplicate pending jobs; `lock=` serializes runs.
- For fire-and-forget work without a `background_jobs` row, use
  `after_commit(session, lambda: defer("x.y", id=1))` with `from app.jobs.app import defer`.
- Cancel: `await request_cancel(session, job_id)` sets `background_jobs.cancel_requested`
  (in your transaction) so the task's `run.cancel_requested()` turns true.
- Seeders record finished history with `await record_job(session, kind, status, created_at=...,
  started_at=..., finished_at=..., progress=(n, total), result=...)`; nothing is deferred.
- Per-row work inside one transaction: `async with savepoint(session):` (`app.core.db`) rolls
  back the SAVEPOINT on an exception **and** drops the after-commit callbacks (mails, jobs)
  queued inside it, then re-raises.

### 3.8 Notifications, files, gateways, search, seed

**Mail.** Queue a mail with:

```python
await notify(
    session,
    "sr.shipping_confirmation",
    to=[contact.email],
    cc=[...],
    context={...},
    entity=EntityRef(type="service_request", id=sr.id, label=sr.number),
    attachments=[stored_file.id],
)
```

- **Delivery** happens after commit, in task `notifications.send` (queue `mail`), through
  the `Mailer` gateway. Retries wait 10 s, 60 s and 300 s.
- **Templates** are body fragments; core wraps them in
  `core/notifications/templates/base.*.j2`.
- **Available in templates:**
  - filters `date`, `datetime`, `time` (business TZ);
  - global `salutation(gender, first, last)`;
  - always-present context keys `app_name`, `operator_name`, `client_name`,
    `team_mailbox`, `frontend_url`.
- **Undefined variables raise** (StrictUndefined), so keep `sample_context` complete.
- **Demo mode** delivers only to `@example.com`.

**Files.**
- Store bytes: `stored = await store_file(session, data, filename, content_type, purpose, entity=..., ttl=...)`
  (`created_at=` backdates seeded files). `files.cleanup` deletes expired files only when no
  foreign key (core or slice table) still references them.
- New articles: `await ensure_article_site_settings(session, article.id)` (`app.core.stock_math`)
  inserts the per-hub settings rows (idempotent).
- Get a link: `signed_url(stored)` returns `DownloadLink {url, filename, contentType, sizeBytes, expiresAt}`,
  served by `GET /files/{id}/download?expires=&signature=`.
- Read uploads: `await read_upload(upload, allowed={".csv": "text/csv", ...}, max_bytes=...)`.

**Gateways.** Depend on the protocol, never on a concrete class.
- In FastAPI routes: `booking: BookingDep`, `carrier: CarrierDep`, `mailer: MailerDep`,
  `ticketing: TicketingDep`.
- In tasks: `gateways.booking()` etc.
- Fakes are bound by default (`GATEWAY_*=fake`).
- Tests swap implementations with `restore = gateways.override(carrier=MyFake())`.

**Search.** A provider is `async def provider(session, q, user) -> list[SearchHit]`. Return
at most a handful of hits for your own entities; core merges them into `/search`.

**Seed.**
- A seeder is `async def seed_x(session, ctx: SeedContext) -> None`. `ctx` provides:

  | Attribute | Meaning |
  |---|---|
  | `ctx.today`, `ctx.d0` | today and the story's anchor date |
  | `ctx.wd(n)` | working days from the anchor |
  | `ctx.at(day, hour)` | a datetime on that day |
  | `ctx.rng` | random generator, seeded per seeder |
  | `ctx.data["core"]` | ids the core story prepared for slices: Theo's stops, overdue SRs, missing assets, inventory ids, ... |
  | `await ctx.site(session, "RIV")`, `ctx.user(...)`, `ctx.article(...)`, `ctx.location(...)` | lookups |

- Seeders must be:
  - idempotent: return early when your rows already exist;
  - deterministic;
  - relative to today.
- Exporters return `{"table_name": [camelCase rows]}`; `table_exporter(*tables)` does that
  for whole tables.
- `uv run hangar seed --export-json DIR` writes `DIR/seed.json` (`{meta, tables, enums, config}`).

### 3.9 Tests

- Put slice tests in `app/modules/<slice>/tests/test_*.py`. They are collected together
  with the core tests.
- Point at your own databases and port (WORKSPACE port table) in `.env` or the shell:
  - `TEST_DATABASE_URL=postgresql+psycopg://dev:dev@localhost:5432/hangar_h3_test`
  - `DATABASE_URL=.../hangar_h3`
  - `PORT=8103`
- What happens per test session:
  - The test DB is rebuilt once (`create_all` + job schema).
  - **All slices are loaded and all seeders run**, so your seeder is tested too.
  - Each test gets `db_session` inside a transaction that is rolled back, so `commit()` in
    code under test is safe.
- Fixtures from the root `conftest.py`:

  | Fixture | What it gives you |
  |---|---|
  | `db_session` | session inside the rolled-back test transaction |
  | `client` | httpx client against the app, bound to `db_session` |
  | `user(email)` | the persona's `User` |
  | `headers(email)` | `{"Authorization": "Bearer …"}` for that persona |
  | `jobs` | in-memory procrastinate connector; deferred jobs in `jobs.jobs` |
  | `mailer` | `MemoryMailer`; sent mails in `mailer.outbox` |
  | `file_store`, `app`, `engine` | file store, FastAPI app, DB engine |

- The seven personas (e.g. `dana.dispatcher@example.com`, `theo.technician@example.com`)
  use the password `demo1234`.
- Factories: `from app.testing import factories as f`.
  - `f.make_sr(session, status=..., region_code=..., campus=..., delivery_type=..., new_types=..., old_tags=..., email=..., appointment=..., editor=..., assign_new=..., packed=..., **columns)`
  - also `f.make_lease`, `f.make_asset`, `f.make_contact`, `f.user`, `f.site`, `f.region`,
    `f.location`, `f.article`.
- Code that runs in a task with `open_session()` cannot see the test transaction. Keep the
  task a thin wrapper and test its inner `async def ...(session, ...)` with `db_session`.
- Time: `time_machine.travel(...)`. HTTP adapters: `respx`.
- Cover:
  - the happy path;
  - 401 and 403 per role;
  - the 422 shape;
  - 409 for transitions and guards;
  - pagination and sort;
  - job behaviour;
  - your seeder's story numbers.

### 3.10 Slice README

Keep `app/modules/<slice>/README.md` current. It should list:
- tables;
- endpoints;
- actions;
- templates;
- tasks;
- the seed story;
- decisions taken without the product owner.

### 3.11 Definition of done

1. `uv run ruff check`, `uv run ruff format --check` and `uv run pyright` are clean.
2. `uv run pytest` passes for the **whole suite** against your `TEST_DATABASE_URL`.
3. The migration branch exists and `alembic check` is clean (§3.3).
4. `uv run hangar db reset --yes --seed` and `uv run hangar serve` work on your port, and
   `uv run hangar seed --export-json var/seed-export` writes your tables.
5. `uv run hangar openapi` has been regenerated and committed (§3.12).
6. The blocklist check prints `HARD hits: 0`, and your final report lists shared-file
   requests and decisions.

### 3.12 `openapi.json`

Regenerate it with `uv run hangar openapi` and commit it with your slice; never hand-edit
it. CI fails when it is stale. If it conflicts on merge, QA regenerates it.

## 4. What slices must never touch

**Code and config files:**
- `app/core/**`, `app/main.py`, `app/cli.py`, `app/jobs/**`, `app/testing/**`;
- `app/modules/__init__.py` and other slices' packages;
- `conftest.py`, `tests/**`, `pyproject.toml`, `uv.lock`;
- `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_core.py`, other slices' revisions;
- `Dockerfile`, `docker-compose.yml`, `.github/**`, `.env.example`, this file.

**Core-owned data**, which only core services write (golden rule 3):
- SR and lease columns: `status`, `status_changed_at`, `communication_date`, `*_sent_at`;
- asset columns: `status`, `location_id`, `installation_place`, `service_request_id`;
- `appointment_*`;
- `region_*`, `supplying_site_id`, `delivery_type`, `is_campus`, `is_vip`;
- lease link columns;
- the tables `status_transitions` and `asset_movements`;
- `background_jobs` (use the job helpers) and `stored_files` (use `store_file`).

Any other core column may be written only by the slice that SPEC §5 names as its writer.

**Also never:**
- read another slice's tables;
- use the registries outside `register()`;
- change global settings at runtime;
- call `session.commit()` directly (use `commit(session)`).

## 5. API and data conventions (summary)

| Thing | Convention | Example |
|---|---|---|
| Tables / columns | snake_case, tables plural | `tour_stops.planned_at` |
| Constraints | naming convention (automatic) | `uq_assets_serial_number`, `ck_tours_status` |
| Enum values | lower snake_case strings (DB + JSON) | `appointment_booked` |
| JSON fields, query params | camelCase | `assetTag`, `perPage`, `siteId` |
| URL paths | kebab-case plural resources | `/calloff-orders/{id}/actions/{action}` |
| Permissions | keys from `app/core/permissions.py` (SPEC §3.2) | `staging.personalize` |
| Task names | `<area>.<verb>` | `appointments.sync` |
| Error codes | snake_case `code`; `type` = `https://hangar.example.com/problems/<code>` | `guard_failed` |
| Times | `timestamptz` UTC in DB/JSON (`...Z`); business dates in `HANGAR_BUSINESS_TZ` | `business_today()` |
| Money/qty | integers | `orderQty` |

Errors are RFC 9457 `application/problem+json`:
- standard fields: `type`, `title`, `status`, `detail`, `instance`;
- Hangar fields: `code` and `requestId`;
- extensions: `errors`, `allowedActions`, `guard`, `currentVersion`.

UI labels and tones for enums come from `GET /meta/enums` (`ENUM_META`).

## 6. Commands

| Command | What it does |
|---|---|
| `uv sync` | install everything (incl. dev tools) |
| `uv run hangar serve [--reload]` | API on `PORT` (default 8100); docs at `/api/v1/docs` |
| `uv run hangar worker [--once] [-q mail]` | job worker (all queues by default) |
| `uv run hangar db upgrade` | `alembic upgrade heads` + procrastinate schema |
| `uv run hangar db reset --yes [--seed]` | drop schema + `create_all` + job schema (dev/test only) |
| `uv run hangar seed [--reset] [--export-json DIR]` | idempotent seed; `--reset` = demo reset; writes `DIR/seed.json` |
| `uv run hangar demo-reset` | truncate demo data + re-seed |
| `uv run hangar openapi [-o FILE]` | write `openapi.json` |
| `uv run pytest [path]` | tests (`TEST_DATABASE_URL` from the shell or `.env`, default `hangar_test`) |

## 7. Pitfalls (learned the hard way)

- **Use `commit(session)`, not `session.commit()`.** Only `app.core.db.commit` runs the
  after-commit callbacks: job deferral, mail delivery and after-commit event handlers.
- **Async lazy loading raises `MissingGreenlet`.** Two kinds of object have unloaded
  relationships: objects you created in this session, and objects loaded without eager
  options. Either call `await ensure_loaded(session, entity, "rel_a", "rel_b")` or use
  `selectinload(...)`. Core already does this before guards, transitions and builders.
- **`model_dump()` returns camelCase** (`serialize_by_alias=True`). To turn a DTO into ORM
  kwargs, use `body.model_dump(exclude_unset=True, by_alias=False)`.
- **An `IntegrityError` poisons the session** inside a request. Check uniqueness explicitly
  and raise `ConflictError` before flushing.
- **After a rollback** (for example bulk failures), ORM objects are expired. Re-read the ids
  and values you need.
- **Status values compare as strings** (`StrEnum`), but always assign enum members.
- **Dates and times:** use `business_today()` and `now_utc()` from `app/core/timeutil.py`,
  never `date.today()`.
- **Windows:** psycopg async needs the selector event loop. The CLI, worker and tests already
  configure it. Scripts should use `app.core.aio.run(...)` instead of `asyncio.run(...)`.

## 8. Available libraries

**Runtime:**
- web and validation: fastapi, pydantic 2 (+ email-validator), pydantic-settings,
  python-multipart;
- database and jobs: SQLAlchemy 2 (async, psycopg 3), alembic, procrastinate;
- spreadsheets: polars, fastexcel, xlsxwriter;
- documents and mail: reportlab, jinja2, aiosmtplib;
- HTTP: httpx, tenacity;
- auth: PyJWT, pwdlib[argon2];
- other: structlog, faker, orjson, click, charset-normalizer, tzdata.

**Dev:** pytest, pytest-asyncio, respx, time-machine, pypdf, coverage, ruff, pyright.

## 9. Logging

Use `log = structlog.get_logger(__name__)` and `log.info("event_name", key=value)`.
- Event names are snake_case.
- Log ids instead of personal data.
- No `print`.
- Every request log line carries `request_id` and `user_id`. The request id is also
  returned in the `X-Request-Id` header and in problem bodies.

## 10. Decisions taken without the product owner

**Phase 2a:**
- States are VARCHAR + CHECK (`str_enum`), not native PostgreSQL enums.
- No implicit commit per request: services never commit; endpoints call `commit()`.

**Phase 2b (core):**
- **Slice packages** are `h1_requests`, `h2_imports`, `h3_staging`, `h4_field` and
  `h5_stock`. The canonical extension mechanism is `register(reg)`; only `models.py` is
  auto-imported.
- **`JWT_SECRET` default** is `dev-only-change-me-0123456789abcdef`. HS256 needs at least
  32 bytes, and the settings validator rejects the spec's shorter placeholder.
- **Fake-gateway storage is core (#5):** `booking_sandbox_appointments` and the sequences
  `booking_sandbox_change_seq` / `fake_carrier_seq`, because the fakes are core gateways.
- **Missing comment** on a `requiresComment` action returns `422 validation_error`
  (`errors.comment`); it is not a guard (#34).
- **Manual region override (#40)** sets `region_overridden=true`, clears the rule and sets
  `is_campus=false`. Clearing the override re-resolves the region.
- **Unknown serial during an inventory scan** uses scan code `unknown_recorded`.
- **Any-of permissions** (the on-site tour actions) appear in `/meta/transitions` with their
  first key.
- **Health endpoints** are `/api/v1/healthz` (liveness) and `/api/v1/readyz` (DB + job
  schema).
- **File downloads** are served at `/files/{id}/download?expires=&signature=`. The
  signature is HMAC-SHA256 hex over `"{id}:{expires}"`; a bad or expired signature returns
  `403 invalid_signature`.
- **Seeded missing assets:** the 4 are split Riverton 2 / Eastgate 2 (#44). The contact
  total (420) is derived from the story.
- **`holdSource`** (`import` | `manual`) is also exposed on the SR detail, for #37.
- **Chain mails** set `communication_date` (#9) and the lease `*_sent_at` columns together.
- **`/meta/enums`** omits `stage`/`terminal` where they do not apply.
- **405 problems** use code `method_not_allowed`.
- **`/stock-locations`** accepts an `active` parameter.
- **Demo reset** keeps the `background_jobs` row of the running reset (#48) and the
  `deviation_codes` reference rows.
