# Backend EC2 Deploy (Linux self-hosted runner)

**Status:** Active release path (replaces AWS ECR and SSH hop)  
**Qualified release example:** `v2.5.0-rc19-release`  
**Runner:** Linux self-hosted on production EC2 (`ip-10-0-1-153`, labels `self-hosted` + `Linux`)

---

## Deployment model

```text
GitHub Release (immutable tag)
        ↓
Backend Release          (Windows Build Host: verify → tests → image build qualification)
        ↓
Deploy Backend           (Linux self-hosted runner on EC2 — no SSH)
        ↓
git checkout <release_tag>
docker compose build --pull
docker compose up -d
Health Check
```

No AWS ECR. No SSH from GitHub-hosted runners.

---

## Workflows

| Workflow | File | Purpose |
|---|---|---|
| Backend Release | `.github/workflows/backend-release.yml` | Tag verification, pytest, Build Host image build |
| Deploy Backend | `.github/workflows/backend-deploy.yml` | Local deploy on EC2 Linux runner |
| Deploy Backend (legacy) | `.github/workflows/deploy.yml` | Retired master-push path |

---

## Required GitHub secrets

**None for Deploy Backend.** EC2 SSH secrets (`EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`, `EC2_PORT`) are **not** used by this workflow.

Application/runtime secrets remain on the EC2 host (e.g. `.envs/.production/.django`).

---

## Deploy Backend inputs

| Input | Default | Meaning |
|---|---|---|
| `release_tag` | `v2.5.0-rc19-release` | Immutable tag already qualified |
| `deploy_path` | `/home/ubuntu` | Git checkout on EC2 |
| `compose_file` | `docker-compose.production.yml` | Compose file |
| `health_url` | `http://127.0.0.1:8080/api/v1/analysis/health/ready/` | Loopback readiness |
| `enable_flower` | `true` | Passes `--profile flower` |

---

## On-host sequence

Executed on the Linux runner in `deploy_path`:

1. Record previous git ref / release tag under `.deploy-state/`
2. `git fetch --all --tags`
3. `git checkout <release_tag>`
4. `git submodule update --init --recursive`
5. `docker compose -f docker-compose.production.yml down`
6. `docker compose … build --pull` (Flower profile when enabled)
7. `docker compose … up -d`
8. Verify django / celeryworker / celerybeat / flower are up
9. Wait for django health when Docker reports Health
10. `curl` health URL until HTTP 200 (fail closed)

Existing helpers: `./deploy.sh`, `./scripts/deploy/rollback.sh`, `./scripts/deploy/verify-production.sh`.

---

## Rollback procedure

### Automatic (Deploy Backend)

On build/up/health failure the script:

1. Checks out `.deploy-state/previous_release_tag`
2. Rebuilds and `up -d`
3. Leaves the Actions run failed

### Manual

```bash
cd /home/ubuntu
PREV=$(cat .deploy-state/previous_release_tag)
git fetch --all --tags
git checkout "$PREV"
docker compose -f docker-compose.production.yml --profile flower down
docker compose -f docker-compose.production.yml --profile flower build --pull
docker compose -f docker-compose.production.yml --profile flower up -d
curl -fsS http://127.0.0.1:8080/api/v1/analysis/health/ready/
```

Or: `ROLLBACK_REF=<tag> ./scripts/deploy/rollback.sh`

---

## Operator checklist

1. Qualify tag with **Backend Release**.
2. Confirm Linux runner `ip-10-0-1-153` is **online**.
3. Dispatch **Deploy Backend** with `release_tag=<qualified tag>`.
4. Confirm compose ps + health HTTP 200 in the run log.
