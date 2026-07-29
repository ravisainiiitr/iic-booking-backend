"""Permissions for Department Sync control-plane APIs."""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from iic_booking.sync.authentication import SyncAgentUser
from iic_booking.users.models.user_type import UserType


class IsDepartmentSyncAgent(BasePermission):
    """Allow only requests authenticated as a Department Sync Agent."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and isinstance(user, SyncAgentUser) and user.is_authenticated)


class CanManageDepartmentSync(BasePermission):
    """
    Main Administrator (user_type=admin), Django staff, or superuser.

    Used by portal-facing /api/v1/sync/admin|enterprise|monitoring|operations APIs.
    """

    def has_permission(self, request, view):
        user = request.user
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if isinstance(user, SyncAgentUser):
            return False
        if getattr(user, "is_superuser", False):
            return True
        # Preserve Django Admin / staff session access to the same JSON APIs.
        if getattr(user, "is_staff", False):
            return True
        user_type = str(getattr(user, "user_type", "") or "").lower()
        return user_type == UserType.ADMIN
