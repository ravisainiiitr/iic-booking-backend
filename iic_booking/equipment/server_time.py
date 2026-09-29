"""Server clock for the booking page, so users can align with the slot-window opening time."""

from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def server_time(request):
    now = timezone.now()
    local = timezone.localtime(now)
    response = Response(
        {
            "server_time": now.isoformat(),
            "epoch_ms": int(now.timestamp() * 1000),
            "timezone": str(timezone.get_current_timezone()),
            "utc_offset_minutes": int(local.utcoffset().total_seconds() // 60),
        }
    )
    response["Cache-Control"] = "no-store"
    return response
