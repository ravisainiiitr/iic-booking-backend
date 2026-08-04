#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path $vswhere) {
  $path = & $vswhere -products * -requires Microsoft.Component.MSBuild -property installationPath
  if ($path) {
    Write-Host "VS Build Tools / MSBuild present: $path"
    return
  }
}
Write-Host 'Installing Visual Studio 2022 Build Tools via winget if available...'
if (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id Microsoft.VisualStudio.2022.BuildTools -e --accept-source-agreements --accept-package-agreements --override '--wait --passive --add Microsoft.VisualStudio.Workload.ManagedDesktopBuildTools'
} else {
  throw 'Install VS 2022 Build Tools manually: https://aka.ms/vs/17/release/vs_BuildTools.exe'
}
