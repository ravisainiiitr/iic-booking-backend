# RC1 Go-Live Package

## Repository Versions and SHAs

### Portal Backend (B1-B8)
- `d4d50e29891bce543d6d9258958fb744df71d90e`
- `500629b60992839fce99be2d2257230dfcb43ba3`
- `24fb089613ad7fd51dd39bde24ebf1f2845a385d`
- `61b151fdb66d5dffef84dbbe9786e05e458ad167`
- `932d016bb1119e71ada4df4959ab508217d46c52`
- `49bfd66835e1c9d6d40e84184cf2dab28cd7281d`
- `7b53a93542950ed30df8a27f235bfe7cfc02693d`
- `4ed823579474a9b4d15ca35703543dfc42491184`

### Frontend (F1-F4)
- `e8b4d1dd94f0fd79dbf11f8b3298d92b1b89e518`
- `3a66794e446374f65dcc939008c30f4f6aa1a7aa`
- `8cd1d59f7150b0b8354dce5dfc99b60ff8631056`
- `e548c7962af84c611543b03e723ea76683e49476`

### DSA (D0-D4)
- `b657c20228a9c7f273d78c0af6c6b25e059fa1f7`
- `f58f8e5937c4f8e117d1af14b5e9ae01c9757b4e`
- `6c0191f1c7187ce005756264d9aa209c11546213`
- `6d9e5dd52ac80ceb564d947fba3fe16082e11224`
- `495e27b56377b1168328189ad82f2bfeee2be826`

### RAA (R1-R4)
- `e841afbf0a693b348c833ead5ce958efa8e06044`
- `93533bfad6608c0c36d06cf4a90c8ca118deb285`
- `80314f07f7f4ad24dc5614cc4162e71d9141294f`
- `170d689e7e543f73e6b328ae6566ddddc57c0b1e`

## Installer Versions

- DSA installer: RC1 package from DSA D4 release stream (publish-time version/hash to be recorded).
- RAA installer: RC1 package from RAA R4 release stream (publish-time version/hash to be recorded).
- Equipment Wizard installer: deployment center-managed release package (publish-time version/hash to be recorded).

## Migration Versions

- `remote_analysis`: through `0020_reservation_checkin_window`
- `equipment`: through `0184_equipment_analysis_checkin_policy`
- `deployment`: through `0002_compatibility_repair_packages`
- `sync`: through `0018_equipment_pc_ip_reservation`
- `lab_infrastructure`: through `0003_sat_execution_mode`

## Deployment Order

1. Infra dependencies (Postgres, Redis, storage, gateway).
2. Portal backend deployment + migration.
3. Frontend deployment.
4. Deployment center verification.
5. DSA/RAA/Wizard installer publication.
6. Agent rollout and commissioning checks.

## Acceptance Criteria References

- `docs/phase-5/Production-Acceptance-Criteria.md`
- `docs/phase-5/Master-Commissioning-Checklist.md`
- `docs/phase-5/EndToEnd-Test-Matrix.md`

## Commissioning Sequence References

- `docs/phase-5/Live-Commissioning-Procedure.md`
- `docs/phase-5/Verification-*.md` worksheets
- `docs/phase-5/playbooks/*.md`

## Known Issues

Primary known items tracked in:
- `docs/phase-5/Known-Issues.md`

## Rollback

- Use `docs/phase-5/Production-Rollout-Plan.md` rollback points and
  `docs/phase-4/ProductionDeploymentRunbook.md` rollback procedure.

## Contacts

| Function | Primary | Secondary | Contact Channel |
|---|---|---|---|
| Release Manager |  |  |  |
| Platform Backend Lead |  |  |  |
| Frontend Lead |  |  |  |
| DSA Lead |  |  |  |
| RAA Lead |  |  |  |
| Lab Operations Lead |  |  |  |
| Security On-Call |  |  |  |
| Incident Commander |  |  |  |
