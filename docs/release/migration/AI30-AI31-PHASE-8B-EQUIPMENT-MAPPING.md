# Phase 8B — Equipment Mapping

## Principle

Explicit OLD → NEW mapping only. **No fuzzy runtime name matching.**

Model: `LegacyEquipmentMapping` (`users.models.portal_migration`)

| Field | Notes |
|-------|--------|
| `old_equipment_id` | Legacy portal equipment PK (unique) |
| `old_equipment_code` / `old_equipment_name` | Audit labels |
| `new_equipment` | FK to `equipment.Equipment` (nullable until mapped) |
| `department` | Optional; cross-dept vs new equipment → CONFLICT |
| `status` | ACTIVE / UNMAPPED / DISABLED / CONFLICT / RETIRED |
| `mapping_reason` | Free text; include `MODE_MISMATCH` to force conflict |

## Rules

1. Do not assume old and new IDs are identical.
2. Do not assume names are identical.
3. Ambiguous cases stay `CONFLICT` until Main Administrator resolves.
4. ACTIVE mapping requires a valid new equipment FK.
5. Duplicate ACTIVE mappings to the same new equipment are reported as CONFLICT by validation.
6. Non-operational new equipment (status ≠ Active) is reported under `disabled`.

## Commands / APIs

```bash
python manage.py validate_legacy_equipment_mapping
```

- `GET/POST /api/portal-migration/admin/equipment-mappings/`
- `GET /api/portal-migration/admin/equipment-mappings/validate/`
- `GET/PATCH /api/portal-migration/admin/equipment-mappings/<id>/`

Main Administrator only. Does not weaken department scope for other roles.

## Production safety

Mapping rows may be created in staging. Do **not** assume production MySQL equipment IDs without operator-confirmed inventory.
