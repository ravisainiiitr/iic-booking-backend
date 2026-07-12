from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from iic_booking.users.models import ExternalBillingProfile, UserType
from iic_booking.users.serializers.billing_serializer import ExternalBillingProfileSerializer


def _require_external_user(request):
    user_type = getattr(request.user, "user_type", None)
    # Note: Industry user type is stored as UserType.INSTITUTE (value: "Industry")
    if UserType.is_external_user(user_type or ""):
        return None
    return Response({"error": "Only external users can access billing profile."}, status=status.HTTP_403_FORBIDDEN)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def external_billing_profile_me(request):
    """Get or update current external user's billing/shipping details."""
    err = _require_external_user(request)
    if err is not None:
        return err

    profile, _created = ExternalBillingProfile.objects.get_or_create(user=request.user)

    if request.method == "GET":
        return Response(ExternalBillingProfileSerializer(profile).data, status=status.HTTP_200_OK)

    serializer = ExternalBillingProfileSerializer(profile, data=request.data, partial=True)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    serializer.save()
    return Response(serializer.data, status=status.HTTP_200_OK)

