# Build Host Offline Package Manifest

Download these artifacts onto removable media or an internal share before commissioning a build host with limited internet.

**Operator:** fill SHA256 after download (`Get-FileHash -Algorithm SHA256`). Sizes are approximate.

| Component | URL (canonical) | Suggested filename | SHA256 | Approx size |
|---|---|---|---|---|
| PowerShell 7 Windows x64 MSI | https://github.com/PowerShell/PowerShell/releases | `PowerShell-7.x.x-win-x64.msi` | `TBD` | ~100 MB |
| Git for Windows | https://git-scm.com/download/win | `Git-2.x.x-64-bit.exe` | `TBD` | ~70 MB |
| VS 2022 Build Tools bootstrapper | https://aka.ms/vs/17/release/vs_BuildTools.exe | `vs_BuildTools.exe` | `TBD` | ~5 MB (+ layout cache multi-GB) |
| VS Build Tools offline layout | Created via `vs_BuildTools.exe --layout` | `vs2022-buildtools-layout\` | `TBD` | ~5–10 GB |
| .NET SDK 8 Windows x64 | https://dotnet.microsoft.com/download/dotnet/8.0 | `dotnet-sdk-8.x.x-win-x64.exe` | `TBD` | ~200 MB |
| Node.js 20 LTS x64 MSI | https://nodejs.org/dist/ | `node-v20.x.x-x64.msi` | `TBD` | ~30 MB |
| Docker Desktop Installer | https://docs.docker.com/desktop/setup/install/windows-install/ | `Docker Desktop Installer.exe` | `TBD` | ~500 MB |
| AWS CLI v2 MSI | https://aws.amazon.com/cli/ | `AWSCLIV2.msi` | `TBD` | ~40 MB |
| GitHub CLI Windows amd64 | https://github.com/cli/cli/releases | `gh_x.y.z_windows_amd64.msi` | `TBD` | ~15 MB |
| Syft Windows amd64 | https://github.com/anchore/syft/releases | `syft_x.y.z_windows_amd64.zip` | `TBD` | ~20 MB |
| Trivy Windows 64-bit | https://github.com/aquasecurity/trivy/releases | `trivy_x.y.z_windows-64bit.zip` | `TBD` | ~50 MB |
| GitHub Actions Runner win-x64 | https://github.com/actions/runner/releases | `actions-runner-win-x64-x.y.z.zip` | `TBD` | ~100 MB |
| Ubuntu WSL rootfs (optional offline) | Microsoft Store / `wsl --export` from a prepared host | `ubuntu-22.04.tar` | `TBD` | ~500 MB–2 GB |
| This repo release docs + scripts | Internal git bundle / zip of `scripts/build-host` + `docs/release` | `iic-build-host-docs.zip` | `TBD` | &lt; 5 MB |

## Notes

1. Prefer generating a **VS Build Tools layout** on a connected machine for fully offline VS installs.  
2. Docker Desktop still expects Windows Features (WSL2/Hyper-V) enabled on the target.  
3. After offline install, still verify with `Verify-BuildHostReady.ps1`.  
4. Do not store production portal secrets on offline media.
