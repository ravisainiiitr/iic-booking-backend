#Requires -Version 5.1
<#
.SYNOPSIS
  Verify HEAD matches an annotated release tag peel (or lightweight tag target).
#>
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$ExpectedSha = ''
)

$ErrorActionPreference = 'Stop'

git fetch --tags --force origin 2>$null | Out-Null
git checkout --detach "refs/tags/$Tag" 2>$null
if ($LASTEXITCODE -ne 0) {
    git checkout --detach $Tag
    if ($LASTEXITCODE -ne 0) { throw "Failed to checkout tag $Tag" }
}

$head = (git rev-parse HEAD).Trim()
$peeled = (git rev-list -n 1 $Tag).Trim()
Write-Host "TAG=$Tag"
Write-Host "HEAD=$head"
Write-Host "PEELED=$peeled"

if ($head -ne $peeled) {
    throw "HEAD ($head) does not match tag peel ($peeled)"
}

if ($ExpectedSha -and ($head -ne $ExpectedSha.Trim())) {
    throw "HEAD ($head) does not match expected SHA ($ExpectedSha)"
}

# Prefer annotated tags for releases
$objType = (git cat-file -t $Tag 2>$null)
if ($objType -eq 'commit') {
    Write-Warning "Tag $Tag is lightweight (points directly at commit). Prefer annotated tags for releases."
}

Write-Output $head
