"""Permissions for Department Sync control-plane APIs."""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from iic_booking.sync.authentication import SyncAgentUser


class IsDepartmentSyncAgent(BasePermission):
    """Allow only requests authenticated as a Department Sync Agent."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and isinstance(user, SyncAgentUser) and user.is_authenticated)
