# Cross-Repository Dependencies — Phase 2.6

```text
                    ┌─────────────────────┐
                    │  Docker / Compose   │
                    │  (images + env)     │
                    └─────────┬───────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
     Portal Backend ←──► PostgreSQL      Redis/Celery
              │
              ├──────────► Portal Frontend (VITE_API_URL)
              │
              ├──────────► Deployment Center metadata
              │                 │
              │                 ├── DSA installer (SHA256)
              │                 ├── Wizard installer
              │                 └── RAA installer
              │
              ├──► DSA ──► Equipment Wizard ──► Equipment PC
              │
              └──► RAA ──► Analysis PC ──► Tunnel / Guacamole
```

## Compatibility (proposed SemVer — fill after tags)

| Consumer | Needs |
|----------|--------|
| Frontend 2.5.x | Backend 2.5.x (`/v1/lab/`, `/v1/deployment/`) |
| DSA 1.0.x | Backend sync APIs with `equipment_pcs` + config ack |
| Wizard 1.0.x | DSA LocalApi with ManagementApiKey + discovery |
| RAA 1.0.x | Backend analysis APIs + enrollment; update discover auth |
| DB schema 2.5 | Backend migrations through lab 0003 |
| Docker FE image | Matching backend API URL |

## Break scenarios

| Mix | Result |
|-----|--------|
| FE 2.5 + BE pre-2.5 | Cards/APIs 404 |
| DSA 1.0 + BE without heartbeat field | Silent rollup loss |
| Unsigned installer without SHA check | Integrity risk |

Detail: [`docs/release/phase-2.5-rc1/02-Dependency-Matrix.md`](../release/phase-2.5-rc1/02-Dependency-Matrix.md).
