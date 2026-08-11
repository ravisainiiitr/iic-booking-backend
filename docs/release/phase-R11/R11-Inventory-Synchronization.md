# R11 — Inventory Synchronization

- Startup inventory + periodic refresh default **30 minutes**.
- Portal `InventoryService` upserts installs, promotes catalog entries, honors delta payloads.
- Heartbeat remains separate (health/online). Allocation requires recent heartbeat AND fresh inventory.
