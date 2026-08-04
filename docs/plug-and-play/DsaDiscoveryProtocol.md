# DSA Discovery Protocol

## Preferred HTTP probe

```
GET http://192.168.1.100:6001/api/discovery/info
```

Configurable via DSA `DsaDiscovery:AdvertiseIpAddress` / `AdvertiseApiBase`.

Response (example):

```json
{
  "product": "DepartmentSyncAgent",
  "departmentName": "...",
  "computerName": "...",
  "version": "...",
  "ipAddress": "192.168.1.100",
  "apiBase": "http://192.168.1.100:6001",
  "pairingRequired": true
}
```

## UDP broadcast

- Port: **6010**
- Request: UTF-8 text `DSA-DISCOVER`
- Reply: `DSA-ADVERTISE` + JSON body (same fields as HTTP info)

Hosted service: `DsaDiscoveryAdvertiser` (starts with DSA API).

## Pairing

```
POST /api/pairing/issue
→ { "token": "...", "expiresAt": "..." }
```

Subsequent Wizard calls require header:

```
X-Pairing-Token: <token>
```

TTL default: 15 minutes (`DsaDiscovery:PairingTokenTtlMinutes`).

## Equipment PC endpoints

| Method | Path | Auth |
|--------|------|------|
| POST | `/api/equipment-pcs/announce` | Pairing |
| GET | `/api/equipment-pcs/{id}/config-pack` | Pairing |
| POST | `/api/equipment-pcs/{id}/validate` | Pairing |
| GET | `/api/equipment/list` | Pairing |

## LAN bind note

For discovery from another PC, DSA must bind non-loopback (`LocalApi:BindLoopbackOnly=false`, `RejectRemoteClients=false`) and advertise the LAN IP.
