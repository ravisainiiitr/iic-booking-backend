# Phase 8B — Architecture Audit (READ-ONLY)

**Date:** 2026-08-22  
**Tree:** `iic-booking-backend-no-automigrate` + `iic-booking-frontend`  
**Production actions:** NONE (inspection only)

---

## Implementation status (Phase 8B)

Code + staging tests implemented in-tree. **Production T0 / migrate / blocks / cleanup / refunds: NOT activated.**

See also: `AI30-AI31-PHASE-8B-EQUIPMENT-MAPPING.md`, `AI30-AI31-PHASE-8B-BOOKING-BLOCKING.md`, `AI30-AI31-PHASE-8B-MIGRATION-RUNBOOK.md`.

---

## 1. Executive decision (do not invent a second conflict engine)

| Question | Finding |
|----------|---------|
| How does NEW portal occupancy work? | Discrete `DailySlot` rows (`status=AVAILABLE` → `BOOKED`), not continuous ranges on `Booking` |
| Canonical claim | `DailySlot.objects.select_for_update().filter(..., status=AVAILABLE)` in `_book_equipment_impl` |
| Separate `LegacyBookingBlock` alone? | **Insufficient** — create path never queries such a table |
| Recommended design | **Hybrid:** audit model `LegacyBookingBlock` **plus** mark matching `DailySlot` as `BLOCKED` with migration label so existing availability + `select_for_update` reject overlaps |

**Verdict for Phase 8B implementation:** Proceed with hybrid (metadata block + DailySlot.BLOCKED). Do **not** create fake `Booking` rows for protection. Do **not** invent a parallel overlap engine.

---

## 2. Models / services to REUSE

### Equipment / department / modes

| Symbol | Path |
|--------|------|
| `Equipment` (`equipment_id`, `code`, `name`, `internal_department`, `enable_multi_mode`, `parent_equipment`) | `iic_booking/equipment/models.py` |
| `EquipmentModeSchedule` / `ModeScheduleBehavior.EXCLUSIVE` | same |
| `family_slots_overlap_conflict` | `iic_booking/equipment/mode_utils.py` |
| `Department` / `DepartmentType` | `iic_booking/users/models/department.py` |
| `Laboratory` (sync UUID lab; **not** equipment scope) | `iic_booking/sync/models.py` |

### RBAC

| Role | `UserType` code | Assignment |
|------|-----------------|------------|
| Main Administrator | `admin` | global |
| OIC | `manager` | `EquipmentManager` |
| Lab-in-Charge | `operator` | `EquipmentOperator` |
| Dept Admin | `dept_admin` | department-scoped |
| Helpers | `get_equipment_ids_managed_by_oic` | `equipment/reports.py` |

### Booking / slots / conflict

| Symbol | Path |
|--------|------|
| `Booking`, `BookingStatus`, `DailySlot`, `SlotStatus` | `equipment/models.py` |
| Create API | `POST /api/equipments/<pk>/book/` → `book_equipment` / `_book_equipment_impl` (`equipment/api_views.py`) |
| Availability | `SlotAvailabilityChecker.is_slot_available` / `block_slot` (`equipment/slot_utils.py`) |
| TZ | `TIME_ZONE=Asia/Kolkata`, `USE_TZ=True` (`config/settings/base.py`) |
| Waitlist | side-effect of failed book → `add_user_to_waitlist` (`equipment/waitlist.py`) |
| Cancel / complete | `cancel_booking`, `user_cancel_booking`, `complete_booking` |

### Migration / freeze / Phase 8A

| Symbol | Path |
|--------|------|
| `PortalMigrationState.end_user_booking_enabled` | `users/models/portal_migration.py` |
| `end_user_booking_is_locked` / `PORTAL_BOOKING_LOCKED` | `users/legacy_ledger/booking_lock.py` (enforced in `_book_equipment_impl`) |
| `MigrationBookingSettlement` + `issue_migration_refund` | Phase 8A — **exists** |
| `LegacyBookingHistoryRecord` | archive-only; never slots/billing |
| `OldMySQLReader` | wallet tables verified; `booking` table existence probed only |

### Test accounts

| Symbol | Path |
|--------|------|
| `User.is_test_account` | explicit flag — **required** for cleanup |
| `clear_test_account_data` | `users/management/commands/clear_test_account_data.py` (`--confirm CLEAR_TEST_ACCOUNT_DATA`) |
| `seed_test_users` | `users/management/commands/seed_test_users.py` |

