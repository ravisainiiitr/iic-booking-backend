from celery import shared_task

TASK_NAME = "integration_health.run"


@shared_task(name=TASK_NAME, ignore_result=True)
def run_integration_health() -> dict:
    """Every 15 minutes: checks, live checks, safe fixes, and an email to the Main Administrators when something new
    goes wrong (or is resolved)."""
    from .report import run

    payload = run(heal=True, live=True, alert=True)
    return {"overall": payload["overall"], "fixes": payload["fixes"]}
