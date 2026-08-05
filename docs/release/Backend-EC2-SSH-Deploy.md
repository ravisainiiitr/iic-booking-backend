# Backend EC2 SSH Deploy

**Status:** Active release path (replaces AWS ECR publication)  
**Qualified release example:** `v2.5.0-rc19-release`

---

## Deployment model

```text
GitHub
        ↓
Backend Release          (Build Host: verify tag → tests → image build qualification)
        ↓
Deploy Backend           (ubuntu-latest → SSH → EC2)
        ↓
EC2 host
  git fetch / checkout <release tag>
  docker compose -f docker-compose.production.yml build --pull
  docker compose -f docker-compose.production.yml up -d
  health checks
```

AWS ECR, OIDC publish roles, and image registry push are **not** part of this path.

---

## Workflows

| Workflow | File | Purpose |
|---|---|---|
| Backend Release | `.github/workflows/backend-release.yml` | Tag verification, pytest, Build Host image build |
| Deploy Backend | `.github/workflows/backend-deploy.yml` | SSH deploy of an immutable tag to EC2 |
| Deploy Backend (legacy) | `.github/workflows/deploy.yml` | Retired master-push deploy (dispatch fails with redirect) |

---

## Required GitHub secrets

| Secret | Required | Notes |
|---|---|---|
| `EC2_HOST` | Yes | EC2 hostname or IP |
| `EC2_USER` | Yes | SSH user (e.g. `ubuntu`) |
| `EC2_SSH_KEY` | Yes* | Private key PEM (*or* `EC2_SSH_PRIVATE_KEY`) |
| `EC2_SSH_PRIVATE_KEY` | Alternate | Used if set; otherwise `EC2_SSH_KEY` |
| `EC2_PORT` | No | Defaults to `22` |

Do **not** configure AWS credentials, IAM OIDC roles, or ECR registry variables for Backend deploy.

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

Executed over SSH in `deploy_path`:

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

Existing on-host helpers remain available: `./deploy.sh`, `./scripts/deploy/rollback.sh`, `./scripts/deploy/verify-production.sh`.

---

## Rollback procedure

### Automatic (Deploy Backend workflow)

On build/up/health failure the remote script:

1. Checks out the previous release tag from `.deploy-state/previous_release_tag`
2. Rebuilds and `up -d` that tag
3. Leaves failure status so the Actions run is red

### Manual

```bash
cd /home/ubuntu   # or your deploy_path
PREV=$(cat .deploy-state/previous_release_tag)
git fetch --all --tags
git checkout "$PREV"
git submodule update --init --recursive
docker compose -f docker-compose.production.yml --profile flower down
docker compose -f docker-compose.production.yml --profile flower build --pull
docker compose -f docker-compose.production.yml --profile flower up -d
curl -fsS http://127.0.0.1:8080/api/v1/analysis/health/ready/
```

Or:

```bash
ROLLBACK_REF=<previous-tag-or-sha> ./scripts/deploy/rollback.sh
```

If the database schema is newer than the rolled-back code, restore DB from `backups/deploy` before traffic returns — see [Production-Deployment-Guide.md](../deploy/Production-Deployment-Guide.md).

---

## Operator checklist

1. Qualify tag with **Backend Release** (`dry_run=true`).
2. Confirm secrets `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`.
3. Dispatch **Deploy Backend** with `release_tag=<qualified tag>`.
4. Confirm Actions log: compose ps + health 200.
5. Smoke-test portal URL externally if applicable.

**STOP:** Do not treat Build Host image digests as production registry artifacts — EC2 builds from the checked-out tag.
