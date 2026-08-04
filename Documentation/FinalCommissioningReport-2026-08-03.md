# Final Commissioning Report — Remote Analysis (2026-08-03)

## Overall

**Automated commissioning: PASS** (after fleet merge + RAW/RESULTS config)

**Live desktop E2E: NOT COMPLETED** — requires interactive user session on Analysis PC.

**Git commits: DEFERRED** per gate (live E2E + sustained ONLINE fleet).

---

## Completed this phase

| Item | Result |
|------|--------|
| Persistent Machine ID (MachineGuid + BIOS UUID fingerprint) | Agent + Portal registration reconnect |
| Duplicate RAVI merge (5 → 1 enabled + 4 archived) | Done |
| Fleet inventory + duplicates APIs | `/api/v1/analysis/fleet/inventory/`, `/fleet/duplicates/` |
| Reservation check-in (`AWAITING_CHECKIN`, start/release APIs) | Implemented |
| Equipment check-in policy fields | Migration 0184 |
| Production commissioning runner | `/api/v1/analysis/commissioning/run/` → PASS |
| RAW/RESULTS on PXRD [A] | `D:\PXRD\RAW`, `D:\PXRD\RESULTS` |
| Docker image rebuild + recreate | `iic_booking_production_django` rebuilt; containers recreated from image |
| Migrations 0019, 0020, 0184 | Applied |

## Commissioning checklist (latest PASS)

- Database PASS
- Cache/Redis PASS
- Reverse Tunnel Config PASS
- Heartbeat 1/1 enabled online PASS
- Duplicate Workstations PASS
- Tunnel Orphans PASS
- RAW/RESULTS Config PASS
- Guacamole (mock=false) PASS
- End/Start Analysis APIs PASS
- Scheduler Extensions (Maintenance + Check-in) PASS

## Remaining before commits

1. Deploy updated **RemoteAnalysis.Agent** (MachineGuid identity) to the Analysis PC and confirm re-registration does not create duplicates.
2. Complete one **live** booking → check-in → desktop → End Analysis → S3 → email.
3. Confirm optional required-software mapping for PXRD if CasaXPS (or instrument software) is needed.
4. Rebuild image once more after any post-PASS hotfixes so **zero** docker cp remains on running hosts.
5. Document Windows GPO lockdown for shared Analysis PCs (guide drafted).

## APIs added

- `POST /api/v1/bookings/{id}/analysis/start/`
- `POST /api/v1/bookings/{id}/analysis/release/`
- `GET /api/v1/analysis/fleet/inventory/`
- `GET|POST /api/v1/analysis/fleet/duplicates/`
- `GET /api/v1/analysis/equipment/config-audit/`
- `GET|POST /api/v1/analysis/commissioning/run/`

## Docs

- `Documentation/FleetManagementGuide.md`
- `Documentation/ReservationCheckinGuide.md`
- `Documentation/CommissioningGuide.md`
