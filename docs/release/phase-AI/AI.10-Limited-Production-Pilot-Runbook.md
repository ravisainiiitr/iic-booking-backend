# AI.10 — Limited Production Pilot Runbook

**Portal:** https://equip.iitr.ac.in  
**Audience:** Lab operators, OIC, on-call admins  
**Scope:** Core booking / sample / result pilot with **Copilot OFF** and **FCM OFF**  
**Companions:** `AI.10-Pilot-Support-Matrix.md`, `AI.10-Pilot-Checklist.md`, phase-M `Operations-Runbook.md`

---

## 0. Safety rules for this pilot

1. Keep **`RESEARCH_COPILOT_ENABLED=false`** in production.
2. Keep **FCM disabled** (no Firebase / server key required for pilot).
3. Users receive **email + in-app notifications** (CommunicationLog / Alerts).
4. **Do not attach result files to email** — results are downloaded from Booking Details.
5. Do **not** invent staging; use production only with **controlled pilot accounts**.
6. Equipment PC has **no Internet** — it talks to **DSA on the lab LAN only**.

---

## 1. Happy-path pilot workflow

| Step | Actor | Action | Expected outcome |
|------|-------|--------|------------------|
| 1 | User | Creates booking (web or Android) | Slot/quota validated; booking **BOOKED** |
| 2 | System | Booking confirmation | Email + in-app notification; deep link → Booking Detail |
| 3 | User | Submits / delivers sample per equipment rules | Sample lifecycle advances (e.g. Sample Sent / Held / Forwarded) |
| 4 | Operator | Accepts or rejects sample (mobile Operations or portal) | Accept → Sample Accepted; Reject → **reason required** + refund/cancel path |
| 5 | Lab | Equipment operation via DSA ↔ Equipment PC | Workspace / instrument data per existing DSA flow |
| 6 | Operator | Completes booking | Status **COMPLETED**; completion email **without** result attachments |
| 7 | Lab / DSA | Result upload to portal object storage / result record | Files listed under Booking Results when available |
| 8 | System | Result available notice | Email/push log + in-app; deep link → Booking Detail → Results |
| 9 | User | Downloads result from Booking Details | Authz: owner (or operator) only; other users **403** |
| 10 | User / Lab | Booking closure / sample return-dispose as policy | Lifecycle closed |

---

## 2. Operator mobile path (Android)

1. Login as lab operator / incharge.
2. Home → **Operations** (Today’s Work).
3. Open booking → **Accept Sample** or **Reject Sample**.
4. Rejection: enter reason → confirm dialog.
5. When ready → **Complete Booking** (do not expect results in email).
6. Advise user to open **Alerts** / **My Bookings** → Booking Detail → Results.

---

## 3. What to do when things go wrong

### Equipment PC offline

1. Confirm PC power and **department LAN** link.
2. Confirm **DSA** service running on the bridge PC (`DepartmentSyncAgent`).
3. On Equipment PC: do **not** require Internet; fix LAN/DSA discovery first.
4. Escalate to Lab IT if SMB/workspace share missing after DSA is healthy.

### DSA offline

1. Check Windows service / DSA process on department sync host.
2. Portal: device provisioning / agent online status.
3. Local health (on DSA host): existing DSA health endpoint (see phase-M runbook).
4. Do not complete “result available” communications until sync recovers.

### Result upload fails

1. Confirm booking is **COMPLETED** (or eligible for results).
2. Check portal result list for the virtual booking id.
3. If S3/object storage fails: retry upload; temporary DSA upload copies are retained until successful S3 publish (`delete_local_upload_copy` only after success).
4. Do **not** email the file as an attachment workaround.
5. Escalate to Admin if retries fail.

### User says they did not get the result / “rejects” result

1. Confirm user is the **booking owner**.
2. Ask them to open Booking Detail → Results (web or Android).
3. Check Alerts for “Results available”.
4. Check spam for results-available **email** (instructions only, no attachment).
5. If files missing in portal: treat as upload/storage issue (above), not a mail-attachment issue.

### Remote Analysis PC unavailable

1. Check RAA service / heartbeat (portal fleet / agent health).
2. Confirm software inventory and license for required software.
3. Scheduler selects workstations by department / online / healthy / software / license / resources / load / LRU — **not** a hard Equipment→Remote PC map.
4. If only RA is down, **core instrument booking + DSA results** can still proceed for non-RA bookings.

### Booking needs cancellation

1. User cancels via portal/Android when policy allows.
2. If sample already accepted: follow existing refund / disruption policy (portal).
3. Operator/admin cancellation must leave an audit trail (booking events).
4. Confirm user receives cancellation / refund notification.

### Android login / session issues

1. Confirm device network can reach `https://equip.iitr.ac.in`.
2. Force-stop → reopen (token should persist).
3. If 401: app clears session → Sign in again.
4. Logout from Profile clears encrypted token store.

---

## 4. Daily pilot health (non-destructive)

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' https://equip.iitr.ac.in/
curl -fsS https://equip.iitr.ac.in/api/version/
curl -fsS https://equip.iitr.ac.in/api/v1/provisioning/capabilities/
curl -fsS https://equip.iitr.ac.in/api/v1/analysis/health/ready/
```

Expect HTTP **200**. Capabilities must show **`research_copilot=false`** for this pilot.

---

## 5. Migration note (ops)

Production Django start script runs:

```bash
python /app/manage.py migrate --noinput
```

Manual / workflow equivalent:

```bash
# On production host (existing process)
docker compose -f docker-compose.production.yml exec django python manage.py migrate --noinput
# or GitHub Actions: migrate-production.yml
```

Do **not** hand-edit the production database.

---

## 6. Rollback (do not run as a drill unless approved)

- Deploy workflow stores previous release tag under `.deploy-state/`.
- Manual: `scripts/deploy/rollback.sh` (optional `ROLLBACK_REF=<sha>`).
- If schema is newer than rolled-back code: restore DB from backup per phase-M Operations Runbook — **do not invent ad-hoc SQL**.

---

## 7. Out of scope for AI.10 pilot

- Production Copilot enablement  
- FCM push delivery  
- Creating a staging environment  
- Load testing production  
- Destructive backup restore on live RDS without change window  
