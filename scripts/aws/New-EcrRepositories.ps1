#Requires -Version 5.1
<#
.SYNOPSIS
  Create ECR repositories, scanning, lifecycle (idempotent). DO NOT RUN until authorized.
#>
param(
    [string]$Region = 'ap-south-1',
    [string]$Prefix = 'iic',
    [string[]]$Repos = @(
        'booking-django',
        'booking-celeryworker',
        'booking-celerybeat',
        'booking-flower',
        'booking-frontend',
        'reverse-tunnel-gateway'
    )
)

$ErrorActionPreference = 'Stop'
Write-Warning "This script mutates AWS. Run only on an authorized admin session."

foreach ($r in $Repos) {
    $name = "$Prefix/$r"
    $exists = aws ecr describe-repositories --repository-names $name --region $Region 2>$null
    if ($LASTEXITCODE -ne 0) {
        aws ecr create-repository --repository-name $name --region $Region --image-scanning-configuration scanOnPush=true --encryption-configuration encryptionType=AES256 | Out-Null
        Write-Host "CREATED $name"
    } else {
        Write-Host "EXISTS $name"
        aws ecr put-image-scanning-configuration --repository-name $name --image-scanning-configuration scanOnPush=true --region $Region | Out-Null
    }

    $policy = @{
        rules = @(
            @{
                rulePriority = 1
                description  = 'Expire untagged after 14 days'
                selection    = @{ tagStatus = 'untagged'; countType = 'sinceImagePushed'; countUnit = 'days'; countNumber = 14 }
                action       = @{ type = 'expire' }
            },
            @{
                rulePriority = 2
                description  = 'Keep last 10 images'
                selection    = @{ tagStatus = 'any'; countType = 'imageCountMoreThan'; countNumber = 10 }
                action       = @{ type = 'expire' }
            }
        )
    } | ConvertTo-Json -Depth 8 -Compress

    $tmp = [System.IO.Path]::GetTempFileName()
    Set-Content -Path $tmp -Value $policy -Encoding ascii
    aws ecr put-lifecycle-policy --repository-name $name --lifecycle-policy-text "file://$tmp" --region $Region | Out-Null
    Remove-Item $tmp -Force
    Write-Host "LIFECYCLE $name"
}
