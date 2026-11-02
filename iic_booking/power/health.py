"""Running the UPS health check: assess, word the summary, store it, tell the officers if something is due."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from . import advisor, analysis, notify
from .models import UpsDevice, UpsHealthAssessment

REUSE_SUMMARY_FOR = timedelta(hours=24)


def _signature(findings: list[dict]) -> list:
    return sorted((f["code"], f["level"]) for f in findings)


def run(device: UpsDevice, *, now=None, gateway=None, send=True) -> UpsHealthAssessment:
    now = now or timezone.now()
    result = analysis.assess(device, now=now)
    findings = result.findings_dicts()
    names = [e.name for e in device.equipment.all()]
    previous = UpsHealthAssessment.objects.filter(device=device).order_by("-created_at").first()
    if previous is not None and previous.summary and _signature(previous.findings) == _signature(findings) \
            and now - previous.created_at < REUSE_SUMMARY_FOR:
        # Same findings as an hour ago: keep the wording instead of asking the model again.
        summary, source, model = previous.summary, previous.summary_source, previous.summary_model
    else:
        summary, source, model = advisor.summarise(device, result, names, gateway=gateway)
    row = UpsHealthAssessment.objects.create(
        device=device, level=result.level, risk_score=result.risk_score, findings=findings, metrics=result.metrics,
        summary=summary, summary_source=source, summary_model=model)
    if send:
        notify.health_notice(device, row, now=now)
    # Keep 90 days of assessments.
    UpsHealthAssessment.objects.filter(device=device, created_at__lt=now - timedelta(days=90)).delete()
    return row


def run_all(*, now=None) -> dict:
    from . import service

    service.check_silent(now)
    done = 0
    for device in UpsDevice.objects.filter(enabled=True).select_related("department").prefetch_related("equipment"):
        run(device, now=now)
        done += 1
    return {"assessed": done}
