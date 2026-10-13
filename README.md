# Hangar API

**The backend of Hangar, an IT-hardware rollout and logistics operations console.** It covers
the whole run of a hardware refresh: service requests from import to deployment, lease returns,
staging and shipping, technician tours, stock and inventory.

[![CI](https://github.com/martenackermann/hangar-backend-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/martenackermann/hangar-backend-demo/actions/workflows/ci.yml)
![Python 3.13](https://img.shields.io/badge/python-3.13-3776ab)
![License: MIT](https://img.shields.io/badge/license-MIT-blue)

- **Live demo** (the frontend, running fully in the browser): <https://hangar-demo.vercel.app>
- **Frontend repository:** [hangar-frontend-demo](https://github.com/martenackermann/hangar-frontend-demo)
- **API docs:** Swagger UI at `/api/v1/docs` on a running instance, or the committed
  [`openapi.json`](openapi.json)

## About this project

Hangar is a clean-room re-engineering of an internal logistics tool, rebuilt from scratch as a
portfolio demo. All people, companies, sites and data are fictional: the operator is *Acme IT
Services*, its client *Globex Fleet AG*, the logistics partner *Initech Logistics*, and the hubs
are Riverton, Hillford and Eastgate. External integrations (the ticketing system, the booking
provider and the parcel carrier) are simulated by fake adapters, and every e-mail address uses
the reserved `example.com` domain.

## What it does

A client is replacing thousands of laptops, docks and monitors. Each replacement is a
**service request** (SR) that arrives in a ticketing export and travels down a fixed line:

1. **Imported** from the ticketing export (CSV/XLSX); the region is resolved from the zip code.
2. **Invited**: a mail chain asks the employee to book an appointment (invitation, two
   reminders, escalation), pausable per request.
3. **Booked**: the booking provider is synced hourly and appointments land on the SR.
4. **Personalizing** at the hub: someone claims the request, devices are assigned by scanning
   (oldest stock first), installation places and a software checklist are recorded.
5. **Ready** for picking.
6. **Shipped / provided**: parcels go out through the carrier (label, tracking, return label);
   technician deliveries are planned into tours.
7. **Deployed** on site: the technician scans the new and the old devices, records deviations,
   and a protocol PDF goes to the employee.
8. **Closed**: cancelled or archived.

Alongside that, **lease returns** chase old devices back, **stock** is planned per hub with
call-off orders to the logistics partner, **inventories** are counted with scanners, and a
dashboard shows where everything stands.

## Why this stack

| Choice | Why it fits the job |
|---|---|
| **FastAPI + Pydantic v2** | Every screen of the console is a dense, filterable list plus workflow actions. Typed request and response models give one camelCase contract (`openapi.json`, which the frontend generates its types from), RFC 9457 problem responses with stable codes, and one place to validate the packed import fields. |
| **SQLAlchemy 2 (async) + Alembic** | The domain is relational (requests, items, assets, movements, tours) and needs optimistic versions and row locks. Each feature slice owns its tables and its own **Alembic branch** (`h1_0001_requests` … `h5_0001_stock`), joined by one merge revision, so the slices were built in parallel without migration conflicts. |
| **PostgreSQL 17** | One database for the data, trigram search (`pg_trgm`) for the ⌘K palette, and the job queue. |
| **procrastinate** | Postgres-native background jobs for imports, the hourly appointment sync, the daily mail dispatch, stock recalculation, mail delivery with retries and the nightly demo reset. No Redis or broker to run, and a job is enqueued in the same transaction as the change that caused it. |
| **polars** (+ fastexcel) | Imports are ticketing exports with thousands of rows and *packed* fields (`[Procurement type: Replacement],[Old base device: GX100324]`). Column-wise validation in polars, then one savepoint per row, keeps a large import fast and reports every bad line instead of failing the file. |
| **ReportLab** | Deployment protocols and call-off orders as PDFs, generated on the server and served as signed downloads. |
| **httpx gateways behind Protocols** | The booking provider, parcel carrier, ticketing system and mailer are `typing.Protocol`s. Deterministic fakes are bound by default (the booking fake keeps its state in Postgres, so syncs behave like a real provider); the real adapters are httpx skeletons with tenacity retries. Tests swap them with `gateways.override(...)`. |
| **structlog, uv, ruff, pyright** | JSON logs with request ids, reproducible installs, strict lint and type checks in CI. |

## Architecture

```mermaid
flowchart LR
  subgraph API["FastAPI app (/api/v1)"]
    direction TB
    R["Core routers<br/>auth · me · meta · search · files · jobs · saved views · demo"]
    S["Slice routers<br/>h1 requests · h2 imports · h3 staging · h4 field · h5 stock"]
    A["Generic action endpoints<br/>/resource/{id}/actions/{name} · /bulk/{name}"]
  end

  subgraph Core["Core"]
    direction TB
    REG["SliceRegistry<br/>routers · actions · guards · authorizers<br/>templates · tasks · seeders · search"]
    AR[ActionRegistry]
    TS["TransitionService<br/>SR · lease · asset maps"]
    AS["AssetService<br/>status, location, movement ledger"]
    EB["Event bus<br/>in-transaction · after-commit"]
    N["Notifications<br/>templates · mail log"]
    G["Gateways<br/>booking · carrier · ticketing · mailer"]
  end

  W["Worker<br/>(procrastinate)"]
  DB[("PostgreSQL<br/>data + job queue")]
  M["SMTP / Mailpit"]

  S -. "register(reg)" .-> REG
  A --> AR --> TS
  R --> TS
  TS --> EB
  AS --> EB
  EB --> N
  TS --> DB
  AS --> DB
  N -- "after commit" --> W
  W --> DB
  W --> G
  G --> M
```

- **Core** (`app/core`) owns auth and permissions, the three transition maps, the action
  registry, the asset ledger, the event bus, notifications, files, jobs, gateways, search, the
  seed story and the demo reset.
- **Slices** (`app/modules/h1_requests` … `h5_stock`) plug in through a single function,
  `register(reg: SliceRegistry)`: routers, actions with payloads, guards and authorizers on core
  transitions, event subscribers, mail templates, periodic tasks, search providers, seeders and
  exporters. Core never imports a slice by name.
- **Status changes go through services only.** `TransitionService.apply()` checks the action,
  the source state, the permission, row-level authorizers ("this stop belongs to another
  technician's tour") and guards, then writes the status and a `status_transitions` row and
  emits `Transitioned`. Every detail response lists `availableActions` and `blockedActions`
  with the guard's reason, so the UI never re-derives a rule.
- **Events run in two phases.** In-transaction subscribers (side tables, chain mails) roll back
  with the request; after-commit subscribers call providers and can never undo the change.

[CONVENTIONS.md](CONVENTIONS.md) is the contributor guide, including how to build a slice.

## Domain

### Service request lifecycle

Eight pipeline stages (the boxes) with the states inside them:

```mermaid
stateDiagram-v2
  direction LR
  state "1 Imported" as S1 {
    imported
    imported_vip
    hardware_unavailable
  }
  state "2 Invited" as S2 {
    invited
    campus_info_sent
    reminder_1_sent
    reminder_2_sent
    escalated
  }
  state "3 Booked" as S3 {
    appointment_booked
  }
  state "4 Personalizing" as S4 {
    personalization_in_progress
    personalization_done
  }
  state "5 Ready" as S5 {
    ready_for_picking
  }
  state "6 Shipped / provided" as S6 {
    shipped
    provided
    reschedule_needed
  }
  state "7 Deployed" as S7 {
    completed
  }
  state "8 Closed" as S8 {
    cancelled
  }

  [*] --> S1: import
  S1 --> S2: invitation / campus info
  S2 --> S3: booked or synced
  S3 --> S4: claim
  S3 --> S5: skip preparation
  S4 --> S5: close order
  S5 --> S6: ship / hand to technician
  S6 --> S7: complete deployment
  S6 --> S3: reschedule, new booking
  S6 --> S8: cancel on site
  S8 --> S1: reopen
```

Inside a stage: `imported_vip` waits until VIP handling is released, `hardware_unavailable` is
a hold, and the mail chain steps from `invited` through two reminders to `escalated`. Mail steps
can be paused until a resume date, and `send_next_mail` picks the next step. Any open request
can be cancelled with a comment.

### Lease return lifecycle

```mermaid
stateDiagram-v2
  direction LR
  [*] --> imported: import
  imported_vip --> imported: release VIP
  imported --> info_sent: info mail
  info_sent --> reminder_1_sent
  reminder_1_sent --> reminder_2_sent
  reminder_2_sent --> escalated
  imported --> replacement_ordered: link the replacement SR
  escalated --> replacement_ordered: link the replacement SR
  replacement_ordered --> imported: unlink (restores the previous step)
  escalated --> probably_closed: missing from the next import
  probably_closed --> closed
  replacement_ordered --> closed
  closed --> imported: reopen (restores the previous step)
```

A lease return is linked to the service request that orders its replacement, and the device's
retirement date drives the reminders.

### Assets

`AssetService` is the only writer of an asset's status and location; every change is a row in
the `asset_movements` ledger (goods-in, assignment, staging, shipping, deployment, return,
deactivation). Inventories compare scans against that ledger and report wrong locations,
unknown serials and missing devices.

## Import formats

An upload's type is detected from its header row. Rows are validated column-wise, applied in
chunks of 500 with one savepoint per row, and failed rows come back as a downloadable report.

| Format | Creates or updates | Sample |
|---|---|---|
| Service requests (ticketing export) | SRs, items and contacts; packed `[Key: value]` order details | [`service_requests.csv`](samples/service_requests.csv), with errors: [`service_requests_with_errors.csv`](samples/service_requests_with_errors.csv) |
| Lease returns | lease returns, late links to SRs | [`lease_returns.csv`](samples/lease_returns.csv) |
| New assets (goods-in) | assets received at a hub | [`assets_new.csv`](samples/assets_new.csv) |
| Asset status sync | devices in use or collected | [`asset_status_sync.csv`](samples/asset_status_sync.csv) |
| Contacts | employee master data | [`contacts.csv`](samples/contacts.csv) |
| Deactivation | retired devices | [`deactivation.csv`](samples/deactivation.csv) |

CSV (UTF-8, delimiter detected) and XLSX are accepted. Uploads are size-limited while they
stream, XLSX archives are checked for decompression bombs before parsing, and CSV/XLSX exports
neutralise spreadsheet formulas.

## Quick start

### Docker: everything included

```bash
docker compose up --build
```

| Service | URL |
|---|---|
| API docs | <http://localhost:8100/api/v1/docs> |
| Mailpit, every mail the demo sends | <http://localhost:8025> |

The stack runs the API, the worker, PostgreSQL 17 and Mailpit; migrations and the demo seed run
before the API starts. Sign in with `dana.dispatcher@example.com` / `demo1234`, or call
`POST /api/v1/auth/persona-login`. Ports bind to `127.0.0.1`; if 8100 or 8025 are taken, set
`API_PORT` / `MAILPIT_UI_PORT` (for example `API_PORT=18100 docker compose up --build`).

### Local development with uv

Needs Python 3.13, [uv](https://docs.astral.sh/uv/), PostgreSQL and an SMTP sink such as Mailpit.

```bash
uv sync
cp .env.example .env               # adjust DATABASE_URL / PORT if needed
uv run hangar db upgrade           # alembic upgrade heads + job-queue schema
uv run hangar seed                 # deterministic demo story, relative to today
uv run hangar serve --reload       # http://localhost:8100/api/v1/docs
uv run hangar worker               # second terminal: imports, mails, syncs
```

To use the console against it, run the
[frontend](https://github.com/martenackermann/hangar-frontend-demo) with `pnpm dev`; it expects
the API at `http://localhost:8100/api/v1`.

| Command | Purpose |
|---|---|
| `uv run hangar serve [--reload]` | API on `PORT` (default 8100) |
| `uv run hangar worker [--once] [-q mail]` | background worker (all queues by default) |
| `uv run hangar db upgrade` | `alembic upgrade heads` + job-queue schema |
| `uv run hangar db reset --yes [--seed]` | drop and recreate the dev schema |
| `uv run hangar seed [--reset] [--export-json DIR]` | demo data; `--export-json` writes `DIR/seed.json` for the frontend's demo mode |
| `uv run hangar demo-reset` | truncate the demo data and seed again |
| `uv run hangar openapi` | write `openapi.json` (CI checks that it is current) |

## Demo personas

All personas use the demo-only password `demo1234`.

| Persona | E-mail | Role | Story |
|---|---|---|---|
| Dana Dispatcher | `dana.dispatcher@example.com` | dispatcher | imports, mail chain, staging, tour planning, call-off orders |
| Theo Technician | `theo.technician@example.com` | technician | Riverton tour today with 5 stops |
| Vic Viewer | `vic.viewer@example.com` | viewer | read-only observer on the client side |
| Ada Admin | `ada.admin@example.com` | admin | master data, users, demo reset |
| Tara Lindqvist | `tara.lindqvist@example.com` | technician | Hillford tours |
| Omar Reyes | `omar.reyes@example.com` | technician | Eastgate, Northport, Southmere tours |
| Jonas Weber | `jonas.weber@example.com` | technician | Westbay, Lakeside, Highmoor, Oakridge tours |

The seed is deterministic and relative to today, so Theo's tour is always today's. Persona
logins and the demo endpoints exist only with `DEMO_MODE=true`.

## API conventions

- JSON is camelCase. Lists take `?page=&perPage=&sort=` and return
  `{ data, meta: { page, perPage, total, totalPages } }`.
- Errors are `application/problem+json` with a stable `code`: validation errors carry
  `errors: { field: [messages] }`, refused transitions `allowedActions`, failed guards
  `guard: { code, message }`.
- Workflow actions: `POST /<resource>/{id}/actions/{action}` and
  `POST /<resource>/bulk/{action}` (up to 200 ids, one savepoint each).
- Long-running work answers `202 { jobId }`; poll `GET /api/v1/jobs/{jobId}`.
- Labels, tones and transition metadata for every enum: `GET /api/v1/meta/enums`.

## Testing

```bash
uv run ruff check && uv run ruff format --check
uv run pyright
uv run pytest     # TEST_DATABASE_URL, default …/hangar_test on the local PostgreSQL
```

The suite (550+ tests) builds the test database once per session, loads every slice, runs every
seeder, and gives each test a session inside a rolled-back transaction. It covers the transition
maps, guards and authorizers, 401/403 for every role across all routes, imports end to end,
jobs, mails, PDFs, and the security hardening (login throttling, upload limits, formula
injection, demo gating). CI also applies the migrations to an empty database, runs
`alembic check`, exports the seed, checks that `openapi.json` is current, and builds the Docker
image.

## Configuration

Settings come from environment variables or `.env`; [`.env.example`](.env.example) lists them
with local placeholders.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://dev:dev@localhost:5432/hangar_main` | application database |
| `TEST_DATABASE_URL` | `…/hangar_test` | test database |
| `HOST` / `PORT` | `127.0.0.1` / `8100` | API bind address |
| `ENVIRONMENT` | `development` | `production` refuses placeholder secrets |
| `JWT_SECRET` | dev placeholder | token signing key (32+ characters) |
| `JWT_TTL_MINUTES` | `720` | token lifetime |
| `FILE_URL_SIGNING_KEY` | dev placeholder | HMAC key for signed download links |
| `HANGAR_CORS_ORIGINS` | `http://localhost:5173…5178` | allowed browser origins (`*` is refused) |
| `HANGAR_PUBLIC_API_URL`, `FRONTEND_URL` | localhost | links in mails and downloads |
| `HANGAR_BUSINESS_TZ` | `Europe/Zurich` | business dates and schedules |
| `SMTP_HOST` / `SMTP_PORT` | `localhost` / `1025` | outgoing mail |
| `MAIL_FROM`, `MAIL_TEAM_MAILBOX`, … | `…@example.com` | sender and team mailboxes |
| `GATEWAY_BOOKING` / `GATEWAY_CARRIER` / `GATEWAY_MAILER` | `fake` / `fake` / `smtp` | integration adapters |
| `DEMO_MODE` | `true` | personas, persona login, demo reset |
| `DEMO_NIGHTLY_RESET` | `false` | reset the demo data every night |
| `MAX_REQUEST_BYTES`, `IMPORT_MAX_BYTES`, `XLSX_MAX_UNCOMPRESSED_BYTES` | 2 MiB, 20 MiB, 128 MiB | request and upload limits |
| `LOGIN_RATE_LIMIT_*` | 10 per account, 50 per IP per 5 min | login throttling |

For anything public: `ENVIRONMENT=production`, long random `JWT_SECRET` and
`FILE_URL_SIGNING_KEY`, exact `HANGAR_CORS_ORIGINS`, and `FORWARDED_ALLOW_IPS` behind a reverse
proxy.

## Project structure

```
app/
  main.py            create_app(): core routers + slice routers under /api/v1
  cli.py             `hangar` CLI: serve, worker, db, seed, demo-reset, openapi
  core/              auth, permissions, transition maps, actions, assets, events,
                     notifications, files, jobs, gateways, search, seed, demo
  jobs/              procrastinate app and worker
  modules/
    h1_requests/     service requests, lease returns, pipeline, mail chain and mail log
    h2_imports/      imports (CSV/XLSX), articles, regions, VIP list, users
    h3_staging/      installation places, personalization, order closing, picking, shipping
    h4_field/        appointments, tours, deployment, protocols
    h5_stock/        stock matrix, call-off orders, inventories, deactivation, dashboard
  testing/           factories for slice tests
alembic/versions/    0001_core, one branch per slice, the merge revision
samples/             import samples
tests/               core and security tests (slice tests live in each slice)
```

Each slice has a README listing its tables, endpoints, actions, templates, tasks and seed
story, for example [`app/modules/h2_imports/README.md`](app/modules/h2_imports/README.md).

## License

[MIT](LICENSE) © Marten Ackermann
