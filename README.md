# Hangar API

This is the backend of **Hangar**, a portfolio demo of an IT-hardware rollout and logistics
console. It covers:

- service requests and lease returns;
- staging and shipping;
- technician tours;
- stock, inventory and imports.

The fictional operator is *Acme IT Services*; its client is *Globex Fleet AG*.

> **Status:** the core is complete. Five feature slices (`app/modules/h1_*` … `h5_*`)
> plug into it.

**What the core contains:**
- auth and permissions;
- the transition maps for service requests, lease returns and assets;
- the action registry, asset ledger, event bus and notifications;
- files, jobs and gateways (fakes bound by default);
- region resolver and stock math;
- global search;
- the deterministic seed story and demo reset.

## Stack

| Area | Tools |
|---|---|
| Language and API | Python 3.13, FastAPI, Pydantic 2 |
| Database | SQLAlchemy 2 (async, psycopg 3), Alembic, PostgreSQL 17 |
| Jobs | procrastinate (Postgres job queue) |
| Files and mail | polars, ReportLab, Jinja2 |
| Tooling | structlog, uv |

## Quick start

With Docker only:

```bash
docker compose up --build
# API docs  http://localhost:8100/api/v1/docs   (dana.dispatcher@example.com / demo1234)
# Mailpit   http://localhost:8025
```

For development you need PostgreSQL and Mailpit, for example the shared dev containers:

```bash
uv sync
cp .env.example .env               # adjust DATABASE_URL / PORT if needed
uv run hangar db upgrade           # alembic upgrade heads + job-queue schema
uv run hangar seed                 # deterministic demo story, relative to today
uv run hangar serve --reload       # http://localhost:8100/api/v1/docs
uv run hangar worker               # in a second terminal
uv run pytest                      # uses TEST_DATABASE_URL (default: hangar_test)
```

## Commands

| Command | Purpose |
|---|---|
| `uv run hangar serve` | run the API (port `PORT`, default 8100) |
| `uv run hangar worker [--once]` | run the background job worker |
| `uv run hangar db upgrade` | apply migrations (`alembic upgrade heads`) and the job-queue schema |
| `uv run hangar db reset --yes [--seed]` | drop and recreate the dev schema with `create_all` |
| `uv run hangar seed [--reset] [--export-json DIR]` | load demo data; with `--export-json`, also write `DIR/seed.json` for the frontend mock API |
| `uv run hangar demo-reset` | truncate demo data and re-seed |
| `uv run hangar openapi` | export `openapi.json` (CI checks that it is current) |

## Demo personas

All seven personas use the password `demo1234`:

| Persona | E-mail | Role | Story |
|---|---|---|---|
| Dana Dispatcher | `dana.dispatcher@example.com` | dispatcher | imports, comms, staging, tour planning, call-offs |
| Theo Technician | `theo.technician@example.com` | technician | Riverton tour today with 5 stops |
| Vic Viewer | `vic.viewer@example.com` | viewer | client-side observer |
| Ada Admin | `ada.admin@example.com` | admin | master data, users, demo reset |
| Tara Lindqvist | `tara.lindqvist@example.com` | technician | Hillford tours |
| Omar Reyes | `omar.reyes@example.com` | technician | Eastgate, Northport, Southmere tours |
| Jonas Weber | `jonas.weber@example.com` | technician | Westbay, Lakeside, Highmoor, Oakridge tours |

`GET /api/v1/auth/personas` lists them; `POST /api/v1/auth/persona-login` signs in as one
of them in demo mode.

## API conventions

- **JSON** is camelCase.
- **Lists** use `?page=&perPage=&sort=` and return
  `{ data, meta: { page, perPage, total, totalPages } }`.
- **Errors** are RFC 9457 `application/problem+json` with a stable `code`:
  - validation errors carry `errors: { field: [messages] }`;
  - refused transitions carry `allowedActions`;
  - failed guards carry `guard: { code, message }`.
- **Workflow actions** use `POST /<resource>/{id}/actions/{action}` and
  `POST /<resource>/bulk/{action}`. Every detail response lists `availableActions` and
  `blockedActions`.
- **Long-running operations** answer `202 { jobId }`; poll `GET /api/v1/jobs/{jobId}`.
- **Labels and colours** for every enum come from `GET /api/v1/meta/enums`.

[CONVENTIONS.md](CONVENTIONS.md) is the contributor guide. Its "How to build a slice"
section explains how to add a slice. `_spec/hangar/SPEC_RESOLUTIONS.md` takes precedence
over the other spec documents.

## Demo notice

All names, companies and addresses are fictional; e-mail addresses use the reserved
`example.com` domain. The demo accounts use the password `demo1234` (demo only).

## License

[MIT](LICENSE)
