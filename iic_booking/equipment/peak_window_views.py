"""Public peak-window status and the main-admin settings API."""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from config.admin_extra_settings_api import IsMainAdmin

from .models import PeakWindowSetting
from .peak_window import (
    compute_peak_state,
    get_or_create_peak_setting_row,
    invalidate_peak_window_cache,
    public_peak_state,
)

STATUS_CACHE_SECONDS = 5


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def peak_window_status(request):
    """Anonymous, briefly cacheable: is any equipment inside its slot-opening peak window?"""
    response = Response(public_peak_state())
    response["Cache-Control"] = f"public, max-age={STATUS_CACHE_SECONDS}"
    return response


class PeakWindowSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = PeakWindowSetting
        fields = [
            "enabled",
            "lead_minutes",
            "trail_minutes",
            "block_external_users",
            "external_notice_minutes",
            "defer_background_tasks",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]


class PeakWindowSettingView(APIView):
    """GET / PATCH the singleton peak-window setting (main admin only)."""

    permission_classes = [IsMainAdmin]

    def _payload(self, obj):
        data = PeakWindowSettingSerializer(obj).data
        state = compute_peak_state()
        data["status"] = {k: v for k, v in state.items() if not k.startswith("_")}
        return data

    def get(self, request):
        return Response(self._payload(get_or_create_peak_setting_row()))

    def patch(self, request):
        obj = get_or_create_peak_setting_row()
        serializer = PeakWindowSettingSerializer(obj, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        invalidate_peak_window_cache()
        return Response(self._payload(obj))

    put = patch
