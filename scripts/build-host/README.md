# IIC Build Host scripts

Idempotent PowerShell bootstrap for the dedicated Windows Release Build Host.

## Quick start (on the build host, as Administrator)

```powershell
cd <clone>\scripts\build-host
.\Bootstrap-BuildHost.ps1
# After Docker Desktop is running:
.\Verify-BuildHost.ps1
# Register runner only with a registration token (do not commit tokens):
.\Bootstrap-BuildHost.ps1 -RegisterRunner -RunnerToken <TOKEN> -RunnerUrl https://github.com/ravisainiiitr
```

## Scripts

| Script | Purpose |
|---|---|
| `Bootstrap-BuildHost.ps1` | Master installer |
| `Initialize-BuildDirectories.ps1` | `C:\iic-build\...` layout |
| `Install-*.ps1` | Toolchain installers |
| `Configure-Docker.ps1` | BuildKit / engine checks |
| `Configure-WindowsServices.ps1` | Service notes |
| `Register-GitHubRunner.ps1` | Runner registration |
| `Verify-BuildHost.ps1` | Toolchain gate |
| `Verify-ReleaseInfrastructure.ps1` | + AWS/ECR checks |

Do **not** run these on production EC2.
