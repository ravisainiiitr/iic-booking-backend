# Collaboration Center

Milestone 7 of the Remote Analysis Platform.

## Architecture

```
Equipment Booking Portal
  ├── Collaboration Center
  ├── Notification Engine
  ├── Session Assistance
  ├── Activity Feed
  ├── Shared Workspace
  ├── Notes / Comments
  ├── Timeline
  └── Existing Platform (Agent, Scheduler, Guacamole, Workspace, Operations — unchanged)
```

Everything remains Portal-driven. This milestone extends `remote_analysis` with packages:

| Package | Role |
|---------|------|
| `notifications/` | Portal + Email delivery, preferences, quiet hours |
| `activity/` | Per-user and platform activity feeds |
| `comments/` | Session/workspace comments + research notes |
| `sharing/` | Shared workspaces, permissions, invitations |
| `assistance/` | Help request lifecycle |
| `timeline/` | Session lifecycle timeline builder |
| `collaboration/` | Dashboard facade, API views, event hooks |

## Notification flow

1. Domain event (reservation allocated, session create/terminate, sync, transfer, alert, share, invite, assistance)
2. `NotificationEngine.notify(user, type, title, body)`
3. Load `NotificationPreference` — respect disabled types, channel toggles, quiet hours
4. Create `Notification` rows per channel (`PORTAL`, `EMAIL`; SMS/WhatsApp/Push stubs)
5. Deliver + telemetry `notification_delivery`; mark read → `notification_read`
6. Audit under `AuditCategory.NOTIFICATIONS`

Supported types: reservation confirmed/reminder, session starting/ending/terminated, workspace synced, upload/download complete, agent offline (via alerts), maintenance, alerts, invitations, assistance, comments, shares, announcements.

## Sharing model

- `SharedWorkspace` + `WorkspaceSharePermission` (Read / Write / Download / Comment)
- Grant to a **user** or **department** only — no anonymous sharing
- Time-limited via `expires_at`; revoked via `revoked_at`
- Owner or Remote Analysis manager may share
- Does not bypass department RBAC / reservation ownership — share checks sit on top of existing policies
- Every grant audited (`ShareGranted`)

## Assistance flow

```
Help Request → Assigned Operator → Accepted → Resolved → Closed
```

Models: `SessionAssistanceRequest`, `SessionAssistanceEvent`.  
Priority: LOW / NORMAL / HIGH / URGENT. Resolution notes + response-time telemetry.

## Timeline

`GET /api/v1/analysis/timeline/?session_id=` (or `reservation_id`) builds:

Reservation → Workspace → Synchronization → Preparation → Launch → Connection → Uploads → Downloads → Cleanup → Archive

Sources: reservation, workspace, session state history, transfers, cleanup command.

## Permissions

- Existing `CanViewRemoteAnalysis` / `CanManageRemoteAnalysis`
- Private notes visible to author (managers see all)
- Assistance list: owners see own; managers see pending queue
- Share/invite/comment audited under `COLLABORATION` / `ASSISTANCE`

## Activity feed

`ActivityService.record(verb, summary, …)` writes to the user feed and optional platform feed. Verbs cover reservation, workspace, session start/end, upload/download, comment, note, invitation, alert, sync, share, assistance, announcement.

## Dashboard APIs

| Method | Path |
|--------|------|
| GET | `/api/v1/analysis/collaboration/dashboard/` |
| GET | `/api/v1/analysis/activity/` |
| GET | `/api/v1/analysis/notifications/` |
| POST | `/api/v1/analysis/notifications/read/` |
| GET/POST | `/api/v1/analysis/comments/` |
| GET/POST | `/api/v1/analysis/notes/` |
| GET/POST | `/api/v1/analysis/share/` |
| GET/POST | `/api/v1/analysis/invite/` |
| GET/POST | `/api/v1/analysis/assistance/` |
| GET | `/api/v1/analysis/timeline/` |
| GET/POST | `/api/v1/analysis/announcements/` |
| GET/POST | `/api/v1/analysis/bookmarks/` |
| GET/POST | `/api/v1/analysis/favorites/` |

UI: Remote Analysis **Collaboration** tab.

## Celery

| Task | Cadence |
|------|---------|
| `expire_invitations` | 5m |
| `send_reservation_reminders` | 5m |

## Out of scope (explicit)

Real-time chat, screen sharing, voice/video, collaborative editing.
