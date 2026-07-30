# Remote Analysis — Disaster Recovery Guide (RC1)

## RTO / RPO targets (recommended)

| Tier | RTO | RPO |
|------|-----|-----|
| Portal API + DB | ≤ 4 hours | ≤ 24 hours (better with PITR) |
| Workspace files | ≤ 8 hours | ≤ 24 hours |
| Guacamole | ≤ 4 hours | ≤ 24 hours (sessions re-created) |
| Agent fleet | ≤ 1 day | State rebuildable |

Adjust to institutional policy.

## Scenarios

### A. Portal host loss

1. Provision new host  
2. Restore DB + media  
3. Deploy same release tag + secrets  
4. Start web + Celery  
5. Validate readiness  

### B. Database corruption

1. Restore latest good backup / PITR  
2. Reconcile media (files may be newer than DB — quarantine orphans)  
3. Re-run readiness and SAT smoke  

### C. Guacamole total loss

1. Redeploy Guacamole stack  
2. Re-apply admin password to Portal settings  
3. Existing Portal sessions: terminate and ask users to re-launch (ephemeral Guac objects are gone)  
4. Sync/file workflows remain available without Guacamole  

### D. Analysis PC loss

1. Rebuild PC + Agent  
2. Re-enroll workstation (new agent id/token as designed)  
3. Recreate `WorkstationRdpSecret`  
4. Re-commission with SAT-05  

### E. Secret key rotation

Rotating `SECRET_KEY` invalidates Fernet-wrapped RDP passwords and Guac temp password ciphertext.  
Plan: re-enter RDP secrets; terminate open desktop sessions before rotation.

## Communication

Declare desktop vs sync impact separately. Sync-only mode can serve labs while Guacamole is rebuilt.
