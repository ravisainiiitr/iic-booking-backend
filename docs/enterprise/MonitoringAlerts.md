# Monitoring and Alerts

`GET /api/v1/lab/alerts/` merges `LabAlert` with open DSA `AlertEvent` rows.

Detectors (`run_lab_health_detectors`):

- Heartbeat timeout (DSA / Analysis PC)
- Configuration drift
- Disk nearly full (Equipment PC rollup)
- Equipment PC last_error
- Duplicate Analysis PC registrations

Severities: warning, error, critical. Email/SMS escalation remains future for SMS/WhatsApp; dashboard Ack is available for LabAlert rows.
