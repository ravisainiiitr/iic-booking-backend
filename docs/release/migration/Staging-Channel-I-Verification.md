# Staging Channel-I Verification

**Status:** LIVE qualification **PASS** on local staging (durable identity re-verification).

## Routing fact (verified)

| Host | Terminates on |
|------|----------------|
| `https://equip.iitr.ac.in/...` | **PRODUCTION** EC2 `3.110.50.174` |
| `http://127.0.0.1:8180/...` | Local Docker staging |
| `https://staging.equip.iitr.ac.in/...` | **DNS NOT_FOUND** (not deployed) |

Do **not** expect production `equip.iitr.ac.in` callback to reach local staging.

## Safe local staging callback (Option B)

Register with Omniport **in addition to** production (do not remove production URI):

```text
http://127.0.0.1:8180/api/auth/omniport/callback/
```

## Live proof (completed)

| Check | Result |
|-------|--------|
| Authorize | PASS (`fixture_mode=false`) |
| OAuth callback | PASS |
| Userinfo | PASS |
| Authoritative claim | `username` |
| `admin.users.emp_id` exact match | PASS (count = 1) |
| Fixture fallback | NONE |

Re-verification command path: `probe_live_channel_i_identity()` → evidence `real_channel_i_live_evidence.json`.

## Invalid

`/api/v1/auth/channel-i/callback/` — legacy wrong path.