### Frontend

| Component | Path |
|-----------|------|
| Book calendar | `iic-booking-frontend/src/pages/BookEquipment.tsx` |
| API | `apiClient.bookEquipment` / `getEquipmentSlots` |
| Migration admin UI | `pages/AdminPortalMigration.tsx` |
| Phase 8A settlement UI | `BookingDetailCard.tsx` |

---

## 3. What does NOT exist (must create or stay out of scope)

| Item | Status |
|------|--------|
| `LegacyEquipmentMapping` | **Missing** — create explicit mapping model |
| `LegacyBookingBlock` | **Missing** — create hybrid metadata + DailySlot.BLOCKED |
| `LegacyBookingMigrationBatch` | **Missing** — create |
| Old-portal PHP/Django codebase under `D:\IIC_NEW` | **Absent** — old portal is **external** Legacy MySQL / separate app |
| Verified legacy `booking` column contract (start/end/equipment) | **NOT established in code** — only table name `booking` verified present |

---

## 4. Architecture / requirement conflicts (STOP points)

### C1 — Old portal freeze cannot be enforced inside this repo

**Requirement:** At T0, OLD portal `new booking = BLOCKED`.

**Reality:** This codebase is the **NEW** portal. Old portal booking APIs are not hosted here.  
`PortalMigrationState.end_user_booking_enabled=False` only freezes **new-portal end-user** booking.

**Resolution for Phase 8B code:**  
- Implement NEW portal freeze/enable semantics and migration mode state.  
- Document OLD portal freeze as an **external operational step** (old-app config / DNS / infra).  
- Do **not** claim this Django app alone can return `MIGRATION_BOOKING_DISABLED` from the old portal process.

### C2 — Legacy booking schema columns unknown

**Requirement:** Discover old bookings by window on start/end.

**Reality:** `OldMySQLReader.discover_schema()` marks `booking` table VERIFIED if present, but does **not** map columns for equipment, start, end, status, amount.

**Resolution:**  
- Discovery accepts **fixture/JSON rows** and optional MySQL after a **schema probe** that requires configured column map settings.  
- Production discovery against live MySQL is **NOT READY** until column map is operator-confirmed.  
- Tests use fixtures only (no invented production SQL).

### C3 — Phase 8A refund vs old MySQL bookings

Phase 8A `MigrationBookingSettlement` attaches to **`equipment.Booking`** (new portal).  
It does **not** refund arbitrary old-MySQL rows. Compatibility: keep Phase 8A for new-portal bookings during freeze; legacy MySQL financial settlement remains wallet-ledger / operator process unless a later phase maps old IDs into new `Booking` (explicitly out of 8B scope for blind copy).

### C4 — Blind copy into `equipment.Booking` forbidden by existing design

`LegacyBookingHistoryRecord` / `migrate_legacy_bookings` (main tree) explicitly: **never write old IDs into `equipment.Booking`**.  
Phase 8B preference for blocks over copy **aligns** with existing architecture.

---

## 5. Planned implementation shape (post-audit)

1. `LegacyEquipmentMapping` — explicit OLD→NEW, statuses ACTIVE/UNMAPPED/DISABLED/CONFLICT/RETIRED  
2. `LegacyBookingMigrationBatch` — DRAFT→…→ABORTED audit  
3. `LegacyBookingBlock` — ACTIVE/RELEASED/CONFLICT/CANCELLED + `slot_ids` JSON  
4. Arm path: create block + `DailySlot.status=BLOCKED`, `blocked_label=LEGACY_MIGRATION:{id}`  
5. Create-booking: when slots unavailable due to migration BLOCKED → `409 LEGACY_MIGRATION_SLOT_BLOCKED`  
6. Extend `PortalMigrationState` with window + `booking_migration_mode` + `new_portal_url`  
7. Commands: validate mapping, discover (fixture), dry-run, reconcile, abort, cleanup alias  
8. Admin report APIs for Main Administrator global view  
9. Tests with fixtures; reuse Phase 8A refund tests compatibility  

**Production:** no migrate, no T0, no blocks, no cleanup, no refunds.

---

## 6. Audit status

| Gate | Result |
|------|--------|
| Reuse inventory complete | **YES** |
| Conflict engine reuse strategy | **Hybrid DailySlot.BLOCKED** |
| Blocking unknowns documented | **C1–C4** |
| Proceed with code (non-production) | **YES — with hybrid + fixture discovery** |

**PHASE 8B.0 = COMPLETE**
