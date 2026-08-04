#Requires -Version 5.1
<#
.SYNOPSIS
  Verify build host toolchain. Exit 1 on hard failures.
#>
$ErrorActionPreference = 'Continue'
$failed = $false
function Test-Tool($Name, [scriptblock]$Block, [switch]$Required) {
  try {
    $out = & $Block
    Write-Host "PASS $Name : $out"
  } catch {
    if ($Required) {
      Write-Host "FAIL $Name : $_"
      $script:failed = $true
    } else {
      Write-Host "WARN $Name : $_"
    }
  }
}

Test-Tool 'git' { git --version } -Required
Test-Tool 'dotnet' { (dotnet --list-sdks | Out-String).Trim() } -Required
Test-Tool 'node' { node -v } -Required
Test-Tool 'npm' { npm -v } -Required
Test-Tool 'pwsh' { pwsh -NoLogo -Command '$PSVersionTable.PSVersion.ToString()' }
Test-Tool 'docker' { docker version --format '{{.Server.Version}}' } -Required
Test-Tool 'docker-compose' { docker compose version } -Required
Test-Tool 'aws' { aws --version }
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
Test-Tool 'vsbuildtools' {
  if (-not (Test-Path $vswhere)) { throw 'vswhere missing' }
  $p = & $vswhere -products * -requires Microsoft.Component.MSBuild -property installationPath
  if (-not $p) { throw 'MSBuild workload missing' }
  $p
} -Required

if ($failed) {
  Write-Host "VERIFY_FAILED"
  exit 1
}
Write-Host "VERIFY_OK"
exit 0
