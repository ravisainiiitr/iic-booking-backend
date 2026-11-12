"""GET /api/admin/integration-health/ (latest result) and POST /api/admin/integration-health/run/ (check and fix now).
Main Administrator only."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType


def _is_main_admin(user) -> bool:
    return getattr(user, "user_type", None) == UserType.ADMIN


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def integration_health(request):
    if not _is_main_admin(request.user):
        return Response({"error": "Only the Main Administrator can view integration health."}, status=status.HTTP_403_FORBIDDEN)
    from .report import latest, run

    payload = latest()
    if payload is None:  # first visit after a restart: database checks only (fast), no outside calls, no fixes
        payload = run(heal=False, live=False, alert=False)
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def integration_health_run(request):
    if not _is_main_admin(request.user):
        return Response({"error": "Only the Main Administrator can run the checks."}, status=status.HTTP_403_FORBIDDEN)
    from .report import run

    return Response(run(heal=True, live=True, alert=True))
