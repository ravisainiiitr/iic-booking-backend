#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$sdks = & dotnet --list-sdks 2>$null
if ($sdks -match '^8\.') {
  Write-Host "NET SDK 8.x present:`n$sdks"
  return
}
if (Get-Command winget -ErrorAction SilentlyContinue) {
  winget install --id Microsoft.DotNet.SDK.8 -e --accept-source-agreements --accept-package-agreements
} else {
  throw 'Install .NET SDK 8 from https://dotnet.microsoft.com/download'
}
