"""Turn the scheduled monthly usage reports on, guarded: status (default, read-only) or --apply.

Prints booleans, counts and schedule times only (safe for public CI logs). Never sends or queues an email: the first
real send is the scheduled beat run.

    python manage.py monthly_reports_switch            # status
    python manage.py monthly_reports_switch --apply    # master + all three audiences on (Excel left as is)
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from iic_booking.communication.models import CommunicationLog, MonthlyReportSendLog, MonthlyReportSettings
from iic_booking.communication.monthly_report_data import IST, ReportMonth

FIELDS = ("enabled", "faculty_enabled", "oic_enabled", "hod_enabled", "attach_excel")
TURN_ON = {"enabled": True, "faculty_enabled": True, "oic_enabled": True, "hod_enabled": True}
TASK = "communication.send_monthly_usage_reports"


def _settings() -> dict:
    row = MonthlyReportSettings.get_singleton()
    return {f: getattr(row, f) for f in FIELDS}


def _beat() -> list[dict]:
    from django_celery_beat.models import PeriodicTask

    now = timezone.now()
    out = []
    for task in PeriodicTask.objects.filter(task=TASK).select_related("crontab"):
        info = {"id": task.pk, "enabled": task.enabled, "last_run_at": task.last_run_at, "next_run_at": None}
        if task.crontab_id:
            c = task.crontab
            info["crontab"] = f"{c.minute} {c.hour} {c.day_of_month} {c.month_of_year} {c.day_of_week}"
            info["crontab_timezone"] = str(getattr(c, "timezone", ""))
            info["next_run_at"] = (now + c.schedule.remaining_estimate(task.last_run_at or now)).astimezone(IST)
        out.append(info)
    return out


def _logs(month: ReportMonth) -> dict:
    sends = MonthlyReportSendLog.objects.filter(month=month.start)
    tests = CommunicationLog.objects.filter(
        metadata__notification_category="monthly_reports", metadata__monthly_report_month=month.key
    )
    return {
        "month": month.key,
        "send_log_rows": sends.count(),
        "test_send_emails": tests.filter(metadata__test_send=True).count(),
        "non_test_emails": tests.exclude(metadata__test_send=True).count(),
    }


def _old_pdf() -> dict:
    from iic_booking.equipment.tasks import oic_monthly_pdf_recipients

    def counts(include_oics: bool) -> dict:
        per = oic_monthly_pdf_recipients(include_oics=include_oics)
        return {
            "equipment": len(per),
            "emails": sum(len(v) for v in per.values()),
            "people": len({u.pk for v in per.values() for u in v}),
        }

    return {"with_oics": counts(True), "operators_only": counts(False)}


class Command(BaseCommand):
    help = "Status of the monthly usage reports switch, or --apply to turn it on for all audiences."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        local = timezone.now().astimezone(IST)
        current = ReportMonth(local.year, local.month)
        before = _settings()
        out = {
            "mode": "apply" if opts["apply"] else "status",
            "settings_before": before,
            "would_set": TURN_ON,
            "beat": _beat(),
            "logs_current_month": _logs(current),
            "logs_previous_month": _logs(current.previous),
            "old_pdf_recipients": _old_pdf(),
        }
        if opts["apply"]:
            row = MonthlyReportSettings.get_singleton()
            for field, value in TURN_ON.items():
                setattr(row, field, value)
            row.save(update_fields=[*TURN_ON, "updated_at"])
            after = _settings()
            out["settings_after"] = after
            out["verified"] = all(after[k] == v for k, v in TURN_ON.items()) and after["attach_excel"] == before["attach_excel"]
            out["send_log_rows_total_after"] = MonthlyReportSendLog.objects.count()
        self.stdout.write(json.dumps(out, default=str, indent=2))
