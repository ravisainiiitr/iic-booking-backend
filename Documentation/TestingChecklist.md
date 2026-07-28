# Testing Checklist

Remote Analysis — automated + manual verification (RC1).

## Automated

```bash
pytest iic_booking/remote_analysis/tests/ -q
python manage.py validate_remote_analysis
python manage.py check
```

Coverage includes: health probes, anonymous dashboard denial, path traversal rejection, notification/activity smoke, invitation expire idempotency, migration graph presence, architecture validation command.

## Regression checklist (manual)

### M1–M2 Agent / Portal

- [ ] Agent registers and heartbeats
- [ ] Inventory / software visible
- [ ] Command PING completes

### M3 Scheduler

- [ ] Create reservation → RESERVED
- [ ] Queue processes when busy
- [ ] Cancel / extend
- [ ] Conflict detection

### M4 Sessions

- [ ] Create session → PREPARING → READY/ACTIVE (mock or Guacamole)
- [ ] Launch token one-time
- [ ] Terminate → cleanup
- [ ] Idle/expire tasks

### M5 Workspace

- [ ] Workspace auto-created with reservation/session
- [ ] Upload / download / version
- [ ] Sync / collect commands
- [ ] Archive / restore
- [ ] Path traversal blocked

### M6 Operations

- [ ] Dashboard KPIs
- [ ] Alerts acknowledge/resolve
- [ ] Report generate JSON/CSV

### M7 Collaboration

- [ ] Notifications list + mark read
- [ ] Comment / note
- [ ] Share / invite
- [ ] Assistance request lifecycle
- [ ] Timeline viewer

### M8 Hardening

- [ ] `/health/live/` 200
- [ ] `/health/ready/` 200
- [ ] `validate_remote_analysis` no FAIL
- [ ] Celery tasks retry on Redis blip (observe Flower)
- [ ] `mock_guacamole` documented for environment

## Failure recovery drills

- [ ] Stop Agent → alert / offline → restart → heartbeat resumes
- [ ] Restart Portal → Agents reconnect
- [ ] Kill Celery worker mid-task → acks_late redelivery for periodic job
- [ ] Terminate failed session → cleanup

## Production manual verification

1. TLS certificate valid
2. Admin can open Remote Analysis UI tabs
3. Operator view-only restrictions as designed
4. Guacamole credentials not present in browser network responses
5. Backup job succeeded in last 24h
