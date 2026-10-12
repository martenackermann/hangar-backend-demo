# Hangar API

Backend of **Hangar**, an IT-hardware rollout & logistics console (portfolio demo): service
requests, lease returns, staging, shipping, technician tours, stock, inventory and imports
for the fictional operator *Acme IT Services* and its client *Globex Fleet AG*.

> Work in progress — the foundation (infrastructure) is in place; feature modules follow.

## Stack

Python 3.13 · FastAPI · Pydantic 2 · SQLAlchemy 2 (async, psycopg 3) · Alembic ·
PostgreSQL 17 · procrastinate (Postgres job queue) · polars · ReportLab · structlog · uv

## Quick start

With Docker only:

```bash
docker compose up --build
# API docs  http://localhost:8100/api/v1/docs
# Mailpit   http://localhost:8025
```

For development (needs PostgreSQL and Mailpit, e.g. the shared dev containers):

```bash
uv sync
cp .env.example .env              # adjust DATABASE_URL / PORT if needed
uv run hangar db-reset --yes --seed
uv run hangar serve --reload      # http://localhost:8100/api/v1/docs
uv run hangar worker              # in a second terminal
uv run pytest                     # uses TEST_DATABASE_URL (default: hangar_test)
```

## Commands

| Command | Purpose |
|---|---|
| `uv run hangar serve` | run the API (port `PORT`, default 8100) |
| `uv run hangar worker` | run the background job worker |
| `uv run hangar db-reset --yes` | drop and recreate the dev schema |
| `uv run hangar migrate` | apply migrations and the job-queue schema |
| `uv run hangar seed` | load demo data |
| `uv run hangar openapi` | export `openapi.json` |

## API conventions

- JSON in camelCase; list endpoints use `?page=&perPage=&sort=` and return
  `{ data, meta: { page, perPage, total, totalPages } }`.
- Errors are RFC 9457 `application/problem+json`; validation errors carry
  `errors: { field: [messages] }`.
- Long-running operations answer `202 { jobId }`; poll `GET /api/v1/jobs/{jobId}`.

See [CONVENTIONS.md](CONVENTIONS.md) for the full contributor guide.

## Demo notice

All names, companies and addresses are fictional; e-mail addresses use the reserved
`example.com` domain. Demo accounts use the password `demo1234` (demo only).

## License

[MIT](LICENSE)
