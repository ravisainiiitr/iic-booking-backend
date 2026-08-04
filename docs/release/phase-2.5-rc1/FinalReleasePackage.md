# Final Release Package - Phase 2.5 RC1

## Repositories and Commit SHAs

### Portal Backend (B1-B8)
- B1 `d4d50e29891bce543d6d9258958fb744df71d90e`
- B2 `500629b60992839fce99be2d2257230dfcb43ba3`
- B3 `24fb089613ad7fd51dd39bde24ebf1f2845a385d`
- B4 `61b151fdb66d5dffef84dbbe9786e05e458ad167`
- B5 `932d016bb1119e71ada4df4959ab508217d46c52`
- B6 `49bfd66835e1c9d6d40e84184cf2dab28cd7281d`
- B7 `7b53a93542950ed30df8a27f235bfe7cfc02693d`
- B8 `4ed823579474a9b4d15ca35703543dfc42491184`

### Frontend (F1-F4)
- F1 `e8b4d1dd94f0fd79dbf11f8b3298d92b1b89e518`
- F2 `3a66794e446374f65dcc939008c30f4f6aa1a7aa`
- F3 `8cd1d59f7150b0b8354dce5dfc99b60ff8631056`
- F4 `e548c7962af84c611543b03e723ea76683e49476`

### Department Sync Agent (D0-D4)
- D0 `b657c20228a9c7f273d78c0af6c6b25e059fa1f7`
- D1 `f58f8e5937c4f8e117d1af14b5e9ae01c9757b4e`
- D2 `6c0191f1c7187ce005756264d9aa209c11546213`
- D3 `6d9e5dd52ac80ceb564d947fba3fe16082e11224`
- D4 `495e27b56377b1168328189ad82f2bfeee2be826`

### Remote Analysis Agent (R1-R4)
- R1 `e841afbf0a693b348c833ead5ce958efa8e06044`
- R2 `93533bfad6608c0c36d06cf4a90c8ca118deb285`
- R3 `80314f07f7f4ad24dc5614cc4162e71d9141294f`
- R4 `170d689e7e543f73e6b328ae6566ddddc57c0b1e`

## Version Summary

- Platform release version target: `2.5.0-rc1`
- Agent release stream: `1.0.0-rc1` family (per existing release documentation)
- Final semantic version stamping and tag publication: pending release-management execution.

## Migration Versions (Portal)

- `remote_analysis`: up to `0020_reservation_checkin_window`
- `equipment`: up to `0184_equipment_analysis_checkin_policy`
- `deployment`: up to `0002_compatibility_repair_packages`
- `sync`: up to `0018_equipment_pc_ip_reservation`
- `lab_infrastructure`: up to `0003_sat_execution_mode`

## Installer Versions

- Portal deployment center metadata: defined; publish-time values to be finalized with artifact hashes.
- DSA installer: project and publish scripts ready; release artifact version/hash pending publish run.
- RAA installer: project and publish scripts ready; release artifact version/hash pending publish run.
- Equipment Wizard installer: release path governed through deployment center; artifact-specific publish metadata pending.

## Deployment Order

1. Infrastructure dependencies (DB/Redis/storage/gateway)
2. Portal backend + migrations
3. Frontend
4. Deployment Center metadata verification
5. DSA/RAA/Wizard installer publication
6. Agent rollout waves and health verification

## Rollback Strategy

1. Stop forward rollout.
2. Restore prior DB snapshot.
3. Re-deploy previous stable backend/frontend artifacts.
4. Re-point deployment center active release metadata.
5. Verify health and agent reconnection.

## Known Limitations

- Full integrated load/security/operational drills are not automatically proven by commit/build completion alone.
- Final artifact signatures/hashes and rollback package validation are release-environment tasks.
- Some advanced API surfaces remain lightly exercised by current UI and need deeper contract testing.

## Open Risks

1. Integration soak risk for heartbeat/tunnel/queue under high concurrency.
2. Installer governance risk until signing + hash publication is fully executed.
3. Operational readiness risk until role-based runbooks are rehearsed and signed off.

## Future Roadmap (Post-RC1)

- Expand automated contract tests across Portal/Frontend/DSA/RAA.
- Add continuous performance/security qualification pipelines.
- Formalize versioned API compatibility policy and endpoint deprecation schedule.
- Add artifact signing and SBOM verification automation in CI.
