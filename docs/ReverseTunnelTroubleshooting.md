# Reverse Tunnel Troubleshooting

| Symptom | Check |
|---------|--------|
| Launch still uses PC hostname | Confirm reverse tunnel provisioned; Direct RDP is retired |
| JOIN_TUNNEL never arrives | Agent heartbeat; command poll; workstation allocation |
| Auth failure on WSS | Token secret mismatch; clock skew; expired token |
| guacd cannot connect | Adapter hostname DNS from guacd network; port allocate |
| Black screen / disconnect | Windows RDP enabled; NLA credentials; agent RDP to 127.0.0.1:3389 |
| Gateway down | Compose health; Toolkit tunnel tab |
| CSRF / dashboard redirects | Unrelated frontend/proxy; not tunnel |

Logs: Gateway stdout; Agent `C:\ProgramData\RemoteAnalysisAgent\Logs\`; Portal `TunnelEvent` rows.
