# Remote Analysis — Release Notes v1.0.0-rc1

**Tag:** `remote-analysis-v1.0.0-rc1`  
**Component version:** `1.0.0-rc1` (reported by `/api/v1/analysis/health/`)

## Semantic version

Recommend **1.0.0-rc1** as the first production candidate of the Remote Analysis subsystem (Portal package + Agent compatibility + Guacamole integration).  
Final GA should be **1.0.0** after RC soak / SAT sign-off.

## New features (cumulative to RC1)

- Agent registration, heartbeat, command plane  
- Reservation / scheduling / allocation  
- Browser remote desktop via Apache Guacamole (ephemeral connections, Portal-owned auth)  
- Analysis workspace file exchange (prepare / collect / cleanup, checksums)  
- Booking ↔ Remote Analysis integration + HTML desktop launcher  
- Operations / diagnostics toolkit + sync commissioning console  
- Commissioning observability (Run ID, timeline, evidence ZIP, failure snapshots)  
- Collaboration / ops analytics models (as shipped in milestones 5–7)  
- Production hardening: readiness probes, enrollment key gate, configuration catalog  

## Breaking changes

- Production readiness **fails** if `DEBUG=False` and `mock_guacamole=True`  
- Production readiness **fails** if `RA_AGENT_ENROLLMENT_KEY` missing when `DEBUG=False`  
- Workspace `sync_phase` values use lifecycle names (`DownloadingInput`, …) after `0010`  
- Booking launch API returns an enriched payload (`launch_url`, `launcher_url`, …) — additive; clients ignoring new fields remain compatible  

## Migration requirements

Apply Django migrations for app `remote_analysis` **0001 → 0012**.

```bash
python manage.py migrate remote_analysis
python manage.py sync_remote_analysis_settings
```

Also apply any related `equipment` booking remote-analysis fields already present in your tree.

## Known limitations

See also [docs/sat/09-Known-Limitations.md](../../sat/09-Known-Limitations.md).

- Session recording not implemented  
- Virus scanner backends other than `noop` not implemented  
- Agent does not embed Guacamole (by design)  
- Large file (&gt;1 GB) transfers depend on reverse-proxy / disk tuning  
- Transfer resume is retry-oriented, not byte-range  
- Guacamole SAT live cases require `SAT_GUAC=1` lab  

## Supported topology

Portal (HTTPS) + PostgreSQL + Redis + Celery worker/beat + Guacamole/guacd + Windows Analysis PCs with RAA + RDP from guacd.

## Upgrade / rollback

See [04-Upgrade-Validation.md](04-Upgrade-Validation.md) and deployment rollback notes.
