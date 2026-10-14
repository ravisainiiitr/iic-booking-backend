"""Main Administrator: monthly usage report settings, recipient search and in-browser preview (never sends)."""

from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from config.admin_extra_settings_api import IsMainAdmin

from .models import MonthlyReportSendLog, MonthlyReportSettings
from .monthly_report_data import ReportMonth
from .monthly_reports import AUDIENCES, active_hod_user_ids, preview

SETTING_FIELDS = ("enabled", "faculty_enabled", "oic_enabled", "hod_enabled", "attach_excel")


def _month(value) -> ReportMonth:
    latest = ReportMonth.previous_to()
    if not value:
        return latest
    month = ReportMonth.parse(value)
    current = timezone.localdate().replace(day=1)
    if month.start >= current:
        raise ValueError("Choose a month that has finished.")
    return month


def _settings_payload(row: MonthlyReportSettings) -> dict:
    last = MonthlyReportSendLog.objects.order_by("-month").values_list("month", flat=True).first()
    runs = None
    if last:
        counts = dict(
            MonthlyReportSendLog.objects.filter(month=last).values_list("status").annotate(n=Count("pk")).order_by()
        )
        runs = {"month": last.strftime("%Y-%m"), "counts": {s: counts.get(s, 0) for s in MonthlyReportSendLog.Status.values}}
    return {
        **{f: getattr(row, f) for f in SETTING_FIELDS},
        "updated_at": row.updated_at,
        "schedule": "1st of each month at 09:10 IST (never during the Wednesday slot-opening window)",
        "default_month": ReportMonth.previous_to().key,
        "last_run": runs,
    }


class MonthlyReportSettingsView(APIView):
    permission_classes = [IsMainAdmin]

    def get(self, request):
        return Response(_settings_payload(MonthlyReportSettings.get_singleton()))

    def patch(self, request):
        row = MonthlyReportSettings.get_singleton()
        changed = []
        for field in SETTING_FIELDS:
            if field in request.data:
                value = request.data.get(field)
                if not isinstance(value, bool):
                    return Response({"error": f"{field} must be true or false."}, status=status.HTTP_400_BAD_REQUEST)
                setattr(row, field, value)
                changed.append(field)
        if changed:
            row.updated_by = request.user
            row.save(update_fields=[*changed, "updated_by", "updated_at"])
        return Response(_settings_payload(row))

    put = patch


class MonthlyReportPreviewView(APIView):
    permission_classes = [IsMainAdmin]

    def get(self, request):
        from iic_booking.users.display import get_user_display_name
        from iic_booking.users.models import User

        try:
            month = _month(request.query_params.get("month"))
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user_id = int(request.query_params.get("user") or 0)
        except (TypeError, ValueError):
            user_id = 0
        user = User.objects.filter(pk=user_id).first() if user_id else None
        if user is None:
            return Response({"error": "Choose a recipient to preview."}, status=status.HTTP_400_BAD_REQUEST)
        data = preview(user, month)
        data["recipient"] = {
            "id": user.pk,
            "name": get_user_display_name(user),
            "is_test_account": bool(getattr(user, "is_test_account", False)),
        }
        return Response(data)


class MonthlyReportRecipientsView(APIView):
    """Faculty wallet owners, Officers in Charge and Heads of Department matching ``q`` (test accounts included)."""

    permission_classes = [IsMainAdmin]

    def get(self, request):
        from iic_booking.equipment.models import EquipmentManager
        from iic_booking.users.display import get_user_display_name
        from iic_booking.users.models import User
        from iic_booking.users.models.user_type import UserType

        q = str(request.query_params.get("q") or "").strip()
        audience = str(request.query_params.get("audience") or "").strip()
        managers = set(EquipmentManager.objects.values_list("manager_id", flat=True))
        hods = active_hod_user_ids()
        scopes = {
            "faculty": Q(user_type=UserType.FACULTY, wallet__isnull=False),
            "oic": Q(pk__in=managers),
            "hod": Q(pk__in=hods),
        }
        if audience in AUDIENCES:
            scope = scopes[audience]
        else:
            scope = scopes["faculty"] | scopes["oic"] | scopes["hod"]
        qs = User.objects.filter(is_active=True).filter(scope)
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q))
        rows = []
        for user in qs.distinct().order_by("-is_test_account", "name")[:25]:
            audiences = [
                a
                for a, member in (
                    ("faculty", user.user_type == UserType.FACULTY and hasattr(user, "wallet")),
                    ("oic", user.pk in managers),
                    ("hod", user.pk in hods),
                )
                if member
            ]
            rows.append(
                {
                    "id": user.pk,
                    "name": get_user_display_name(user),
                    "email": user.email,
                    "audiences": audiences,
                    "is_test_account": bool(user.is_test_account),
                }
            )
        return Response({"results": rows})
