# AWS release bootstrap scripts

Generate ECR repositories and **scoped** IAM/OIDC policy documents.

**Do not execute** until AWS admin authorization.

```powershell
# Create ECR repos + scanning + lifecycle
.\New-EcrRepositories.ps1 -Region ap-south-1 -Prefix iic

# Generate SCOPED OIDC trust (repo + environment:release-ecr + tag refs)
# NOT org-wide repo:ORG/*:*
.\New-EcrOidcRoles.ps1 -AwsAccountId <ACCOUNT> -GithubOrg ravisainiiitr
```

GitHub Environments required before first push: `release-build`, `release-ecr`, `deployment-center`, `platform-release`.
