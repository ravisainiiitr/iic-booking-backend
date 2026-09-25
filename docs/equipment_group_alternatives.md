# Equipment Group: alternative equipment and cross-equipment rescheduling

The existing `EquipmentGroup` (used for group quotas) can also act as a pool of interchangeable
equipment. Two capabilities are available, both **off by default**:

1. **Alternative equipment during new booking.** When the chosen slot is not available, the user is
   offered equipment from the same group *before* being put on the waitlist.
2. **Cross-equipment rescheduling.** A booking can be moved to another equipment of the same group
   while rescheduling.

Everything reuses the existing booking and reschedule code paths: visibility, department locks,
equipment status, charge profiles, home-department slot rules, multi-mode schedules, quotas, the
external slot quota, and the time/charge engines. No new booking engine is introduced.

## Business rules

An Equipment Group is a pool of interchangeable equipment within a single department. It widens
equipment choice without changing the existing financial, departmental, quota, permission or booking
rules.

**Pricing.** All equipment within an Equipment Group are treated as having the same charge. Cross-equipment rescheduling does not create a price difference, additional charge, refund adjustment, or wallet adjustment solely because the equipment changes.

- The existing charge system stays authoritative. A cross-equipment reschedule computes the
  booking's charge on its current equipment and on the target (existing charge engine, booking /
  mapped inputs). If the amounts differ, the target is rejected with `CHARGE_MISMATCH`; if either
  cannot be computed, with `CHARGE_NOT_VERIFIED`. Nothing is ever adjusted, debited or refunded.
- Full and partial cancellation after a reschedule use the existing logic unchanged (full refund of
  the charged amount; partial recalculation with the booking's charge profile, which carries the
  same charge on every member).
- A new booking on an alternative is priced by the target's own charge calculation, as any booking.

**Department.** All equipment within an Equipment Group must belong to the same department. Cross-department Equipment Groups are not supported.

- Enforced server-side on every membership write path with the message "All equipment in an
  Equipment Group must belong to the same department.": admin API group update (`equipment_ids`,
  add/replace), admin API equipment create/update (`equipment_group`, or `internal_department` of a
  grouped equipment), Django admin group inline, equipment form and the bulk "assign to group"
  action. Group create through the admin API does not accept members. Equipment without a
  department counts as its own department.
- Validation runs when membership or department changes, so existing groups are never modified
  automatically and stay editable; a change that would leave a group spanning departments is
  rejected. The admin group editor only offers equipment of the group's department.
- Cross-equipment rescheduling defensively rejects a target from another department
  (`DEPARTMENT_MISMATCH`), and group members from another department are never offered as
  alternatives or reschedule targets (protection against legacy data).

**Settlement.** Cross-equipment rescheduling within an Equipment Group does not transfer settlement between departments.
`settlement_department` is not changed, and no transfer or ledger entry is created.

**Virtual booking id.** virtual_booking_id is preserved when a booking moves between equipment within the same Equipment Group, subject to existing system behavior.

## Switches

Each capability needs its global environment flag **and** its per-group switch.

| Environment flag (default `False`) | Group switch (default off) | Effect |
| --- | --- | --- |
| `EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED` | `alternative_booking_enabled` | Offer alternatives before the waitlist |
| (same flag) | `alternative_search_other_slots` | Also offer the earliest slot on other equipment when the same slot is not free |
| `EQUIPMENT_GROUP_AUTO_ALLOCATION_ENABLED` (also needs the alternative flag) | `auto_allocation_enabled` | Book the first viable alternative automatically instead of asking |
| `EQUIPMENT_GROUP_CROSS_RESCHEDULING_ENABLED` | `cross_rescheduling_enabled` | Allow moving a booking to another group member while rescheduling |

Per-equipment `alternative_priority` (default 100; lower is offered first) orders the pool.

Group switches are edited in Django admin (Equipment Groups, "Alternative equipment pool"; the
fieldset also shows the current state of the environment flags) or through the admin API by the Main
Administrator only (`user_type == admin` or a superuser; other admin-panel roles cannot change them,
even when they have no department scope). Switch changes take effect on the next request. Environment
flags go in the backend `.env` and need a restart of the django and celery containers.

## Alternative equipment during booking

- The frontend sends `offer_group_alternatives: true` to `POST /api/equipments/<id>/book/` only
  when the equipment detail reports `group_alternatives_enabled`. Older clients and the Research
  Copilot never send it, so their behaviour is unchanged.
- When the booking fails because the slot is unavailable, the waitlist step is deferred and
  same-group alternatives are searched (read-only: no locks, no quota consumption, no slot changes).
  - Eligibility per member: operational, not 3D print, visible to the user, department booking not
    locked, external share open for external users, active charge profile for the user type.
  - Slots must pass the same checks as the booking path (availability checker, home-department
    rule, weekly time window, multi-mode schedule and family overlap).
  - Each member is searched only inside its own bookable slot window for this user; an exact-time
    match outside that window is not offered.
  - Ordering: exact requested window first, then earliest start, then `alternative_priority`, then
    `equipment_id`. At most five results.
  - Inputs are carried over only when the target has a field with the same key, type and label (or
    the same type and label under another key). Choice values must be valid options on the target;
    companion keys (for example `B_other`) are dropped together with their base field. Table,
    periodic-table and ICPMS coverage fields are only carried over when key, options and linked field
    are identical. Numeric limits are validated with the booking validator. Missing required fields
    are reported, never guessed.
  - The estimated charge is recalculated with the target's charge profile.
