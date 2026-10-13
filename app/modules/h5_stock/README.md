# `h5_stock` — Stock, Inventory & Dashboard

Stock matrix + buffers + recalculation, call-off orders to Initech Logistics, assets
(list/detail/ledger/corrections/export + the asset actions), deactivation uploads,
inventories (stock-takes with scan station and reconciliation) and the dashboard KPIs.
Spec: SPEC §2.5 (H5 row), §4.6, §4.8, §5.6, §6.7–§6.10, §6.20, §9, §10, §11.7;
API.md §8; SPEC_RESOLUTIONS #18, #23, #24, #25, #27, #28.

## Tables (migration `h5_0001_stock`, branch `h5`)

| Table | Notes |
|---|---|
| `inventories` | partial unique `(site_id) WHERE status='open'`; hubs **and** Campus (#28). **Extra column** `reconciliation jsonb` — the reconciliation frozen on close/cancel (the expected set is live while open, so closed inventories need a snapshot). |
| `inventory_scans` | unique `(inventory_id, serial_number)`; result `matched` / `wrong_location` / `unknown`. |
| `calloff_orders`, `calloff_order_lines` | number `CO-{site}-{YYYYMMDD}-{nn}`; lines snapshot demand/stock/buffer. |
| `deactivation_batches` | **Extra columns** `closed_lease_return_count`, `sample jsonb` — parts of `DeactivationResult` that cannot be recomputed later. |

## Endpoints (`/api/v1`)

| Path | Permission |
|---|---|
| `GET /stock/matrix` · `POST /stock/recalculate` (202) · `PATCH /stock/buffers` · `POST /stock/export` | `stock.read` · `stock.recalculate` · `stock.buffers` · `assets.export` |
| `GET/POST /calloff-orders`, `GET/PATCH /calloff-orders/{id}`, `POST …/actions/{send,cancel}`, `GET …/document?format=xlsx\|pdf` | `stock.read` / `calloff.manage` |
| `GET /assets`, `/assets/{id}`, `/assets/by-serial/{serial}`, `/assets/{id}/movements`, `PATCH /assets/{id}`, `POST /assets/export` | `stock.read` / `assets.update` / `assets.export` |
| `POST /deactivations` (multipart), `GET /deactivations`, `GET /deactivations/sample`, `GET /deactivations/{id}` | `assets.deactivate` / `stock.read` |
| `GET/POST /inventories`, `GET /inventories/{id}`, `POST/GET …/scans`, `DELETE …/scans/{scanId}`, `GET …/reconciliation`, `POST …/actions/{close,cancel}`, `GET …/reports/{kind}` | `inventory.read` / `.scan` / `.manage` |
| `GET /dashboard/kpis?siteId=&regionId=` | `dashboard.read` |

## Actions, guards, codes

- Asset actions on the core endpoints `POST /assets/{id}/actions/{action}` (+ bulk):
  `mark_missing {reason?}` (bulk), `mark_found {locationId, installationPlace?}`,
  `correct {status?, locationId?, installationPlace?, reason}` (bulk, reason required → 422).
  `deactivate` is **not registered** — it only runs inside `POST /deactivations` (#24), so it
  never appears in `availableActions`.
- Call-off `send` guards → `409 guard_failed`: `no_lines`, `delivery_date_past`.
  Non-draft send/cancel → `409 transition_not_allowed`; PATCH of a non-draft → `409 conflict`.
- Inventory: open twice → `409 inventory_already_open`; scan into a closed/cancelled
  inventory → `409 conflict`; undo after close → `409 undo_not_possible`; undo of someone
  else's scan without `inventory.manage` → `403`; close with `countDate` in the future → `422`.
- Scan codes (verdicts per core `SCAN_CODES`, #25): `matched` ok, `duplicate` **warning**
  (no row stored), `unknown_recorded` warning (stored `unknown`), `wrong_zone` / `wrong_site`
  warning (stored `wrong_location`). Serials *or asset tags* are accepted.

## Reconciliation and corrections

Expected set E = assets at the site (goods in, staging, goods out, returns), status not
deactivated (missing included). Categories: matched (scan matched, asset still in E),
missing (E minus scanned assets), wrong location, unknown. With `applyCorrections`:
missing `available`/`collected` → `mark_missing`; wrong-location `available`/`collected`/
`missing` → `inventory_relocate` to the scanned zone (else hub goods-in for stock statuses /
returns for collected and for Campus); matched assets in status `missing` →
`inventory_relocate` to their recorded location (#27). Workflow and deactivated assets are
only reported. Counts are frozen **before** corrections. Close stores 3 CSV reports and
mails `inventory.report` to the team mailbox.

## Templates · tasks · events

- `calloff.order` (to `recipient_email`, cc team mailbox, XLSX + PDF attached),
  `inventory.report` (team mailbox, 3 CSVs). Both have `build_context` for previews.
- `stock.recalculate`: periodic `20 * * * *` (UTC), lock `stock.recalculate`. Manual runs get
  their job row from `POST /stock/recalculate`; periodic and debounced runs create one.
  Debounced (60 s, queueing lock `stock.recalculate.debounce`) after `ImportFinished`
  (`service_requests`/`assets_new`/`asset_status_sync`), deactivation upload and inventory
  close. Emits `StockRecalculated`.
- Lease returns of deactivated devices are closed through `TransitionService` (`close`,
  trigger `system`, comment `device_deactivated`, payload `{closeReason, deactivationBatchId}`).

## Seed (order 50)

Inventories with the fixed ids of `SEED_INVENTORY_IDS` (EAS 1 closed T−60, RIV 2 closed T−30 —
historic expected sets rebuilt from the movement ledger, missing devices = the core's
`inventory:{id}` movements; HIL 3 open since wd(−1) 09:00, ~60 % scanned by Tara, 3 wrong
location, 2 unknown). 9 sent call-offs (T−28/−14/−7 per hub, documents stored, last order
dates set) + an Eastgate draft from today's matrix. Deactivation batches 1 (T−20, 30 + 2 not
found) and 2 (T−6, 6) matching the core movements `deactivation:{id}`. Then a recalculation.
Exporter: all five tables; demo truncate: all five tables.

## Decisions taken without the product owner

- Call-off create/PATCH reject a past delivery date with `422` (send still guards it, since a
  draft can age).
- Call-off cancel/inventory cancel comments are appended to `note` (no extra column).
- `GET /deactivations/sample` returns a `DownloadLink` to a CSV with five currently collected
  devices and one unknown serial (permission `assets.deactivate`).
- Deactivation rows match by serial, falling back to the asset tag.
- Dashboard: `siteId` filters SRs by supplying hub, stock by hub, inventories by site;
  `regionId` filters SRs. Lease tiles are global (leases have no hub/region). Appointment
  tiles/series count SRs in `WITH_APPT ∪ {completed}` by business date of
  `appointment_start_at`; stage counts include deployed/closed of the last 14 days;
  `lastImportAt` = latest of import job finish / SR import / lease import;
  `lastAppointmentSyncAt` = latest succeeded `appointments_sync` job (H4 table not readable).
- Stock matrix `belowMin`/`aboveMax` compare current stock with min/max; call-off line
  warnings use stock + quantity (SPEC §6.8).
- Test workaround: core `tests/test_actions.py` unregisters the asset actions `mark_missing`
  and `correct` it overrides; `tests/conftest.py` re-registers the H5 handlers (shared-file
  request filed).
