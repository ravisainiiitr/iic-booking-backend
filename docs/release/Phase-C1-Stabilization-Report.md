# Phase C.1 — Release Infrastructure Stabilization Report

**Date:** 2026-08-04  
**Mode:** Release engineering only  
**Execution:** No runner provision, no workflow runs, no AWS apply, no deploy

---

## Mandatory defects resolved

| ID | Fix |
|---|---|
| **W1** | Backend `verify_tag.outputs.sha` → `steps.sha.outputs.sha` |
| **W2** | Tag push = verify-only; ECR publish requires `workflow_dispatch` + `publish=true` + `dry_run=false` + `release-ecr` + publish gate |
| **W3** | Docker IDs → `docker-image-ids.txt`; installer hashes remain `ArtifactChecksums-SHA256.txt` |
| **W4** | Backend tests never silent-pass; PARTIAL requires `allow_partial_validation=true` |
| **W5** | Platform fails closed without staged child evidence (digests + installer SHA256 + freeze/ledger docs) |
| **OIDC** | Trust generator scoped to repo + `environment:release-ecr` + tag refs (not `ORG/*`) |

---

## Workflow validation (post-stabilization)

| Workflow | Tag push | Dispatch build | Publish gate | Artifacts |
|---|---|---|---|---|
| Backend | verify only | yes | Assert-PublishGate + release-ecr | digests, IDs, SBOM, validation-level |
| Frontend | verify only | yes | env/secret assert + release-ecr | frontend-image.json, IDs |
| DSA | verify only | yes | DC stub fails closed | Installer SHA256 |
| RAA | verify only | yes | DC stub fails closed | Installer SHA256 |
| Platform | n/a (manual) | evidence required | platform-release env | manifest only if evidence PASS |

---

## Decision

### Infrastructure Ready for Provisioning

Remaining blockers are **environmental**, not design defects in the stabilized YAML/scripts.

| Action | Class |
|---|---|
| Push Phase C.1 commits to remotes | **Mandatory** (separate authorization — this phase commits locally only if requested) |
| Provision Windows Build Host + runner `iic-build` | **Mandatory** |
| Create GitHub Environments + reviewers | **Mandatory** |
| Set `ECR_REGISTRY` / OIDC role secret | **Mandatory** before publish |
| Create ECR + apply scoped OIDC (aws scripts) | **Mandatory** before Batch 3 |
| First runs with `dry_run=true` only | **Mandatory** |
| Stage evidence dirs for Platform workflow | **Mandatory** before Platform green |
| Wire DC upload API | **Optional** (Batch 7) |
| Require SBOM tools on host | **Recommended** |

---

## STOP

Do not provision, execute workflows, register runners, create AWS resources, build, or deploy without new authorization.
