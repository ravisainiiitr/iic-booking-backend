# R11 — Live Qualification

Checklist:

1. Catalog auto-grows after RAA inventory post
2. Equipment mapping UI shows availability counts
3. RAVI busy → next PC with same software allocated
4. No-mapping / all-busy / offline messages are distinct
5. Existing Guacamole session path unchanged

## Evidence (2026-08-11)

| Check | Result |
|-------|--------|
| Backend deploy `v2.5.18-ra-r11` | SUCCESS ([run](https://github.com/ravisainiiitr/iic-booking-backend/actions/runs/31512027197)) |
| Migrate Production (includes `0025_r11_installed_software_allocation`) | SUCCESS ([run](https://github.com/ravisainiiitr/iic-booking-backend/actions/runs/31512287343)); showmigrations `[X] 0025_r11_installed_software_allocation` |
| Frontend Deploy (PR #6 merge) | SUCCESS ([run](https://github.com/ravisainiiitr/iic-booking-frontend/actions/runs/31511974534)) |
| Production catalog/workstations API reachable | HTTP 401 without session (expected auth gate) |
| Unit: busy PC → next PC | PASS (`test_r11_busy_pc_selects_next_compatible_pc`) |
| Live dual-PC allocation on production | **NOT TESTED** — requires two healthy RAA PCs with same software and a controlled booking; do not fabricate |
| Live inventory (RAVI / PXRD→Notepad) | **NOT TESTED** in this pass (read-only portal session not available in agent) |
