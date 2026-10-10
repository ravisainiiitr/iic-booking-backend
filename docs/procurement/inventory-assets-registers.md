# Procurement & Assets — registers, inventory, requirements and maintenance (design note)

Branch `feature/inventory-procurement-assets` (backend + frontend). Extends the existing
`iic_booking.procurement_management` app; nothing is duplicated and existing data keeps working.

## What already existed (origin/master)

| Area | Existing |
| --- | --- |
| Module gate | Per-department switch + pilot allow-list, staff user types only (`access.py`). |
| Roles | Main Admin, OIC, Lab Operator, HOD (derived from portal data); OC Stores, Office (granular permissions), HOD, Auditor (assignments). |
| Requests (indents) | Draft → OIC → OC Stores (available / partial issue / not available) → HOD (in-app or offline signed) → approved → procurement record (indent, RFQ, quotations, comparative, PO, delivery, inspection, invoice, payment, stock / asset entry) → completed. Append-only approval history, audit log, documents, in-app notifications (email only when templates exist). |
| Asset register | `Asset` (number, category, equipment, lab, serial, tag, cost, vendor, warranty, custodian, status) + status history + controlled transfers. No physical register reference, no verification, no import. |
| Stock | `StockBalance` per (department, store) + append-only `StockTransaction` ledger (opening, receipt, issue, return, adjustments); receipts posted from bills; issues posted from requests. |
| AMC, budget, plan/non-plan proposals, reports | Present. |
| Disruption log | `equipment.DisruptionEvent` (downtime start/end, cause, action taken, service reports, linked procurement request ids). Resume dialog can raise a free-text request (consumables / minor / major). |
| Legacy | `equipment.InventoryItem` / `EquipmentInventoryItem` (older per-equipment inventory) — kept; `Item.legacy_inventory_item` links them. |

## Design decisions

* **One app, one additive migration** (`procurement_management/0003_inventory_registers`). No other branch touches
  this app, so the number is conflict-free; the `equipment` app is not migrated (other branches use 0245+).
  All new columns are nullable or have defaults, so old code runs on the new schema and new code tolerates rows
  written by old code.
* **Physical register identification**: `AssetRegister` (register book: type MAJOR / MINOR / LLTA / CONSUMABLE, code,
  name, volume, lab, custodian) and on `Asset`: `register`, `register_page`, `register_serial`, `register_entry_date`.
  `(register, page, serial)` is unique among live rows (DB constraint) — the duplicate key for import.
  Search `REG/page/serial` (e.g. `MAJ-1/12/3`) matches instantly.
* **Asset tag** auto-generated when blank (`<DEPT>/<TYPE>/<id>`), printable QR labels (PDF, reportlab QR) that open
  `/procurement/scan/<tag>` on any phone camera → asset card → *Mark verified*.
* **Accessories / sub-assets**: `Asset.parent` (an equipment's main asset + accessories).
* **GFR fields**: supplier text (when no vendor master row), PO / invoice no. & date for legacy entries, funding
  source, project code, installation date, AMC until, condition, quantity (dead-stock lines), legacy ref,
  optional depreciation inputs (rate, useful life — straight-line book value shown; full depreciation schedules
  deferred).
* **Physical verification** (GFR Rule 213): `VerificationCampaign` (annual, optional register filter) and append-only
  `AssetVerification` (date, verifier, result FOUND / NOT_FOUND / DAMAGED / SHORTAGE, condition, location seen,
  remarks, method SCAN / MANUAL). Latest result is denormalised on the asset for filtering.
* **Disposal / write-off** (GFR Rules 214–217): append-only `AssetDisposal` (condemn, write-off, dispose: board and
  sanction references, mode — auction / scrap / buy-back / transfer / write-off, realised value) driving the existing
  status machine.
* **Item ↔ equipment linkage**: `ItemEquipmentLink` (consumable / spare / accessory, typical quantity). Suggested lines
  for an equipment = linked items + legacy `EquipmentInventoryItem` mappings, with stock on hand (central + labs),
  reorder flags and last receipt price.
* **Stock**: batch no., expiry date, reason code (damaged / expired / count correction / lost / other), equipment and
  maintenance links on ledger rows. Low-stock = below min or at/below reorder level.
* **Store In Charge line editing**: at the OC Stores stage the store person can change quantities, substitute items,
  add/remove lines and mark each line *from stock* or *to procure* (original values kept on the line and shown to the
  requester). The existing available / partial / not-available decision then issues the *from stock* lines.
* **Accounts budget check**: new assignable role **Accounts In Charge** (`ACCOUNTS`: invoices, payments, budget,
  reports). Optional approval stage `ACCOUNTS` (switch `accounts_budget_check`, off by default → existing routes
  unchanged) placed before HOD. Bills can be *forwarded to Accounts*; Accounts dashboard lists bills pending.
* **Lab In Charge**: new assignable role `LAB_INCHARGE` with an equipment scope (`equipment_ids`, empty = every
  equipment of the department); behaves like lab staff for those equipment (raise, view assets/stock/AMC, record
  maintenance, verify).
* **Purchase mode** on procurement records (Direct, GeM, Purchase Committee, Limited / Open / Single tender,
  Proprietary, Rate contract) with configurable GFR thresholds (direct ≤ ₹50,000; purchase committee ≤ ₹10 lakh;
  limited tender ≤ ₹50 lakh) → suggested mode; GeM order / contract reference fields.
* **Maintenance history**: `MaintenanceRecord` per equipment (kind, downtime start/end, cause, action, service
  provider, service / other cost, parts used = stock issues linked to the record, linked disruption event, AMC
  record, follow-up requests). Created from the *back to Operational* dialog or any time from the equipment page.
* **Back-to-functional hook**: the existing resume dialog gains "Record maintenance & parts used" and the request
  step gains *Fill from this equipment's inventory* (auto-suggested lines with stock) and request types
  Repair / Service / AMC besides consumables / minor / major. The request is linked to both the disruption and the
  maintenance record. *Raise requirement* is also on the equipment page for OIC / Lab In Charge any time.
* **SLA ageing**: pending requests bucketed by days since entering the current stage (0–3, 4–7, 8–15, > 15).
* **Exports** use the module's `table_response` (the shared table/export restyle adds S.No., centred cells);
  printable GFR-style register (PDF, grouped by page) and QR label sheets are separate renderers.

## MVP scope delivered vs deferred

Delivered: registers + verification + disposal records, bulk import (template, preview, duplicates, commit),
register print, QR labels, item–equipment links and suggestions, stock batch/expiry/reasons, store line editing,
Accounts role + optional budget-check stage, bill forwarding, purchase mode + thresholds, Lab In Charge scope,
maintenance records with parts from stock, back-to-functional hook, equipment overview, role dashboards with ageing.

Deferred (clear list):
1. Depreciation schedules (WDV/SLM per FY, year-end runs) — only inputs + straight-line book value now.
2. GeM API integration (order pull, CRAC/consignee receipts) — manual GeM references only.
3. Tender module (e-procurement CPPP publishing, bid evaluation).
4. Barcode/QR scanning inside the app via camera API on all browsers — phones scan with the camera app (URL in QR);
   in-app scan uses `BarcodeDetector` where the browser supports it, manual tag entry otherwise.
5. Batch-wise FEFO stock issue (batches recorded, issue not batch-allocated).
6. Inter-department asset transfers and Board-of-Survey multi-member e-sign.
7. Email templates for the new events (in-app notifications work; email starts once `procurement_*` templates exist).
