# Dependency Matrix — Platform 2.5.0-rc1

Versions below are **proposed**. Pins are filled in the [Release Manifest](./00-Release-Manifest.md) after tags exist.

---

## Control-plane topology (unchanged)

```text
Main Admin UI (Frontend)
        │
        ▼
Portal Backend (API + DB)
   ├──► Department Sync Agent ──► Equipment PC Wizard / EqPC
   └──► Remote Analysis Agent ──► Analysis PC
              │
              └──► Reverse Tunnel Gateway + Guacamole
```

---

## Version dependency matrix (proposed)

| Component | Version | Depends on | Compatible with | Breaks if |
|-----------|---------|------------|-----------------|-----------|
| **Portal Backend** | 2.5.0-rc1 | Postgres, Redis, migrations ≥ schema 2.5 | Frontend 2.5.x; DSA ≥1.0.0-rc1 for EqPC rollup; RAA ≥1.0.0-rc1 for enriched HB | Frontend 2.4 without Lab routes still runs booking; Lab APIs 404 if backend old |
| **Portal Frontend** | 2.5.0-rc1 | Backend API 2.5.x (`/v1/lab/`, `/v1/deployment/`) | Backend 2.5.0-rc1 | Backend master/pre-2.5 → cards/APIs fail |
| **Database schema** | logical 2.5.0 | Backend 2.5 migrations | Backup from pre-2.5 for rollback | Skipping migrations |
| **DSA** | 1.0.0-rc1 | Portal sync APIs (enroll, heartbeat, bootstrap, ack) | Portal ≥2.5 for config ack + equipment_pcs | Portal without serializer field drops rollup |
| **Equipment Wizard** | 1.0.0-rc1 | DSA LocalApi discovery/pairing/config-pack | DSA 1.0.0-rc1 with ManagementApiKey | DSA without pairing fail-closed / discovery |
| **RAA** | 1.0.0-rc1 | Portal analysis APIs + enrollment key | Portal ≥2.5 for update discover/report | Admin-only discover endpoints (fixed in WT) |
| **Deployment Center** | (portal feature) | Published installer rows + files | Matching installer versions in matrix JSON | Missing SHA/files |
| **Docker stack** | image tags TBD | Backend+FE commits | Same RC tag | Mixing RC FE with old BE |
| **Guacamole / Tunnel** | as deployed | Portal RA tunnel config | RAA tunnel probe | Misconfigured WSS/secret |

---

## Build-order dependencies

```text
1. Tag / commit Portal Backend (migrations included)
2. Build & push Backend Docker image
3. Run migrations (equipment → remote_analysis → sync → deployment → lab_infrastructure)
4. Tag / commit Portal Frontend
5. Build & push Frontend image (VITE_API_URL → new API)
6. Publish DSA installer from DSA tag → upload via publish_dsa_installer
7. Publish Wizard → publish_equipment_wizard
8. Publish RAA → publish_ra_installer
9. Fill Release Manifest SHA256 + compatibility JSON
10. Lab SAT against this closed set
```

---

## Runtime dependency (lab PC)

| Lab role | Needs |
|----------|-------|
| Equipment PC | Wizard install → DSA reachable on LAN |
| Analysis PC | RAA install → Portal HTTPS + enrollment |
| DSA host | Portal connectivity + LocalApi key configured |

---

## Anti-patterns

- Deploying Frontend 2.5 against Backend without `lab_infrastructure` migrations.  
- Publishing installers built from dirty local `artifacts/` without recording commit.  
- Mixing DSA 1.0-rc1 with Portal lacking `equipment_pcs` heartbeat field.
