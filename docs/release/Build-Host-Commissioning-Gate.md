# Build Host Commissioning Gate

Use this gate after operator bootstrap. Run `scripts\build-host\Verify-BuildHostReady.ps1` and attach output.

---

## READY FOR RUNNER REGISTRATION

All must be true:

- [ ] `Verify-BuildHostReady.ps1` → **RESULT=PASS**  
- [ ] Session can elevate Administrator when needed  
- [ ] Docker Engine responds (`docker version` Server section)  
- [ ] WSL2 + Ubuntu distro present  
- [ ] `C:\iic-build\runners\actions-runner` binaries extracted (not registered)  
- [ ] Labels planned: `self-hosted,windows,iic-build`  
- [ ] Host is **not** Production EC2  

**If any unchecked → NOT READY FOR RUNNER REGISTRATION.**

---

## READY FOR DRY RUN

All of READY FOR RUNNER REGISTRATION, plus:

- [ ] Runner **registered** and online in GitHub  
- [ ] GitHub Environments exist: `release-build` (and optionally others)  
- [ ] Repo variables present or dry-run does not need ECR  
- [ ] Workflows on remotes include Phase C.1 hardened YAML  
- [ ] First dispatch with **`dry_run=true`** / **`publish=false`** only  
- [ ] No ECR login attempted in the dry run  

---

## READY FOR RC1 BUILD

All of READY FOR DRY RUN, plus:

- [ ] Dry-run Backend (and Frontend if in scope) completed successfully  
- [ ] `.NET SDK 8` and Node **20** verified (not merely newer majors)  
- [ ] VS Build Tools workload verified  
- [ ] Syft/Trivy installed if SBOM required  
- [ ] Artifact directories writable under `C:\iic-build\artifacts`  
- [ ] Disk free ≥ 40 GB (prefer ≥ 100 GB before full matrix)  

---

## READY FOR PRODUCTION RELEASE

All of READY FOR RC1 BUILD, plus:

- [ ] ECR repositories created  
- [ ] Scoped OIDC role applied and secrets configured  
- [ ] Environment `release-ecr` has **required reviewers**  
- [ ] Successful publish dry-check (gate scripts)  
- [ ] Platform evidence staging path understood  
- [ ] Deployment Center upload still gated until Batch 7 approval  
- [ ] Rollback digests / prior images identified  
- [ ] Change window / operator approval recorded  

---

## Sign-off

| Gate | Date | Operator | Pass? |
|---|---|---|---|
| Runner registration | | | |
| Dry run | | | |
| RC1 build | | | |
| Production release | | | |
