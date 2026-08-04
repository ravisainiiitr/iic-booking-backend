#Requires -Version 5.1
<#
.SYNOPSIS
  Operator gate: verify Build Host is ready for runner registration / dry runs.
  Does not install software. Does not register runners. Does not call AWS mutably.
#>
param(
    [string]$BuildRoot = 'C:\iic-build',
    [long]$MinFreeGb = 40,
    [int]$MinLogicalCpus = 4,
    [long]$MinRamGb = 16
)

$ErrorActionPreference = 'Continue'
$script:failed = 0
$script:warned = 0

function Write-Pass([string]$Name, [string]$Detail) {
    Write-Host "PASS  $Name : $Detail"
}
function Write-Fail([string]$Name, [string]$Detail, [string]$Fix) {
    Write-Host "FAIL  $Name : $Detail"
    Write-Host "      REMEDIATION: $Fix"
    $script:failed++
}
function Write-WarnItem([string]$Name, [string]$Detail, [string]$Fix) {
    Write-Host "WARN  $Name : $Detail"
    Write-Host "      REMEDIATION: $Fix"
    $script:warned++
}

Write-Host '=== Verify-BuildHostReady ==='
Write-Host ("Time={0}" -f (Get-Date).ToString('o'))

# Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isAdmin) { Write-Pass 'Administrator' 'Session elevated' }
else { Write-Fail 'Administrator' 'Session not elevated' 'Re-open PowerShell via Run as Administrator' }

# PowerShell 7
$pwsh = Get-Command pwsh -ErrorAction SilentlyContinue
if ($pwsh) {
    $ver = & pwsh -NoLogo -Command '$PSVersionTable.PSVersion.ToString()'
    if ([version]$ver -ge [version]'7.0') { Write-Pass 'PowerShell7' $ver }
    else { Write-Fail 'PowerShell7' "Version $ver too old" 'Install PowerShell 7.4+' }
} else {
    Write-Fail 'PowerShell7' 'pwsh not on PATH' 'winget install Microsoft.PowerShell'
}

# Git
try {
    $gv = (git --version)
    Write-Pass 'Git' $gv
} catch { Write-Fail 'Git' 'git missing' 'Install Git for Windows' }

# .NET SDK 8
try {
    $sdks = (& dotnet --list-sdks) -join '; '
    if ($sdks -match '(^|\s)8\.') { Write-Pass 'DotNetSdk8' $sdks }
    else { Write-Fail 'DotNetSdk8' "No 8.x SDK (found: $sdks)" 'Install .NET SDK 8.x' }
} catch { Write-Fail 'DotNetSdk8' 'dotnet missing' 'Install .NET SDK 8' }

# Node 20
try {
    $nv = (node -v)
    if ($nv -match '^v20\.') { Write-Pass 'Node20' $nv }
    else { Write-WarnItem 'Node20' "Found $nv (want v20 LTS)" 'Install Node.js 20 LTS for release builds' }
    Write-Pass 'npm' (npm -v)
} catch { Write-Fail 'Node' 'node/npm missing' 'Install Node.js 20 LTS' }

# Docker Desktop / Engine / Compose
$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
    Write-Fail 'DockerCLI' 'docker not on PATH' 'Install Docker Desktop and restart shell'
} else {
    try {
        $dv = docker version --format '{{.Server.Version}}' 2>&1
        if ($LASTEXITCODE -eq 0 -and "$dv" -match '\d') { Write-Pass 'DockerEngine' "Server $dv" }
        else { Write-Fail 'DockerEngine' "$dv" 'Start Docker Desktop; enable Linux engine / WSL2 backend' }
    } catch { Write-Fail 'DockerEngine' $_.Exception.Message 'Start Docker Desktop' }
    try {
        $cv = docker compose version 2>&1
        if ($LASTEXITCODE -eq 0) { Write-Pass 'DockerCompose' "$cv" }
        else { Write-Fail 'DockerCompose' "$cv" 'Repair Docker Desktop Compose plugin' }
    } catch { Write-Fail 'DockerCompose' $_.Exception.Message 'Repair Docker Desktop' }
}

$buildkit = [Environment]::GetEnvironmentVariable('DOCKER_BUILDKIT', 'Machine')
if ($buildkit -eq '1' -or $env:DOCKER_BUILDKIT -eq '1') { Write-Pass 'BuildKit' 'DOCKER_BUILDKIT=1' }
else { Write-WarnItem 'BuildKit' 'DOCKER_BUILDKIT not set to 1' 'Set machine env DOCKER_BUILDKIT=1; restart shells' }

