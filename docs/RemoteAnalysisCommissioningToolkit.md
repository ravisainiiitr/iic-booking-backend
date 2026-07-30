# Remote Analysis — Commissioning & Diagnostics Toolkit

Admin-only toolkit to commission and troubleshoot Analysis PCs in under ~10 minutes.
**Does not change production booking/sync workflows.** Optional surfaces only.

## Open the toolkit

```
https://<portal>/api/v1/analysis/operations/toolkit/?view=html
```

Requires `IsAuthenticated` + `CanManageRemoteAnalysis` (same as commissioning console).
Anonymous HTML redirects to portal login. Portal SPA may open with `?view=html&token=<drf_token>` once.

Also linked from:

- `/api/v1/analysis/operations/diagnostics/?view=html`
- `/api/v1/analysis/operations/commissioning/?view=html`

## 10-minute commissioning checklist

1. Open **Toolkit → Overview** — workstation Online, health ≥ threshold, DB/Redis/storage green.
2. **Agent** tab — confirm version, token, heartbeat age, disk %.
3. **Connectivity** — Run suite (PASS on portal API, auth, upload/download checksum, cleanup).
4. **Self-test** — Run Full Self Test (PASS).
5. **Commissioning report** — download PDF; operator signs.
6. Optional: run Sync Commissioning Console for a real booking E2E (live agent Input/Output).

## API map (all manage-only)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/operations/toolkit/?view=html` | HTML shell |
| GET | `/operations/toolkit/dashboard/` | Overview JSON |
| GET | `/operations/toolkit/agent/?workstation_id=` | Agent diagnostics |
| POST | `/operations/toolkit/connectivity/` | Connectivity suite |
| GET | `/operations/toolkit/logs/` | Portal log viewer |
| GET | `/operations/toolkit/health-report/` | RAG health report |
| POST | `/operations/toolkit/self-test/` | Full self-test |
| GET/POST | `/operations/toolkit/report/?export=pdf` | Commissioning report |
| GET | `/operations/toolkit/monitoring/` | Alert recommendations |

## What self-test covers

Portal-side disposable reservation + workspace:

1. Portal API / authentication  
2. Create test workspace  
3. Upload probe file → download → checksum  
4. Dummy Processed output  
5. Cleanup command queued  

Agent disk Input/Output verification remains a lab step via the Sync Commissioning Console (unchanged).

## Production monitoring recommendations

| Alert | Condition | Severity |
|-------|-----------|----------|
| Offline workstation | `heartbeat_age_seconds > 90` | critical |
| Heartbeat timeout | No heartbeat in interval | critical |
| Upload failure | transfer FAILED / `UploadFailed` | high |
| Download failure | transfer FAILED / `PreparationFailed` | high |
| Workspace stuck | in-transit phase > 30 min | high |
| Cleanup failure | `CleanupFailed` / CLEAN FAILED | high |
| Token expiry | `AgentToken.expires_at` < 7 days | medium |
| Disk space | heartbeat disk ≥ 90% or portal free < 10% | high |

Wire these to your existing alert stack (Prometheus/Grafana, cloud watch, or portal `AlertEvent` if used).

## Related

- [RemoteAnalysisCommissioning.md](RemoteAnalysisCommissioning.md) — sync E2E operator guide  
- [RemoteAnalysisCommissioningObservability.md](RemoteAnalysisCommissioningObservability.md) — Run ID, timeline, evidence ZIP  
- [docs/sat/](sat/README.md) — System Acceptance Testing  
