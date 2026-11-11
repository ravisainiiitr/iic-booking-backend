"""Analytics (moderators, scoped) and module settings (Portal Admin)."""

from __future__ import annotations

from django.http import Http404
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from .. import access
from ..models import ConnectSettings
from ..models import ModerationAction
from ..permissions import PortalAdmin
from ..services import analytics
from ..services import recognition
from .base import ConnectAPIView
from .threads import _int

TAG = ["IITR Connect: analytics and settings"]
SETTINGS_FIELDS = {
    "recognition_enabled": bool,
    "edit_window_minutes": int,
    "delete_window_minutes": int,
    "max_attachment_mb": int,
    "max_attachments_per_post": int,
    "min_account_age_hours_to_post": int,
}
LIMITS = {"max_attachment_mb": (1, 50), "max_attachments_per_post": (1, 20), "edit_window_minutes": (0, 525600),
          "delete_window_minutes": (0, 10080), "min_account_age_hours_to_post": (0, 720)}


class AnalyticsView(ConnectAPIView):
    @extend_schema(tags=TAG, summary="Analytics for the communities you moderate (admins: all). Not a downtime measure.")
    def get(self, request):
        if not access.snapshot(request.user).moderated:
            raise Http404
        days = min(max(_int(request.query_params.get("days")) or 90, 7), 365)
        return Response(analytics.build(request.user, _int(request.query_params.get("community")), days=days))


def _settings_payload(row: ConnectSettings) -> dict:
    return {key: getattr(row, key) for key in SETTINGS_FIELDS}


class SettingsView(ConnectAPIView):
    permission_classes = [*ConnectAPIView.permission_classes, PortalAdmin]
    throttle_scope = "connect_write"

    @extend_schema(tags=TAG, summary="IITR Connect module settings (Portal Admin)")
    def get(self, request):
        return Response(_settings_payload(ConnectSettings.load()))

    @extend_schema(tags=TAG, summary="Change IITR Connect module settings (Portal Admin; audited)")
    def patch(self, request):
        row = ConnectSettings.load()
        before = _settings_payload(row)
        for key, kind in SETTINGS_FIELDS.items():
            if key not in request.data:
                continue
            value = request.data.get(key)
            if kind is bool:
                setattr(row, key, value is True)
                continue
            try:
                value = int(value)
            except (TypeError, ValueError):
                return Response({"detail": "Use a whole number.", "field": key}, status=400)
            low, high = LIMITS[key]
            if not low <= value <= high:
                return Response({"detail": f"Use a value from {low} to {high}.", "field": key}, status=400)
            setattr(row, key, value)
        row.updated_by = request.user
        row.save()
        after = _settings_payload(row)
        if after != before:
            ModerationAction.objects.create(actor=request.user, action="settings_update", target_type="settings", target_id="1", before=before, after=after)
        if after["recognition_enabled"] and not before["recognition_enabled"]:
            recognition.recompute()
        return Response(after)
