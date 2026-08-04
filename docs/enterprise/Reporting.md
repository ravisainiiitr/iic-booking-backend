# Reporting

## Utilization snapshot

`GET /api/v1/lab/reports/utilization/`  
`GET /api/v1/lab/reports/utilization/?format=csv`

## Software inventory / compliance

`GET /api/v1/lab/software/compliance/` — required vs installed on Analysis PCs.

## Existing RA reports

Reuse Remote Analysis ops weekly/monthly Celery report tasks (`remote_analysis.generate_weekly_reports`, etc.).

## Lab health detectors

`lab_infrastructure.run_health_detectors` every 5 minutes (Celery beat) or:

```bash
python manage.py run_lab_health_detectors
```
