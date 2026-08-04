#Requires -Version 5.1
<#
.SYNOPSIS
  Fail closed if publish prerequisites are missing (vars/secrets/env evidence).
#>
param(
    [Parameter(Mandatory = $true)][string]$EnvironmentName,
    [string[]]$RequiredVars = @(),
    [string[]]$RequiredSecretNames = @(),
    [string]$EcrRegistry = '',
    [switch]$RequirePublishDispatch,
    [string]$EventName = '',
    [string]$PublishFlag = ''
)

$ErrorActionPreference = 'Stop'
$errors = New-Object System.Collections.Generic.List[string]

Write-Host "=== Publish gate: environment=$EnvironmentName ==="

if ($RequirePublishDispatch) {
    if ($EventName -ne 'workflow_dispatch') {
        $errors.Add("Publish requires workflow_dispatch (got '$EventName'). Tag push cannot publish.")
    }
    if ($PublishFlag -ne 'true') {
        $errors.Add("Publish requires explicit input publish=true (got '$PublishFlag').")
    }
}

foreach ($v in $RequiredVars) {
    $val = [Environment]::GetEnvironmentVariable($v)
    if ([string]::IsNullOrWhiteSpace($val)) {
        $errors.Add("Missing required variable/env '$v'. Set GitHub Actions variable or job env.")
    } else {
        Write-Host "PASS var $v is set"
    }
}

if ($EcrRegistry) {
    if ($EcrRegistry -notmatch '\.dkr\.ecr\.|\.amazonaws\.com|ghcr\.io') {
        Write-Warning "ECR_REGISTRY value looks unusual: $EcrRegistry"
    }
    Write-Host "PASS ECR_REGISTRY provided"
} elseif ($RequiredVars -contains 'ECR_REGISTRY') {
    # already counted
}

foreach ($s in $RequiredSecretNames) {
    # Secrets are not enumerable; callers pass a sentinel env that must be non-empty after mapping.
    $val = [Environment]::GetEnvironmentVariable($s)
    if ([string]::IsNullOrWhiteSpace($val)) {
        $errors.Add("Missing required secret mapping '$s'. Configure GitHub Environment/Repository secret and map it to env.")
    } else {
        Write-Host "PASS secret mapping $s is present (value redacted)"
    }
}

Write-Host @"
ACTIONABLE SETUP (if failed):
1. Create GitHub Environment '$EnvironmentName' with required reviewers.
2. Add repository/org variables: AWS_REGION, ECR_REGISTRY, ECR_PREFIX.
3. Add Environment secret AWS_ROLE_ARN_ECR_PUSH (OIDC role ARN).
4. Publish only via workflow_dispatch with publish=true and dry_run=false.
"@

if ($errors.Count -gt 0) {
    Write-Host "PUBLISH_GATE_FAILED"
    foreach ($e in $errors) { Write-Host " - $e" }
    exit 1
}

Write-Host "PUBLISH_GATE_OK"
exit 0
