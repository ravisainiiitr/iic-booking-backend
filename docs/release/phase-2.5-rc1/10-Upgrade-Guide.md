# Upgrade Guide — to Platform 2.5.0-rc1

## Preconditions

- [ ] Read [Rollback Plan](./05-Rollback-Plan.md); backup DB + images.  
- [ ] Staging upgrade successful.  
- [ ] Lab window scheduled.  
- [ ] Manifest SHAs known.

## Order

1. **Announce** maintenance.  
2. **Backup** Postgres + `.envs` + note current image digests.  
3. **Deploy Backend** 2.5.0-rc1 image.  
4. **Migrate** in order:  
   - `equipment` (through 0184)  
   - `remote_analysis` (through 0020; watch 0017 restore)  
   - `sync` (through 0018)  
   - `deployment` (through 0002)  
   - `lab_infrastructure` (through 0003)  
5. **Restart** web + celery worker + beat.  
6. **Deploy Frontend** 2.5.0-rc1 (`VITE_API_URL` → this API).  
7. **Smoke** Admin Dashboard cards + API health.  
8. **Publish installers** (if not already) and set compatibility matrix.  
9. **Upgrade DSA** on lab hosts (Deployment Center).  
10. **Re-validate** ManagementApiKey / pairing.  
11. **Upgrade Wizard / EqPC** as needed.  
12. **Upgrade RAA** on Analysis PCs.  
13. **Lab SAT** smoke (COM-001, BKG-001, RA-001).  
14. **Fill Manifest** post-deploy measured values.

## Administrator notes

- Main Admin only for Lab Infrastructure, Deployment Center, SAT Dashboard (`user_type=admin`).  
- Dept admins keep existing department-sync / RA scopes unless extended later.

## If upgrade fails

Execute Rollback Plan immediately; do not continue agent fleet upgrades.