- Response when alternatives exist: HTTP 409 with `code: "GROUP_ALTERNATIVES_AVAILABLE"`,
  `original_error`, `original_equipment` and `alternatives`.
  - **Book this equipment** posts a normal booking to the alternative with
    `alternative_of_equipment_id`. The server re-validates everything (slots locked, pricing, quota,
    wallet).
  - **Review in booking form / Complete details** opens the booking page for the alternative with
    compatible inputs prefilled and the offered week selected.
  - **Continue to waitlist** repeats the original request with `skip_group_alternatives: true`, so
    the existing waitlist logic runs unchanged.
- No alternatives: the existing waitlist response is returned exactly as before.
- Peak waitlist periods, urgent hold requests and `skip_group_alternatives` bypass the feature.
- Auto-allocation (when enabled) books the first alternative with complete inputs through the same
  booking implementation. The response carries `allocated_alternative` and the success dialog tells
  the user which equipment was allocated. If that attempt loses a race, the user is not waitlisted
  on the alternative; the outcome falls back to the remaining alternatives or the original
  equipment's waitlist.
- Audit: the booking's CREATED `BookingEvent` metadata records `equipment_group_alternative`,
  `alternative_of_equipment_id/code/name`, `auto_allocated` and `equipment_group_id`. The source id
  sent by the client is only recorded after it is verified to be a same-group member with the
  feature on.

## Cross-equipment rescheduling

- `GET /api/bookings/<id>/reschedule-options/` (booking owner or operator/OIC/admin) returns the
  original equipment plus selectable same-group members, each with the number of slots it needs.
- Both reschedule endpoints (`/reschedule/` for staff, `/user-reschedule/` for owners) accept an
  optional `target_equipment_id`. When it is absent or equals the current equipment, the existing
  code runs untouched.
- The cross-equipment branch runs after each endpoint's own permission, status, deadline and
  maintenance checks. It then:
  - re-validates flags, same group, same department, target eligibility for the booking owner,
    input compatibility and same charge (a different group is always rejected);
  - applies the same slot rules as the endpoint it came through;
  - requires exactly the target's slot count, consecutive;
  - checks group/equipment quota (owner endpoint) and the external slot quota against the target;
  - inside a transaction, locks the booking row and then the target slots (`select_for_update`).
    If the booking changed meanwhile (status, equipment, slots, inputs or charge — for example a
    concurrent cancellation) it returns 409 `BOOKING_CHANGED`; if a slot was taken it returns 409
    `SLOT_TAKEN`. In both cases nothing is changed.
- On success the booking's equipment, charge profile and mapped input values change. **The charged
  amount, amount due, wallet debit, `settlement_department` and `virtual_booking_id` are not
  changed.** The RESCHEDULED event metadata records previous/new equipment, times, charge profile,
  previous inputs, dropped fields, `charged_amount`, and the verified `source_charge_reference` and
  `target_charge_reference`. The existing notification email is sent, and the waitlist of the
  original equipment is notified about the released slots.

Error codes: `CROSS_RESCHEDULING_DISABLED`, `DIFFERENT_GROUP`, `DEPARTMENT_MISMATCH`,
`TARGET_NOT_ELIGIBLE`, `INPUTS_INCOMPATIBLE`, `CHARGE_MISMATCH`, `CHARGE_NOT_VERIFIED`,
`SLOT_COUNT_MISMATCH`, `SLOTS_NOT_CONSECUTIVE`, `SLOT_UNAVAILABLE`, `QUOTA_EXCEEDED`,
`QUOTA_CHECK_FAILED`, `SLOT_TAKEN` (409), `BOOKING_CHANGED` (409), `TARGET_INVALID`.

## Logging

Logger `iic_booking.equipment_group` emits `equipment_group.alternative_search`,
`alternatives_found`, `auto_allocated`, `auto_allocation_failed`, `cross_reschedule_rejected`,
`cross_reschedule_slot_taken`, `cross_reschedule_booking_changed` and `cross_reschedule_done` with
equipment/booking ids only.

## Rollback

Fastest: untick the group switches in Django admin (effective immediately, no restart). Global:
set the environment flags to `False` and restart the django and celery containers. With the flags
off:

- `book_equipment` calls the original implementation directly, and the waitlist decision is
  unchanged;
- a `target_equipment_id` on reschedule is rejected with `CROSS_RESCHEDULING_DISABLED`, and
  `reschedule-options` returns only the original equipment;
- the frontend hides the alternatives dialog and the equipment selector (both are driven by server
  responses).

No database restore or data deletion is needed. Migration `0197_equipment_group_alternatives` only
adds five `NOT NULL` columns with database-level defaults (`db_default`: four group booleans `false`,
`Equipment.alternative_priority` `100`). On PostgreSQL this is `ALTER TABLE ... ADD COLUMN ...
DEFAULT ... NOT NULL` (metadata-only on PostgreSQL 11+, no table rewrite). Because the defaults stay
in the database, a previous backend release can keep running against the migrated schema.
Bookings moved or created through the features remain ordinary bookings on their current equipment.
If the columns ever had to be removed, reverse the migration with
`manage.py migrate equipment 0196`; this is not required for rollback.

## Tests

- `iic_booking/equipment/tests/test_equipment_group_alternatives.py`
- `iic_booking/equipment/tests/test_equipment_group_cross_reschedule.py`
- `iic_booking/equipment/tests/test_equipment_group_rollback.py`
- `iic_booking/equipment/tests/test_equipment_group_audit.py` (races, rollback on errors, quota
  denial, input inheritance, visibility, slot window, permissions, database defaults)
- `iic_booking/equipment/tests/test_equipment_group_business_rules.py` (single-department groups on
  every write path, same charge across members, wallet / settlement / `virtual_booking_id`
  preservation, full and partial cancellation after a reschedule)
