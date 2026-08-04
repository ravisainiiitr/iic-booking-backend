# Phase 2.9 - Migration Audit

## Scope

Migrations introduced in backend commit sequence B1-B5 (B6-B8 are documentation/stabilization only).

## Ordered migration chain

### `remote_analysis`

1. `0017_restore_reverse_tunnel_transport` (B1)  
   Depends on:
   - `equipment.0181_waitlistentry_opt_out_and_sample`
   - `remote_analysis.0016_agent_installer_release`
2. `0018_analysis_pc_maintenance_mode` (B2)  
   Depends on `remote_analysis.0017_restore_reverse_tunnel_transport`
3. `0019_workstation_machine_fingerprint` (B2)  
   Depends on `remote_analysis.0018_analysis_pc_maintenance_mode`
4. `0020_reservation_checkin_window` (B2)  
   Depends on `remote_analysis.0019_workstation_machine_fingerprint`

### `equipment`

1. `0182_equipment_analysis_session_duration` (B2)  
   Depends on `equipment.0181_waitlistentry_opt_out_and_sample`
2. `0183_equipment_analysis_raw_results_directories` (B2)  
   Depends on `equipment.0182_equipment_analysis_session_duration`
3. `0184_equipment_analysis_checkin_policy` (B2)  
   Depends on `equipment.0183_equipment_analysis_raw_results_directories`

### `deployment`

1. `0001_equipment_pc_wizard_release` (B3)
2. `0002_compatibility_repair_packages` (B3)  
   Depends on `deployment.0001_equipment_pc_wizard_release`

### `sync`

1. `0017_equipment_sync_template` (B4)  
   Depends on:
   - `sync.0016_dsa_installer_release`
   - `users.0001_initial`
2. `0018_equipment_pc_ip_reservation` (B4)  
   Depends on:
   - `sync.0017_equipment_sync_template`
   - `equipment.0184_equipment_analysis_checkin_policy`

### `lab_infrastructure`

1. `0001_initial` (B5)  
   Depends on:
   - `sync.0018_equipment_pc_ip_reservation`
   - swappable user model
2. `0002_sat_test_dashboard` (B5)  
   Depends on `lab_infrastructure.0001_initial`
3. `0003_sat_execution_mode` (B5)  
   Depends on `lab_infrastructure.0002_sat_test_dashboard`

## Integrity checks

- Missing dependencies: none found in B1-B5 migration set.
- Duplicate migration numbers (within audited apps): none.
- Conflicting migration numbers in audited apps: none.
- Unreachable migration chain in audited apps: none.
- Cross-app dependency loops in audited chain: none.

## Audit result

Migration graph for backend B1-B5 is connected and reachable, with monotonic numbering inside each audited app and explicit cross-app links where required.

