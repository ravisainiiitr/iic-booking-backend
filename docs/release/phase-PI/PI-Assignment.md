# Equipment PI Assignment

## Overview

Each equipment may have zero or more Faculty **Principal Investigators** (`EquipmentPI`).

Admins assign PIs during equipment create/edit (portal Equipment form or Django admin).

## Rules

- Only users with `user_type=faculty` may be assigned.
- Multiple PIs per equipment are allowed (`unique(equipment, faculty)`).
- Soft deactivation via `is_active` is supported; remove deletes the assignment row.
- Changes are audited in `EquipmentPIAuditLog`.

## APIs

- Equipment detail includes `equipment_pis`.
- Equipment admin write accepts `equipment_pis: [{ faculty, is_active }]`.
- Form choices include `faculty` list for the PI picker.

## Security

Only admin-panel roles can mutate equipment (existing Equipment admin permissions). Ordinary users cannot self-assign as PI.
