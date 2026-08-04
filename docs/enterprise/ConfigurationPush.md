# Configuration Push

1. Admin applies `EquipmentSyncTemplate` or POSTs profile change via Lab API.
2. Portal records `ConfigurationChange`, bumps `configuration_version`, sets `bootstrap_required`.
3. DSA heartbeat receives `bootstrap_required` → fetches bootstrap (signed pack).
4. DSA applies locally / pushes Equipment PC config pack.
5. DSA POSTs `/api/v1/lab/configuration/ack/` with agent auth.
6. Dashboard shows Applied / Failed via `ConfigurationAck`.

Rollback: POST `/api/v1/lab/configuration/profiles/{id}/rollback/` with `change_id` restores previous snapshot and bumps version again.
