# `h4_field` — Field service

Booking calendars, appointments + provider sync, tour planning, technician deployment
(scans, unknown devices, completion / reschedule / cancel on site) and the handover
protocol PDF. SPEC §2.5 (H4 row), §4.7, §5.5, §6.11, §6.12, §6.18, §8.1; SPEC_RESOLUTIONS
#2, #3, #6, #7, #8, #21, #26, #30, #32, #33, #47.

## Tables (Alembic branch `h4`, revision `h4_0001_field`)

| Table | Notes |
|---|---|
| `booking_calendars` | ids 1–9 fixed (regions sorted by name, `app/core/constants.py`); identity `BY DEFAULT` so sync can add more |
| `appointments` | one table for synced and manual bookings; **extra columns** `is_current` (the SR's current appointment) and `superseded_at` (#7) |
| `booking_sync_state` | singleton `id = 1` (CHECK), sync token + last result |
| `tours` | partial unique `(calendar_id, tour_date) WHERE status <> 'cancelled'`, optimistic `version` |
| `tour_stops` | unique `(tour_id, position)` DEFERRABLE INITIALLY DEFERRED; partial unique `(service_request_id) WHERE outcome = 'pending'` |

`booking_sandbox_appointments` is core-owned (fake gateway, #5).

## Endpoints (`/api/v1`)

| Method & path | Permission |
|---|---|
| `GET /booking-calendars` · `PATCH /booking-calendars/{id}` | `appointments.read` · `master.manage` |
| `GET /appointments` (`calendarId`, `from`, `to`, `status`, `serviceRequestId`, `unmatched`, `q`, sort `startAt`/`-bookedAt`) | `appointments.read` |
| `POST /appointments/sync` (202, 409 when a sync is queued/running) · `GET /appointments/sync-state` | `appointments.manage` · `appointments.read` |
| `POST /appointments/{id}/link` | `appointments.manage` |
| `GET /service-requests/{id}/appointments` | `sr.read` |
| `GET /service-requests/{id}/tour-stops` (#21) | `tours.read` |
| `GET /tours` (max 31 days, default current week) · `GET /tours/{id}` | `tours.read` |
| `POST /tours` · `PATCH /tours/{id}` · `PUT /tours/{id}/stops` | `tours.plan` |
| `POST /tours/{id}/actions/{start,finish,cancel}` | start/finish: own tour or `tours.execute_any`; cancel: `tours.plan` |
| `GET /tour-planner/candidates` · `GET /tour-planner/overview` (max 14 days) | `tours.read` |
| `GET /my/tours?date=` | `tours.execute_own` |
| `GET /tours/{id}/stops/{stopId}/deployment` · `POST /scan/deployment` · `DELETE …/items/{slot}/verification` · `POST …/items/{slot}/register-unknown` · `POST …/protocol/send` | own tour (`tours.execute_own`) or `tours.execute_any`; else `403 not_own_tour` |
| `GET /service-requests/{id}/protocol` | `sr.read` (404 unless completed; regenerated when missing) |

## SR actions, guards, authorizer

Actions (handlers in `actions.py`): `book_appointment`, `request_reschedule`,
`complete_deployment`, `reschedule_on_site`, `cancel_on_site`, `complete_manually`.
Internal (sync only): `appointment_synced`, `appointment_cancelled` (`appointments.py::reconcile_sr`).

Guard codes (`common.py`):

| Code | Actions | Rule |
|---|---|---|
| `calendar_enabled` | `book_appointment` | the chosen calendar is enabled (payload guard) |
| `tour_startable` | `complete_deployment`, `reschedule_on_site`, `cancel_on_site` | a pending stop's tour is `in_progress`, or `planned` for today with a technician (#32) |
| `deviation_outcome` | same three | the deviation code's outcome matches the action (payload guard) |
| `deployment_scans_complete` | `complete_deployment` | all counted items verified or a `complete` deviation; `handoverConfirmed` when a new item was verified |
| `tour_date_today` | tour `start` | tours start on their tour date |
| `no_pending_stops` | tour `finish` | every stop has an outcome |

Authorizer `own_tour` on #26–#28 (#2): `tours.execute_any`, or `tours.execute_own` and the
SR's pending stop is on the user's tour. Failing → hidden from `availableActions`, `403 not_own_tour`.
Missing reason / comment / recipient name / deviation comment → `422` (#34).

Subscriber: SR `cancel` (H1, any trigger) → pending stop deleted (planned/draft tour) or
`cancelled` (in-progress tour, #8); a future current appointment is cancelled at the provider
after commit (best effort).

## Templates and tasks

- `sr.protocol` (protocol PDF attached), `tour.assigned` (technician; on create-with-technician
  and on technician change).
- `appointments.sync` — queue `sync`, lock `appointments.sync`, periodic `5 * * * *` (UTC),
  creates a `background_jobs` row for periodic runs; manual runs via `POST /appointments/sync`.

## Seed (order 40)

Calendars 1–9; one booked current appointment per SR with `appointment_*` (matching exactly);
6 cancelled (sync-cancel history) + 2 superseded (on-site reschedule history) appointments for
the 8 `reschedule_needed` SRs; 5 unmatched appointments next week (reference typos / empty);
sandbox rows mirroring all of them and the sync token = max sandbox `change_seq`; tours for
every working day from Monday of D0's week (or wd(−1), whichever is earlier) to Friday of the
next week for every calendar with appointments — past days `completed` with outcomes taken
from the SR statuses, D0 Riverton = Theo, `planned`, exactly his 5 `provided` stops, next week
~30 % `draft`; protocol PDFs for every completed SR. Exported tables: `booking_calendars`,
`appointments`, `booking_sync_state`, `tours`, `tour_stops`.

## Decisions taken without the product owner

- **Superseded appointments** (#7): `appointments.is_current` + `superseded_at`. Every
  reschedule (#15/#16/#27) supersedes all bookings of the SR that are still booked, so the
  sync never revives them; new bookings arrive as new rows.
- **On-site reschedule seed rows** are `booked` + superseded (the provider booking stays,
  #7), the sync-cancel ones are `cancelled` (6 + 2 = the 8 history rows of §11.3).
- **Scans auto-start** a planned tour of today like the on-site actions (#32); scans on a tour
  of another day → `409 conflict`.
- **`complete_deployment`** releases unverified new devices and clears the item's asset (the
  device is back in stock), and generates the protocol in the same transaction.
- **Undo of a registered unknown device** is refused (`409 undo_not_possible`); the asset stays.
- **Tour auto-complete** also applies to planned tours whose last pending stop was completed
  manually (`complete_manually`), so no tour is left without pending stops and without an end.
- **Tour stops of in-progress tours** keep their outcome; reordering puts finished stops first.
- **Overview `appointmentCount`** counts booked, not superseded, matched appointments of that
  calendar/day (past days included).
