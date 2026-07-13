"""Views for User model."""

import logging

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.mixins import ListModelMixin
from rest_framework.mixins import RetrieveModelMixin
from rest_framework.mixins import UpdateModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from ..models import User
from ..models.user_type import UserType
from ..serializers import UserSerializer

logger = logging.getLogger(__name__)

# Map frontend role key (UserManagement) to backend user_type for PATCH updates.
ROLE_TO_USER_TYPE = {
    "admin": UserType.ADMIN,
    "officer_in_charge": UserType.MANAGER,
    "operator": UserType.OPERATOR,
    "accounts": UserType.FINANCE,
    "iitr_student": UserType.STUDENT,
    "iitr_faculty": UserType.FACULTY,
    "external_academic": UserType.EXTERNAL,
    "external_rnd": UserType.RND,
    "industrial_user": UserType.OTHER,
}


def _require_admin_panel(request):
    """Return True if request user is admin-panel (admin, manager, operator, finance)."""
    return request.user.user_type in UserType.get_admin_panel_codes()


def _require_admin_user(request):
    """Return True if request user is Admin user_type (not OIC/operator/finance)."""
    return getattr(request.user, "user_type", None) == UserType.ADMIN


class UserViewSet(RetrieveModelMixin, ListModelMixin, UpdateModelMixin, GenericViewSet):
    """ViewSet for User model."""

    serializer_class = UserSerializer
    queryset = User.objects.all()
    lookup_field = "pk"

    def get_queryset(self, *args, **kwargs):
        """Non-admin users see only themselves; admin-panel users see all users."""
        if self.request.user.user_type in UserType.get_admin_panel_codes():
            return self.queryset.select_related("department").order_by("id")
        return self.queryset.filter(id=self.request.user.id)

    @action(detail=False)
    def me(self, request):
        """Get current authenticated user details."""
        serializer = UserSerializer(request.user, context={"request": request})
        return Response(status=status.HTTP_200_OK, data=serializer.data)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve_user(self, request, pk=None):
        """Approve a user (set admin_approved=True). Admin only. Sends approval email."""
        if not _require_admin_user(request):
            return Response(
                {"error": "Only Admin users can approve users."},
                status=status.HTTP_403_FORBIDDEN,
            )
        user = self.get_object()
        if user.admin_approved:
            return Response(
                {"message": "User is already approved.", "user": UserSerializer(user).data},
                status=status.HTTP_200_OK,
            )
        # Once admin approves, user is deemed active; mark email verified too.
        user.email_verified = True
        user.admin_approved = True
        user.save(update_fields=["email_verified", "admin_approved"])
        try:
            from django.conf import settings
            from iic_booking.communication.service import CommunicationService
            web_address = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/") or "/"
            CommunicationService.send_email(
                recipient=user,
                template="registration_approval_confirmation_email",
                template_context={
                    "name": user.name or user.email,
                    "web_address": web_address,
                },
            )
            logger.info("Approval confirmation email sent to %s", user.email)
        except Exception as e:
            logger.exception("Failed to send approval email to %s: %s", user.email, e)
        serializer = UserSerializer(user)
        return Response({"message": "User approved successfully.", "user": serializer.data}, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject_user(self, request, pk=None):
        """Reject a user (set admin_approved=False). Admin only."""
        if not _require_admin_user(request):
            return Response(
                {"error": "Only Admin users can reject users."},
                status=status.HTTP_403_FORBIDDEN,
            )
        user = self.get_object()
        if not user.admin_approved:
            return Response(
                {"message": "User is not approved.", "user": UserSerializer(user).data},
                status=status.HTTP_200_OK,
            )
        user.admin_approved = False
        user.save(update_fields=["admin_approved"])
        serializer = UserSerializer(user)
        return Response({"message": "User rejected successfully.", "user": serializer.data}, status=status.HTTP_200_OK)

    def perform_update(self, serializer):
        """Save serializer data; only Admin may change other users' user_type/name via this path."""
        serializer.save()
        if _require_admin_user(self.request):
            instance = serializer.instance
            update_fields = []
            if "user_type" in self.request.data:
                raw = self.request.data.get("user_type")
                instance.user_type = ROLE_TO_USER_TYPE.get(raw, raw)
                update_fields.append("user_type")
            if "name" in self.request.data:
                instance.name = self.request.data.get("name")
                update_fields.append("name")
            if update_fields:
                instance.save(update_fields=update_fields)

