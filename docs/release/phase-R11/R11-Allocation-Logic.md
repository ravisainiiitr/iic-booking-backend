# R11 — Allocation Logic

Booking → Equipment → Required Software → Eligible RAA PCs (inventory + allocation_enabled + online + not busy/maintenance) → allocate best.

- No hard equipment→RAA binding (pool is soft boost only).
- Multiple PCs with same software: next free PC is selected when one is busy.
- Stale inventory (`INVENTORY_STALE_SECONDS`) excludes a PC.