# WSL2 + Ubuntu
try {
    $st = (wsl --status) 2>&1 | Out-String
    if ($st -match 'Default Version:\s*2' -or $st -match '2') { Write-Pass 'WSL2' 'Default version 2 (or WSL2 available)' }
    else { Write-WarnItem 'WSL2' $st.Trim() 'wsl --set-default-version 2' }
} catch { Write-Fail 'WSL2' 'wsl status failed' 'Install WSL2 Windows features' }

try {
    $list = (wsl -l -v) 2>&1 | Out-String
    if ($list -match 'Ubuntu') { Write-Pass 'UbuntuDistro' ($list -replace '\0','').Trim() }
    else { Write-Fail 'UbuntuDistro' 'No Ubuntu distribution' 'wsl --install -d Ubuntu-22.04' }
} catch { Write-Fail 'UbuntuDistro' $_.Exception.Message 'Install Ubuntu via wsl --install' }

# AWS CLI / gh
try { Write-Pass 'AwsCli' (aws --version 2>&1 | Out-String).Trim() }
catch { Write-Fail 'AwsCli' 'aws missing' 'Install AWS CLI v2' }
try { Write-Pass 'GitHubCli' ((gh --version 2>&1 | Select-Object -First 1) -join ' ') }
catch { Write-WarnItem 'GitHubCli' 'gh missing (optional for bootstrap)' 'winget install GitHub.cli' }

# VS Build Tools
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path $vswhere) {
    $path = & $vswhere -products * -requires Microsoft.Component.MSBuild -property installationPath
    if ($path) { Write-Pass 'VSBuildTools' "$path" }
    else { Write-Fail 'VSBuildTools' 'vswhere found but MSBuild workload missing' 'Install Managed Desktop Build Tools workload' }
} else {
    Write-Fail 'VSBuildTools' 'vswhere.exe missing' 'Install VS 2022 Build Tools'
}

# Disk / CPU / RAM
$sys = Get-CimInstance Win32_OperatingSystem
$ramGb = [math]::Round($sys.TotalVisibleMemorySize / 1MB, 1)
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
$freeC = [math]::Round((Get-PSDrive C).Free / 1GB, 1)
if ($cpu -ge $MinLogicalCpus) { Write-Pass 'CPU' "$cpu logical processors" }
else { Write-Fail 'CPU' "$cpu logical (< $MinLogicalCpus)" 'Use a host with more CPU' }
if ($ramGb -ge $MinRamGb) { Write-Pass 'RAM' "${ramGb} GB" }
else { Write-Fail 'RAM' "${ramGb} GB (< $MinRamGb)" 'Add memory' }
if ($freeC -ge $MinFreeGb) { Write-Pass 'DiskC' "${freeC} GB free" }
else { Write-Fail 'DiskC' "${freeC} GB free (< $MinFreeGb)" 'Free disk or expand volume' }

# Build root
foreach ($d in @($BuildRoot, "$BuildRoot\repos", "$BuildRoot\artifacts", "$BuildRoot\logs", "$BuildRoot\runners", "$BuildRoot\tools")) {
    if (Test-Path $d) { Write-Pass "Dir:$d" 'exists' }
    else { Write-WarnItem "Dir:$d" 'missing' "Run Initialize-BuildDirectories.ps1 -BuildRoot $BuildRoot" }
}

# Connectivity
foreach ($item in @(
    @{ n = 'GitHub'; u = 'https://github.com' },
    @{ n = 'NuGet'; u = 'https://api.nuget.org/v3/index.json' },
    @{ n = 'npm'; u = 'https://registry.npmjs.org/' },
    @{ n = 'AWS'; u = 'https://aws.amazon.com' }
)) {
    try {
        $r = Invoke-WebRequest -Uri $item.u -UseBasicParsing -TimeoutSec 25
        Write-Pass ("Net:" + $item.n) ("HTTP " + $r.StatusCode)
    } catch {
        Write-Fail ("Net:" + $item.n) $_.Exception.Message 'Fix outbound HTTPS / proxy / SSL inspection'
    }
}

Write-Host '=== SUMMARY ==='
if ($script:failed -gt 0) {
    Write-Host "RESULT=FAIL failures=$($script:failed) warnings=$($script:warned)"
    Write-Host 'Not ready for runner registration. Fix FAIL items and re-run.'
    exit 1
}
Write-Host "RESULT=PASS warnings=$($script:warned)"
Write-Host 'Host meets Verify-BuildHostReady gate (review WARN items before RC1 build).'
exit 0
