# Fleet Architecture

Laboratory Infrastructure aggregates existing DSA and RA sources into one Main Admin tree:

Department → DSA nodes → Equipment PC children (from DSA heartbeat rollup)  
Remote Analysis bucket → Analysis PC / RAA nodes (from `fleet_inventory`)

Status enums: online, offline, synchronizing, busy, maintenance, commissioning, error, waiting.

Reuse: `fleet_inventory.py`, `AgentHeartbeat`, `WorkstationHeartbeat`, M15 monitoring, M16 updates.
