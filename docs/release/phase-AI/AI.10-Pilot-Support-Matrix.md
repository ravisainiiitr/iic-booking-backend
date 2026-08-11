# AI.10 — Pilot Support Matrix

**Architecture reminder**

- **Portal / API** (Internet) — booking, auth, notifications, results metadata  
- **DSA** (Internet + lab LAN) — bridges Equipment PC and portal sync  
- **Equipment PC** (lab LAN only, **no Internet**) — instrument software / local workspace  
- **RAA** (Internet) — remote analysis workstations; software-centric selection  
- **Android** — talks to portal API only (`https://equip.iitr.ac.in/api/` in release)

---

| Problem | First Check | Second Check | Escalation |
|---------|-------------|--------------|------------|
| Booking failure | Portal UI error / slot status | API `/api/bookings/` response; quota/wallet | Admin (booking events + logs) |
| Cancellation denied | Policy (sample accepted?) | Android/web cancel endpoint status | OIC / Admin |
| Sample accept/reject fails | Operator role + booking state | Reject reason present; API `sample-trace/set` | Lab Incharge |
| Equipment offline | Equipment PC power + LAN | DSA discovery / agent online | Lab IT |
| Workspace missing | DSA health + session | SMB/share path on Equipment PC | Lab / IT |
| Result unavailable | Booking Completed? Results list | Object storage / DSA upload retry | Admin |
| Email has no file (expected) | Confirm completion/results email is instruction-only | User download from Booking Details | Support (explain policy) |
| Email missing entirely | Spam + CommunicationLog | SMTP / mail provider status | Admin |
| Android notification deep link wrong | Notification has `real_booking_id` | Auth-scoped booking resolve; fallback My Bookings | Mobile / Admin |
| Android login failure | Network to equip.iitr.ac.in | Token cleared on 401; re-login | Admin |
| Persistent login lost | Force-stop reopen | Encrypted prefs / logout state | Mobile |
| DSA connectivity | Windows DSA service | Portal provisioning device status | Lab IT |
| RAA unavailable | Heartbeat / agent service | Software inventory + license | IT / RA owner |
| RA reservation fails | Required software on booking | Scheduler pool: dept/online/healthy/software/license/load/LRU | IT |
| Guacamole / remote desktop | Guacamole stack `docker ps` | Tunnel / transport readiness checks | IT |
| Copilot visible but failing | Confirm pilot keeps Copilot **OFF** | Capabilities `research_copilot=false` | Do **not** enable; Admin |
| FCM not delivering | Expected — FCM **BLOCKED** | Use email + in-app Alerts | N/A for pilot |
| 5xx / portal down | `/api/v1/analysis/health/ready/` | Docker django/celery/redis status | Admin + rollback if deploy-related |
| Suspected deploy regression | Current vs previous release tag | Health/smoke in deploy workflow | Rollback via `rollback.sh` / Actions |

---

## Escalation contacts (fill for institute)

| Role | Contact |
|------|---------|
| Portal Admin | _(institute)_ |
| Lab Incharge / OIC | _(per department)_ |
| Lab IT (LAN/DSA) | _(institute)_ |
| RA / Guacamole IT | _(institute)_ |

---

## Logging hygiene

When collecting logs for escalation:

- Prefer status codes, booking ids, virtual booking ids, container names.
- **Do not** paste passwords, tokens, AWS keys, Firebase keys, or session secrets into tickets/chat.
