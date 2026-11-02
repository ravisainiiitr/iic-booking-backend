"""Hourly UPS health check (Celery Beat schedule added by migration power 0002)."""

from celery import shared_task


@shared_task(name="power.assess_ups_health")
def assess_ups_health() -> dict:
    from .health import run_all

    return run_all()
