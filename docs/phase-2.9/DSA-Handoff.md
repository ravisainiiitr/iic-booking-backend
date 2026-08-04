# DSA Handoff - Phase 2.9

## Portal APIs

- Control plane: `/api/v1/sync/enroll/`, `/heartbeat/`, `/bootstrap/`, `/commands/*`.
- Data plane: `/equipment/`, `/bookings/`, `/workspaces/`, `/uploads/*`, `/results/import/`, `/results/finalize/`.
- Security plane: `/security/device/*`, `/security/certificates/*`, `/security/api-keys/rotate/`.
- Update/release plane: `/releases/*`, `/updates/*`, `/installer/*`.
- Configuration ACK alias: `/api/v1/sync/configuration/ack/` (mapped to lab configuration ack handler).

## Provisioning

- Plug-and-play template model: `sync.EquipmentSyncTemplate` (`sync/0017`) defines share/folder/network/retry/software/health baselines.
- IP reservation model: `sync.EquipmentPcIpReservation` (`sync/0018`) provides portal-side reservation mirror and conflict state.
- Equipment link and installer tree endpoints provide portal-driven mapping for provisioned nodes.

## Configuration

- DSA bootstrap contract should consume template-derived configuration plus equipment-level analysis settings from B2 fields.
- Configuration ACK must post version/status/error metadata to `/api/v1/sync/configuration/ack/`.
- Configuration rollback/audit visibility is surfaced via lab infrastructure APIs (`/api/v1/lab/configuration/*`).

## Enrollment

- Enrollment remains key/token-based via `/api/v1/sync/enroll/`.
- Post-enrollment identity and heartbeat are required before command/data-plane operations.

## Discovery

- Device discovery and capability surfacing depend on `/api/v1/sync/enterprise/*`, `/monitoring/*`, and inventory/heartbeat streams.
- DSA should publish stable identifiers (agent UUID, MAC, computer name, observed IP) for reservation and fleet reconciliation.

## Configuration push

- Portal -> DSA command lifecycle uses `/api/v1/sync/commands/*`.
- DSA must ACK and complete commands to keep portal state and queue progression consistent.
- Template updates and profile version changes must eventually result in ACK records to prevent stale policy drift.

## Expected Portal behavior

- Reject unauthenticated or stale agent tokens for control/data/security operations.
- Keep command queues idempotent and resumable across heartbeat interruptions.
- Preserve existing DSA compatibility while exposing new template and reservation capabilities additively.
- Keep ticketed installer download semantics for auditable release distribution.

