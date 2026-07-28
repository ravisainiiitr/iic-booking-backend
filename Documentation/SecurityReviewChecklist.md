# Security Review Checklist

Remote Analysis Platform — Milestone 8 / RC1.

## RBAC & authorization

- [x] `CanManageRemoteAnalysis` / `CanViewRemoteAnalysis` on management APIs
- [x] Agent authenticated via bearer + agent id (`IsRemoteAnalysisAgent`)
- [x] Session launch/terminate ownership checks
- [x] Workspace access + share permission checks
- [x] Sharing never anonymous; audited
- [ ] Periodic access review of manage grants (operational)

## Tokens & sessions

- [x] Launch tokens short-lived (`launch_token_lifetime_seconds`)
- [x] Optional IP bind (`bind_token_to_ip`)
- [x] Session idle / absolute timeouts
- [x] Agent tokens server-side; not returned to browsers beyond registration flow controls
- [ ] Production token rotation schedule documented for ops

## Workspace isolation

- [x] Per-workspace `storage_key` root
- [x] `absolute_path` resolve + escape check
- [x] Explicit `..` rejection on write
- [x] Quota enforcement
- [x] Extension allow/block lists in settings

## Uploads / files

- [x] Size limits
- [x] Virus scanner hook (default noop — enable before high-risk labs)
- [x] Checksum verification on download

## Secrets

- [x] Guacamole admin password stored server-side only
- [x] Guacamole API URL not for browser clients
- [x] `mask_secret` helper for logs
- [ ] Secrets in env/secret manager (deployment responsibility)
- [ ] No secrets in frontend bundles (verify on release)

## Transport & headers

- [ ] HTTPS via Traefik / reverse proxy in production
- [ ] Secure cookies / CSRF for session auth as per Django production settings
- [ ] HSTS / secure headers at edge

## Audit

- [x] WorkstationEvent / SessionAudit / WorkspaceAudit / Collaboration & Assistance categories
- [x] Share, invite, help, notification events recorded
- [ ] Log retention aligned with policy

## Commands

- [x] Commands created by Portal for known workstations
- [x] Agent executes only polled commands for its agent_id

## Replay / rate limits

- [x] One-time launch tokens
- [ ] Edge rate limiting hooks (Traefik/WAF) recommended
- [x] Celery task idempotency for expire/purge/reminder patterns

## Production gate

- [ ] `DEBUG=False`
- [ ] `mock_guacamole=False`
- [ ] Strong `SECRET_KEY`
- [ ] DB not publicly exposed
- [ ] Redis authenticated/network-restricted

**RC1 security posture:** Acceptable for controlled enterprise pilot after Production gate items are closed.
