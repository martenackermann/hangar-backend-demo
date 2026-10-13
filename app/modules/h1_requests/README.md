# `h1_requests` — Requests & Comms

Service request and lease return lists/details/edits/histories/exports, contacts, the
pipeline summary and the whole communication chain (dispatch settings, the daily
`comms.dispatch` job, dispatch runs, mail log, notification templates).

## Tables (`alembic/versions/h1_0001_requests.py`, branch `h1`)

| Table | Notes |
|---|---|
| `dispatch_settings` | singleton (`CHECK id = 1`, inserted by the migration and the seeder), SPEC §5.2 ranges as CHECKs |
| `dispatch_runs` | scheduled / manual / dry runs; `counts`, `skipped`, `errors`, `planned` (dry runs) |
| `mail_log` | one row per `notify()`; `queued` → `sent` / `failed` from the notification events |

## Endpoints (`/api/v1`)

| Method & path | Permission |
|---|---|
| `GET /service-requests` (all API.md §4.1 filters, `vip=exclude` default) | `sr.read` |
| `GET /service-requests/{id}`, `GET /service-requests/by-number/{number}` | `sr.read` |
| `PATCH /service-requests/{id}` | `sr.update` |
| `GET /service-requests/{id}/history` | `sr.read` |
| `POST /service-requests/export` (list params → xlsx `DownloadLink`) | `sr.export` |
| `GET /lease-returns`, `GET /lease-returns/{id}` | `lease.read` |
| `PATCH /lease-returns/{id}`, `POST /lease-returns/{id}/comments` | `lease.manage` |
| `GET /lease-returns/{id}/comments`, `GET /lease-returns/{id}/history` | `lease.read` |
| `POST /lease-returns/export` | `lease.export` |
| `GET /contacts`, `GET /contacts/{id}` | `sr.read` |
| `PATCH /contacts/{id}` | `contacts.update` |
| `GET /pipeline/summary` | `sr.read` |
| `GET /mail-log`, `GET /mail-log/{id}` | `comms.read` |
| `GET /dispatch-settings` / `PUT /dispatch-settings` | `comms.read` / `comms.settings` |
| `GET /comms/dispatch-runs`, `GET /comms/dispatch-runs/{id}` | `comms.read` |
| `POST /comms/dispatch-runs {dryRun}` (dry → 200 run, real → 202 `JobAccepted`, 409 while one runs) | `comms.dispatch` |
| `GET /notification-templates`, `GET /notification-templates/{id}/preview?entityType&entityId` | `comms.read` |

## Actions (generic endpoints, core dispatch)

- SR: `send_invitation`, `send_campus_info`, `send_reminder_1`, `send_reminder_2`, `escalate`,
  `send_next_mail`, `mark_hardware_unavailable`, `release_hardware_hold`, `release_vip`,
  `pause_communication {reason, resumeDate?}`, `resume_communication`, `cancel {comment}`, `reopen`.
- Lease: `send_info`, `send_reminder_1`, `send_reminder_2`, `escalate`, `send_next_mail`,
  `release_vip`, `link_service_request {serviceRequestNumber}`, `unlink_service_request`,
  `close {comment}`, `reopen`, `pause_communication`, `resume_communication`.

Effects run in in-transaction `Transitioned` subscribers, so they apply whatever the
trigger (user, job, import, system): chain mails, hold mail once (+ `hold_source`), chain
restart on `release_hardware_hold` / `reopen`, cancel effects (new assets released, only new
items lose `asset_id`, editor cleared, `cancelled_at`), lease `closed_at` / `close_reason`,
reschedule mails, and lease unlinking when an SR reaches `cancelled` by any action (#31).

## Guards (job trigger only — manual sends ignore due rule, pause and master switch)

| Code | Actions | Message |
|---|---|---|
| `chain_due` | SR + lease chain actions | The next mail of the chain is not due yet. |
| `not_paused` | SR + lease chain actions | Communication is paused. |
| `retirement_window` | lease `send_info` | The retirement date is outside the info mail window. |

Authorizers: `send_campus_info` only for campus SRs (`403 is_campus`), `send_invitation`
only for non-campus SRs (`403 not_campus`) — the variant that does not apply is hidden
instead of being offered as a permanently blocked button.

## Templates

`sr.invitation`, `sr.campus_info`, `sr.reminder_1`, `sr.reminder_2`, `sr.escalation`
(cc escalation mailbox), `sr.hardware_unavailable`, `sr.reschedule`, `sr.reschedule_campus`,
`lease.info|reminder_1|reminder_2|escalation` + `_campus` variants (escalation cc the lease's
`escalation_email` + escalation mailbox). All have `sample_context` and `build_context`.

## Tasks

`comms.dispatch` (queue `default`, lock `comms.dispatch`): periodic hourly at :00 UTC,
proceeds only at 07:xx business time and only if no scheduled run exists today (DST-safe);
also deferred by `POST /comms/dispatch-runs` with `job_id` + `dispatch_run_id`. Algorithm:
SPEC §6.3 (reverse step order, leases first, pages of 200, commit per entity, budget →
`cap_reached`, guard failures → `skipped[code]`, other errors → `errors`).

## Seed (order 10)

`dispatch_settings` defaults; `mail_log` rebuilt from every mail-implying
`status_transitions` row (chain mails, holds, reschedules, shipping confirmations,
protocols — H1 templates rendered in their historical state, other slices' templates get the
catalogue subject only); the daily runs T−13…T whose counts equal that day's job-triggered
chain transitions (today's only once the story sent today's mails), each with its
`background_jobs` row; one dry run by Dana computed live. Two lease reminders are seeded as
`failed` (mailbox full). Exporter: all three tables; `mail_log.bodyHtml` only for the newest
150 rows (size), `bodyText` for all.

## Decisions taken without the product owner

- `PATCH /service-requests/{id}`: zip, city, cost centre, region and delivery-type override
  are locked outside `REGION_MUTABLE_STATUSES` (#29); street, room, building, country,
  on-site phone and note stay editable until the request is closed.
- `link_service_request` with an unknown/cancelled SR number → `422` on
  `serviceRequestNumber` (form field error) rather than a guard.
- `pause_communication` with a `resumeDate` not in the future → `422 resumeDate`.
- `GET /comms/dispatch-runs`: `dryRun` is an optional filter (absent = all runs).
- `POST /comms/dispatch-runs`: a queued/running real run younger than 30 minutes blocks a
  new one (`409 conflict`); older ones are treated as stale.
- `NotificationTemplateInfo` carries an extra `entityType` (null = sample preview only).
- Contacts list `vip` defaults to `include` (it is a lookup, not an operational list).
- Seeded `sr.protocol` mails show the SR's stored protocol PDF as attachment (H4 stores it
  after the H1 seeder ran).
- Effects that write chain columns (`communication_date` reset on hold release / reopen,
  `closed_at`, `cancelled_at`, `hold_source`) are written by the H1 subscribers of H1-owned
  transitions — the spec names H1 as the owner of those effects and core implements none.
