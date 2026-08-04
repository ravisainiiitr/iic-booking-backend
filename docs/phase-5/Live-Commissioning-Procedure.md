# Live Commissioning Procedure (IIT Roorkee Production)

## 1. Server Preparation

1. Verify host OS patches, clock sync, firewall policy, and backup agent status.
2. Confirm network routes to database, storage, SMTP, and gateway/guacamole.
3. Validate secrets vault/credential distribution process for production keys.

## 2. Portal Deployment

1. Deploy backend artifact for approved RC1 SHA set.
2. Apply runtime env configuration and secret bindings.
3. Start API and worker services in runbook order.
4. Verify health, readiness, and auth endpoints.

## 3. Frontend Deployment

1. Deploy frontend artifact mapped to RC1 commit.
2. Validate API base URL, proxy, CORS, and role-based route rendering.
3. Smoke test key flows: login, dashboard, booking, remote analysis entry.

## 4. Database Migration

1. Take full production snapshot backup.
2. Execute migrations to approved heads.
3. Validate migration head integrity and critical tables.
4. Keep rollback snapshot pointer recorded in release log.

## 5. Deployment Center

1. Verify deployment center endpoints and metadata listing.
2. Confirm compatibility matrix entries for DSA/RAA/Wizard.
3. Validate ticket generation and secure download behavior.

## 6. DSA Deployment

1. Publish DSA installer and metadata.
2. Deploy to pilot equipment PCs.
3. Complete enrollment and verify heartbeat/commands/config ACK.

## 7. Equipment Wizard Deployment

1. Publish wizard artifact and metadata.
2. Validate discovery, pairing, and provisioning workflow on pilot node.
3. Confirm provisioning outputs in portal equipment inventory.

## 8. RAA Deployment

1. Publish RAA installer and metadata.
2. Deploy to pilot analysis PCs.
3. Verify register/heartbeat/inventory/command-complete pathways.

## 9. Agent Enrollment and Registration

1. Enroll DSA and RAA with production enrollment policy.
2. Confirm identity appears in portal fleet/infrastructure views.
3. Validate duplicate handling and health classification.

## 10. Software Registration and Mapping

1. Ensure software inventory posted from agents.
2. Verify software mapping policies in portal.
3. Confirm reservation allocation respects software requirements.

## 11. Configuration Push and Commissioning

1. Push baseline configuration profile to pilot nodes.
2. Validate apply, ACK, and drift metrics.
3. Run commissioning checklist for each subsystem.

## 12. Health Verification

1. End-to-end run: booking -> reservation -> check-in -> launch -> end -> upload -> download.
2. Validate monitoring, alerts, logs, and audit traces.
3. Verify SAT dashboard baseline and readiness report surfaces.

## 13. Rollback Procedure

Trigger rollback if any critical acceptance criterion fails:
1. Stop rollout.
2. Restore database snapshot.
3. Redeploy prior backend/frontend artifacts.
4. Revert active installer release pointers.
5. Validate core service and agent recovery.

## 14. Completion Gate

- Mark all critical commissioning checklist items `PASS`.
- Capture evidence links/screenshots/log extracts.
- Obtain release manager, operations lead, and security sign-off.
