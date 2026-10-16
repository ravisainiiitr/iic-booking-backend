"""Dashboard banner: equipment Under Maintenance handled by ``maintenance_auto`` (staff of that equipment only)."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def maintenance_status_view(request):
    from .maintenance_auto import active_summaries_for_user
    from .results_deadline import viewer_is_staff

    if not viewer_is_staff(request.user):
        return Response({"periods": []}, status=status.HTTP_200_OK)
    return Response({"periods": active_summaries_for_user(request.user)}, status=status.HTTP_200_OK)
