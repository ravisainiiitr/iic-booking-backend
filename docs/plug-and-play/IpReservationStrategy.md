# IP Reservation Strategy

## Phase 1 — Soft reservation (default)

1. Wizard announces MAC + hostname + MachineGuid to DSA.
2. DSA looks up `IpReservation` by MAC.
3. If known: reuse `PreferredIp` in announce response / config pack.
4. If unknown: leave preferred IP null; **DHCP** remains default.
5. Observed IP is registered on the PC registration row (`AssignedIp` / last seen).
6. **Static apply** only when `network_mode=static` on the registration/template:
   - Config pack includes PreferredIp, SubnetMask, Gateway, DNS
   - Wizard writes intent under `%ProgramData%\DepartmentSyncAgent\EquipmentPcWizard\pending-static-ip.json`
   - Forced campus-wide static is **out of scope** for Phase 1

## Phase 2 — Full allocator (deferred)

- IP pool, gateway, DNS per department/building
- LAN conflict scan before assign
- netsh/static apply behind admin toggle
- Optional Portal mirror of reservations for multi-DSA visibility

## Data (DSA SQLite)

- `EquipmentPcRegistration` — Equipment UUID, Computer Name, MAC, Preferred/Assigned IP, NetworkMode, LastSeen, Status
- `IpReservation` — MAC (unique), PreferredIp, EquipmentPcId, Status, LastSeen
