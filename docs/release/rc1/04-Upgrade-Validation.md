# Remote Analysis RC1 — Upgrade / Migration Validation

## Migration chain (`remote_analysis`)

| Migration | Purpose | Destructive? |
|-----------|---------|--------------|
| `0001_initial_remote_analysis` | Core workstation/agent models | No (create) |
| `0002_scheduler_reservation_engine` | Reservations / queue | No |
| `0003_browser_remote_desktop_guacamole` | Sessions / Guacamole models | No |
| `0004_analysis_workspace_file_exchange` | Workspace files | No |
| `0005_operations_center` | Ops KPIs / alerts / reports | No |
| `0006_collaboration_center` | Collaboration models | No |
| `0007_production_hardening_indexes` | Indexes | No |
| `0008_workstation_status_heartbeat_index` | Indexes | No |
| `0009_auto_data_sync_fields` | Sync fields / AlterField | No |
| `0010_workspace_lifecycle_phases` | Phase rename + `RunPython` map | Data remap only |
| `0011_commissioning_run_observability` | Commissioning run models | No |
| `0012_single_active_session_per_booking` | Settings boolean | No |

## Forward upgrade

```bash
python manage.py migrate remote_analysis
python manage.py showmigrations remote_analysis
python manage.py sync_remote_analysis_settings
```

Expected: all migrations `[X]` through `0012`.

## Rollback path

| Target | Guidance |
|--------|----------|
| App rollback (preferred) | Redeploy previous code; leave DB at `0012` if columns are additive |
| `0012` reverse | Safe: removes `single_active_session_per_booking` field |
| `0011` reverse | Drops commissioning observability tables |
| `0010` reverse | Remaps phase strings via `backwards_map_phases` — test on staging first |
| Pre-`0003` | Not recommended for any environment that has live sessions |

**No `DeleteModel` / `RemoveField` of core booking/workspace entities** in this chain.  
**No unconditional data wipe.**  
`0010` is the only data-transforming migration; reverse is defined.

## Verdict

Forward upgrade to RC1 is **supported**.  
Preferred production rollback is **application rollback + keep DB forward**.  
Destructive migrations: **none identified** for RC1 path.
