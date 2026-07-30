# Remote Analysis — Backup Guide (RC1)

## Automation

```bash
./scripts/deploy/backup.sh
./scripts/deploy/backup.sh --label nightly-$(date -u +%Y%m%d)
./scripts/deploy/restore-verify.sh backups/deploy/<label>
```

## What to back up

| Asset | Frequency | Notes |
|-------|-----------|-------|
| PostgreSQL (full DB) | Daily + PITR if available | Includes all `remote_analysis_*` tables |
| `MEDIA_ROOT` / workspace + archive roots | Daily | File exchange content |
| Redis | Optional | Ephemeral; not source of truth |
| Guacamole DB | Daily | Connections/users (ephemeral sessions less critical) |
| Agent state on PC | Optional | `ProgramData\RemoteAnalysisAgent\State` — rebuildable |
| Secrets / `.env` | On change | Secret manager; encrypted offline copy |
| Django `SECRET_KEY` | On change | Required to decrypt stored RDP/Guac secrets |

## Portal workspace paths

- Default workspaces: `MEDIA_ROOT/remote_analysis/workspaces`  
- Default archives: `MEDIA_ROOT/remote_analysis/archives`  
- Commissioning evidence: storage key `remote_analysis/commissioning_runs/<id>/evidence.zip`  

## Restore order

1. Restore PostgreSQL  
2. Restore media/workspace volumes  
3. Restore Guacamole DB (if used)  
4. Deploy Portal + Celery with same `SECRET_KEY`  
5. Verify readiness + sample workspace file download  
6. Re-check agent enrollment (tokens may still be valid if DB restored)  

## Verification

- `showmigrations remote_analysis`  
- Spot-check one `AnalysisWorkspace` + files  
- Spot-check one `RemoteDesktopSession` history row  
- Toolkit health green  

Do **not** restore Redis as a substitute for DB/media.
