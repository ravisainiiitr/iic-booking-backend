# Equipment Templates + Config Push

## Model

`EquipmentSyncTemplate` — reusable pack (share, folders, sync interval, network_mode, firewall, retry, required software, health thresholds).

## Apply flow

1. Admin creates/edits template in Django Admin.
2. **Apply** to an `EquipmentSyncProfile`.
3. Profile fields updated; `configuration_version` incremented.
4. Active `AgentAssignment` agents get `bootstrap_required=True`.
5. DSA heartbeat detects version mismatch → bootstrap refresh = **Configuration Push**.

## Bootstrap extensions

Each assigned equipment entry may include:

- `network_mode`
- `windows_account_policy`
- `folder_layout`
- `firewall_profile`
- `retry_policy`
- `required_software`
- `health_thresholds`
- `template_code`

These are also mirrored inside `enabled_features` for backward-compatible consumers.

## New equipment

Recommended: create Equipment → create Sync Profile → apply template → assign DSA agent.
