# GitHub Runner Installation (Preparation Only)

**Do NOT register the runner in this document’s scope until the commissioning gate allows READY FOR RUNNER REGISTRATION.**

---

## 1. Runner download

1. Open https://github.com/actions/runner/releases  
2. Download latest **actions-runner-win-x64-*.zip**  
3. Verify SHA256 against the release page  
4. Expand to:

```text
C:\iic-build\runners\actions-runner\
```

Expect `config.cmd`, `run.cmd`, `svc.cmd` present.

---

## 2. Directory layout

```text
C:\iic-build\runners\actions-runner\     # runner binaries + _work
C:\iic-build\logs\runner\               # optional redirected logs
C:\iic-build\tools\                     # syft/trivy on PATH for jobs
```

Working directory default: `_work` under the runner folder.

---

## 3. Service account

| Recommendation | Detail |
|---|---|
| Account | Dedicated local (or domain) user e.g. `svc_gha_build` |
| Rights | Log on as a service; access to `C:\iic-build`; Docker users group if required by Desktop |
| Secrets | No AWS long-lived keys in the account profile; prefer OIDC later |
| Interactive | Avoid daily interactive login as this account |

---

## 4. Labels

When registration is authorized, use **exactly**:

```text
self-hosted,windows,iic-build
```

Workflows select: `runs-on: [self-hosted, windows, iic-build]`.

Optional extra labels (do not replace required three): `x64`, `docker`.

---

## 5. Auto-start

After authorized registration:

```powershell
cd C:\iic-build\runners\actions-runner
.\svc.cmd install
.\svc.cmd start
.\svc.cmd status
```

Configure recovery: restart on failure (Windows Service properties).

---

## 6. Update policy

| Item | Policy |
|---|---|
| Runner version | Pin to a tested release; upgrade in maintenance window |
| OS patches | Monthly; re-run `Verify-BuildHostReady.ps1` after reboot |
| Docker Desktop | Upgrade only after a dry-run workflow passes |
| Toolchain (.NET/Node) | Stay on documented majors (SDK 8, Node 20) |

---

## 7. Registration (future — not now)

Operators will need a short-lived registration token from GitHub org/repo settings.  
Use `scripts\build-host\Register-GitHubRunner.ps1` only after gate approval.

**This phase ends before registration.**
