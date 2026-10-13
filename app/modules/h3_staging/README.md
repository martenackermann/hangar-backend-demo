# `h3_staging` — Staging & Fulfilment

Installation places, personalization, order closing, picking, parcel shipping (SPEC §2.5 H3,
§6.6, §6.15–6.17; API.md §6; SPEC_RESOLUTIONS #15, #25, #35).

## Tables
- `shipments` (migration `h3_0001_staging`, branch `h3`): one row per carrier shipment —
  tracking + return tracking URLs, options, recipient snapshot, label file (`stored_files`).

## Endpoints (`/api/v1`)
| Method & path | Permission | Notes |
|---|---|---|
| `POST /scan/installation-place` | `staging.scan` | one request per (serial, place); verdicts per SPEC §6.16 |
| `POST /scan/installation-place/undo` | `staging.scan` | `undone`, or `409 undo_not_possible` |
| `GET /staging/installation-places?siteId&q` | `stock.read` | occupied places (place prefix `q`) |
| `GET /personalization/queue` | `sr.read` | `siteId` required; sorted by appointment |
| `GET /service-requests/{id}/asset-candidates?slot` | `sr.read` | FIFO per supplying hub + article (max 50) |
| `POST/DELETE /service-requests/{id}/items/{slot}/assignment` | `staging.personalize` | assign by scan / unassign |
| `PATCH /service-requests/{id}/staging` | `staging.personalize` | `osReinstall` (+ optional `addedSoftware`) |
| `GET/PUT /service-requests/{id}/software-checklist` | `sr.read` / `staging.personalize` or `staging.close` | SPEC §6.15 |
| `GET /order-closing/queue` | `sr.read` | `siteId`, `regionId`, `checklistComplete`, … |
| `GET /picking/technician?siteId&date` | `sr.read` | grouped by denormalized calendar |
| `GET /picking/parcel` | `sr.read` | ready parcel orders + ship-to |
| `POST /scan/parcel`, `DELETE …/items/{slot}/packing` | `fulfilment.ship` | packing marks |
| `POST /service-requests/{id}/shipments` | `fulfilment.ship` | transition `ship` via ParcelCarrier |
| `GET /service-requests/{id}/shipments`, `GET /shipments` | `sr.read` | |
| `GET /shipments/{id}/label` | `fulfilment.ship` | signed download link |

## Actions (generic endpoint, effects in `effects.py`)
`claim` (not bulk), `release`, `finish_personalization`, `skip_preparation`,
`reopen_personalization`, `close_order`, `mark_provided` (bulk), `close_without_label`
(comment required). `ship` is registered only so it appears in `availableActions`; the map
marks it `via_endpoint=False` → the generic endpoint answers `404 unknown_action`.
All guards are core guards (`has_appointment`, `is_editor`, `is_editor_or_admin`,
`all_new_items_assigned`, `software_checklist_complete`, `delivery_type_is_*`,
`all_new_items_packed`); H3 registers none of its own.

### Assignment errors (SPEC_RESOLUTIONS #35)
| Case | Response |
|---|---|
| SR not `personalization_in_progress` | `409 transition_not_allowed` |
| actor is not the editor | `409 guard_failed`, guard `is_editor` |
| `old_*` slot | `422 slot_not_assignable` |
| SR has no item in that new slot | `404 not_found` |
| scan misses (unknown serial, wrong article, …) | `200` with verdict (SPEC §6.6) |

## Templates
`sr.shipping_confirmation` (subject "Your hardware for {srNumber} is on its way").

## Tasks
None.

## Seed (order 30)
A `shipments` row + label PDF for every SR with a `ship` transition in the core story
(shipped, reschedule-from-shipped, completed/cancelled after shipping). Carrier ids
`PC<ship date><900001…>`, ~25 % combined parcels. Exporter: `shipments`; demo truncate:
`shipments`.

## Decisions taken without the product owner
- Scan verdicts come from the single core table `SCAN_CODES` (#25). Consequence: an
  assignment scan of a device outside the supplying hub's staging returns `wrong_location`
  with verdict **warning** (not applied) — SPEC §6.6 lists it as an error, #25 wins.
- Re-scanning the device already assigned to the slot returns `unchanged` (ok, not applied).
- Scan stations accept an asset tag where a serial is expected (labels carry both).
- Undo of an installation-place scan that came from goods-in is recorded as a `correct`
  movement with reason "Undo of installation place scan (movement N)" — no other movement
  action moves an available asset back to goods-in. Undo back to a previous place uses `place`.
- `PATCH …/staging` also accepts optional `addedSoftware` (same normalization as the checklist
  PUT); omitted fields stay unchanged. Allowed in `appointment_booked`,
  `personalization_in_progress` (editor only) and `personalization_done`; else `409 conflict`.
- Packing scans on an SR that is not `ready_for_picking` → `409 conflict`; on a technician
  SR → `409 guard_failed delivery_type_is_parcel`.
- Shipment creation: guards are evaluated before the carrier is called; a `CarrierError` is
  a `502 carrier_error` with nothing written. Missing delivery address → `409 conflict`.
  Without old items a requested return label yields warning `no_return_items`;
  `sendConfirmation` without contact email yields warning `no_contact_email`.
- Parcel weight: article `weight_kg` per item (1.0 kg if unknown); combined parcel = sum, min
  0.5 kg; return parcels 2.0 kg each, addressed to the supplying hub.
- `GET /picking/technician`: `siteId` required (response carries a `SiteRef`);
  `GET /picking/parcel`: `siteId` optional (all hubs), max 200 rows.
- ⌘K: carrier shipment ids (`PC…`) resolve to their SR (search provider).
