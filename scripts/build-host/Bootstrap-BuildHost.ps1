#Requires -Version 5.1
<#
.SYNOPSIS
  Master idempotent bootstrap for the IIC Windows Build Host.
.NOTES
  Does NOT register the GitHub runner unless -RegisterRunner is passed (and token provided).
  Do not run against production EC2.
#>
param(
    [switch]$RegisterRunner,
    [string]$RunnerToken = '',
    [string]$RunnerUrl = 'https://github.com/ravisainiiitr',
    [string]$BuildRoot = 'C:\iic-build'
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

Write-Host "=== IIC Build Host Bootstrap ==="
Write-Host "BuildRoot=$BuildRoot"

& "$here\Initialize-BuildDirectories.ps1" -BuildRoot $BuildRoot
& "$here\Install-Git.ps1"
& "$here\Install-PowerShell7.ps1"
& "$here\Install-DotNetSdk.ps1"
& "$here\Install-NodeJs.ps1"
& "$here\Install-VSBuildTools.ps1"
& "$here\Install-DockerDesktop.ps1"
& "$here\Install-AwsCli.ps1"
& "$here\Configure-Docker.ps1"
& "$here\Install-GitHubRunner.ps1" -BuildRoot $BuildRoot

if ($RegisterRunner) {
    if (-not $RunnerToken) { throw 'RunnerToken required with -RegisterRunner' }
    & "$here\Register-GitHubRunner.ps1" -BuildRoot $BuildRoot -Url $RunnerUrl -Token $RunnerToken
}

& "$here\Configure-WindowsServices.ps1"
& "$here\Verify-BuildHost.ps1"

Write-Host "=== Bootstrap complete ==="
