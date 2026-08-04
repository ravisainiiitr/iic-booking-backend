#Requires -Version 5.1
<#
.SYNOPSIS
  Emit scoped IAM trust + permission policy JSON for GitHub OIDC ECR push.
  Default trust is repository + environment scoped (NOT org-wide).
  Does not apply unless -Apply.
#>
param(
    [Parameter(Mandatory = $true)][string]$AwsAccountId,
    [Parameter(Mandatory = $true)][string]$GithubOrg,
    [string[]]$GithubRepos = @(
        'iic-booking-backend',
        'iic-booking-frontend'
    ),
    [string[]]$Environments = @('release-ecr'),
    [string]$RoleName = 'iic-gha-ecr-push',
    [string]$Region = 'ap-south-1',
    [string]$OutDir = '.\artifacts\aws',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# sub claim patterns:
#   repo:ORG/REPO:environment:ENV
#   repo:ORG/REPO:ref:refs/tags/v*
# Avoid repo:ORG/*:* (over-broad).
$subs = New-Object System.Collections.Generic.List[string]
foreach ($repo in $GithubRepos) {
    foreach ($envName in $Environments) {
        $subs.Add("repo:${GithubOrg}/${repo}:environment:${envName}")
    }
    $subs.Add("repo:${GithubOrg}/${repo}:ref:refs/tags/v*")
}
$subJson = ($subs | ForEach-Object { '"{0}"' -f $_ }) -join ",`n          "

$trust = @"
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GithubActionsEcrPushScoped",
      "Effect": "Allow",
      "Principal": { "Federated": "arn:aws:iam::${AwsAccountId}:oidc-provider/token.actions.githubusercontent.com" },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": [
            $subJson
          ]
        }
      }
    }
  ]
}
"@

$perm = @"
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrAuth",
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken"],
      "Resource": "*"
    },
    {
      "Sid": "EcrPush",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:CompleteLayerUpload",
        "ecr:InitiateLayerUpload",
        "ecr:PutImage",
        "ecr:UploadLayerPart",
        "ecr:BatchGetImage",
        "ecr:DescribeImages",
        "ecr:DescribeRepositories"
      ],
      "Resource": "arn:aws:ecr:${Region}:${AwsAccountId}:repository/iic/*"
    }
  ]
}
"@

$pull = @"
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability",
        "ecr:DescribeImages"
      ],
      "Resource": "arn:aws:ecr:${Region}:${AwsAccountId}:repository/iic/*"
    }
  ]
}
"@

$readme = @"
# OIDC trust scope (Phase C.1)

This generator NO LONGER emits org-wide ``repo:ORG/*:*``.

Subjects included:
$(($subs | ForEach-Object { "- $_" }) -join "`n")

Workflows that push must use GitHub Environment ``release-ecr`` so the
``environment:`` subject matches.

Do not apply with -Apply until AWS admin review is complete.
"@

Set-Content (Join-Path $OutDir 'oidc-trust.json') $trust -Encoding utf8
Set-Content (Join-Path $OutDir 'ecr-push-policy.json') $perm -Encoding utf8
Set-Content (Join-Path $OutDir 'ecr-pull-policy.json') $pull -Encoding utf8
Set-Content (Join-Path $OutDir 'OIDC-SCOPE.md') $readme -Encoding utf8
Write-Host "Wrote scoped policies to $OutDir"

if (-not $Apply) {
    Write-Warning 'Dry generate only. Pass -Apply to create role (requires iam permissions).'
    return
}

aws iam create-role --role-name $RoleName --assume-role-policy-document "file://$OutDir/oidc-trust.json" 2>$null
aws iam put-role-policy --role-name $RoleName --policy-name ecr-push --policy-document "file://$OutDir/ecr-push-policy.json"
Write-Host "Applied role $RoleName"
