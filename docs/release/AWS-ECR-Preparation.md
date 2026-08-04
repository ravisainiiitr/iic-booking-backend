# AWS ECR Preparation Checklist

**Planning only — create no AWS resources in this phase.**

Companion scripts (generate JSON only until `-Apply` is authorized): `scripts/aws/New-EcrRepositories.ps1`, `scripts/aws/New-EcrOidcRoles.ps1`.

---

## 1. OIDC provider

| Step | Detail |
|---|---|
| Provider URL | `https://token.actions.githubusercontent.com` |
| Audience | `sts.amazonaws.com` |
| Thumbprint | Per AWS GitHub OIDC docs (keep current) |
| Account | Same AWS account as ECR / production pull role |

---

## 2. IAM role (push)

| Field | Value |
|---|---|
| Role name | `iic-gha-ecr-push` |
| Trust | Scoped subjects — **not** `repo:ORG/*:*` |
| Example subjects | `repo:ravisainiiitr/iic-booking-backend:environment:release-ecr` |
|  | `repo:ravisainiiitr/iic-booking-frontend:environment:release-ecr` |
|  | `repo:…:ref:refs/tags/v*` (optional; prefer environment-gated push) |
| Permissions | `ecr:GetAuthorizationToken` + push actions on `repository/iic/*` |

Generate with:

```powershell
.\scripts\aws\New-EcrOidcRoles.ps1 -AwsAccountId <ACCOUNT> -GithubOrg ravisainiiitr
# Review artifacts\aws\oidc-trust.json — do NOT -Apply yet
```

---

## 3. IAM role (pull — production EC2 later)

| Field | Value |
|---|---|
| Role / instance profile | `iic-ecr-pull` |
| Permissions | BatchGetImage, GetDownloadUrlForLayer, etc. on `iic/*` |
| Attach | Production EC2 instance profile only |

---

## 4. ECR repositories

| Repository |
|---|
| `iic/booking-django` |
| `iic/booking-celeryworker` |
| `iic/booking-celerybeat` |
| `iic/booking-flower` |
| `iic/booking-frontend` |
| `iic/reverse-tunnel-gateway` |

Region: **`ap-south-1`**.

---

## 5. Repository policies

- Private by default  
- Allow push role to push  
- Allow pull role (prod) to pull  
- Deny anonymous  

---

## 6. Lifecycle rules

Suggested (from script):

- Expire **untagged** after 14 days  
- Keep last **10** images (tune for RC retention)  

Review before apply so RC tags are not pruned unexpectedly.

---

## 7. Scanning & encryption

| Control | Setting |
|---|---|
| Scan on push | Enabled |
| Encryption | AES256 (CMK optional later) |

---

## 8. GitHub wiring (later)

| Item | Value |
|---|---|
| Variable `AWS_REGION` | `ap-south-1` |
| Variable `ECR_REGISTRY` | `<account>.dkr.ecr.ap-south-1.amazonaws.com` |
| Variable `ECR_PREFIX` | `iic` |
| Secret `AWS_ROLE_ARN_ECR_PUSH` | Role ARN |
| Environment | `release-ecr` with required reviewers |

---

**STOP:** Do not create the provider, roles, or repositories until explicitly authorized.
