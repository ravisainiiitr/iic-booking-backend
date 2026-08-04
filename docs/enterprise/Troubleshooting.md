# Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Empty Equipment PC children | DSA not sending `equipment_pcs` / PCs not posting status | Check DSA `:6001` status API + LAN bind |
| Config never applied | Agent offline or ack failing | Check bootstrap_required, agent auth to `/lab/configuration/ack/` |
| Lab UI 403 | Not Main Admin | Use admin user_type / superuser |
| Alerts empty | Detectors not scheduled | Run `manage.py run_lab_health_detectors` |
| RAA update discover 401 | Installer latest requires admin | Expected until enrollment-key read path; logs locally |

Phase-1 Deployment Center and Equipment PC Wizard flows remain supported and unchanged.
