# Phase 2.9 - Model Audit (B1-B8)

## New models introduced

### B1
- `remote_analysis.TunnelSession`
- `remote_analysis.TunnelMetric`
- `remote_analysis.TunnelEvent`

### B3
- `deployment.EquipmentPcWizardRelease` (extended in `deployment/0002`)

### B4
- `sync.EquipmentSyncTemplate`
- `sync.EquipmentPcIpReservation`

### B5
- `lab_infrastructure.ConfigurationChange`
- `lab_infrastructure.ConfigurationAck`
- `lab_infrastructure.LabRepairAction`
- `lab_infrastructure.LabAuditEvent`
- `lab_infrastructure.LabAlert`
- `lab_infrastructure.SatTestCase`
- `lab_infrastructure.SatTestRun`
- `lab_infrastructure.SatTestResult`
- `lab_infrastructure.SatEvidence`
- `lab_infrastructure.SatDefect`

## Modified models/fields introduced by migrations

### B1
- `remote_analysis.RemoteAnalysisSettings`: tunnel transport and gateway configuration fields.
- `remote_analysis.RemoteCommand`: command choices expanded for tunnel commands.

### B2
- `equipment.Equipment`: added session defaults, extension, RAW/RESULTS paths, and check-in policy fields.
- `remote_analysis.MaintenanceWindow`: added maintenance kind/metadata/restore fields and indexes.
- `remote_analysis.AnalysisWorkstation`: status choice expansion and fingerprint identity fields.
- `remote_analysis.WorkstationStateHistory`: status choice alignment.
- `remote_analysis.AnalysisReservation`: check-in window/notification/missed-checkin fields and status choice expansion.

### B3
- `deployment.EquipmentPcWizardRelease`: added compatibility JSON, rollback pointer, repair/emergency file fields.

### B4
- `sync` core models and serializers updated to integrate template/ip-reservation-driven behavior.

### B5
- `lab_infrastructure` SAT models expanded in `0003` with execution ordering, readiness metadata, defect/evidence flows.

## Removed models

- None removed by B1-B8 migrations.

## Migration ownership map

- B1: `remote_analysis/0017`
- B2: `equipment/0182-0184`, `remote_analysis/0018-0020`
- B3: `deployment/0001-0002`
- B4: `sync/0017-0018`
- B5: `lab_infrastructure/0001-0003`
- B6-B8: no schema changes

## Audit result

Database model evolution is additive and traceable through B1-B5; no model deletions were introduced in this backend commit sequence.

