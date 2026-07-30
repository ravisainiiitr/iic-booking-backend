# Guacamole Security Review (Phase 3)

## Trust boundaries

| Boundary | Control |
|----------|---------|
| End-user browser ↔ Portal | Portal Session/Token auth + CSRF for browser POSTs |
| Portal ↔ Guacamole API | Admin credentials server-only; internal API URL never returned |
| Browser ↔ Guacamole UI | Ephemeral Guacamole user token after Portal one-time token consume |
| guacd ↔ Analysis PC | RDP with `WorkstationRdpSecret` (encrypted at rest) |

## Authentication integration

- Users **do not** log into Guacamole with personal accounts.  
- Portal authenticates the user, then creates an ephemeral Guacamole user + connection, then issues a short-lived Portal launch token, then mints a Guacamole user token server-side.  
- Launch tokens: hashed at rest, single-use, optional IP bind, short TTL.

## Authorization

- Booking owner only for launch.  
- Manage RBAC for terminate/observe.  
- Re-check eligibility + analysis window + single-session + workstation health at create/launch.  
- Rejected attempts audited with reason + IP.

## Transport / HTTPS

- Public Guacamole and Portal should be HTTPS.  
- `verify_tls` applies to Portal→Guac API calls.  
- Never expose `guacamole_api_url`, admin password, workstation IPs, or RDP passwords in serializers/HTML.

## Client policies

| Feature | Policy |
|---------|--------|
| Clipboard | Configurable; mapped to Guac disable-copy/paste |
| File transfer / drive | Default off; policy UPLOAD_ONLY / DOWNLOAD_ONLY / both |
| Printing | Disabled in connection parameters |
| Audio | Configurable |
| Multi-monitor | Guacamole/RDP client behavior; not Portal-managed |
| Keyboard mapping | OS / Guacamole client; not Portal-managed |
| Session recording | **Not implemented** (`recording_enabled` forced false) |

## Secrets hardening (Phase 3)

Ephemeral Guacamole user passwords are stored as Fernet ciphertext in `GuacamoleConnection.metadata.temp_password_encrypted` (not plaintext). Legacy plaintext `temp_password` keys are still read if present for migration.

## Residual risks

- Guacamole admin password strength and rotation is an ops responsibility.  
- RDP network path must be firewalled to guacd only.  
- Mock mode must remain off in production (`DEBUG=False` readiness checks warn when mock is on).  
- Session recording remains future work.
