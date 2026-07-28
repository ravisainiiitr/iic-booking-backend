"""Permissions for Remote Analysis APIs."""

from __future__ import annotations

from rest_framework.permissions import BasePermission, SAFE_METHODS

from iic_booking.remote_analysis.authentication import RemoteAnalysisAgentUser
from iic_booking.remote_analysis.constants import (
    MANAGE_USER_TYPES,
    PERMISSION_REMOTE_ANALYSIS_MANAGE,
    PERMISSION_REMOTE_ANALYSIS_VIEW,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.rbac import user_has_permission


class IsRemoteAnalysisAgent(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and isinstance(user, RemoteAnalysisAgentUser) and user.is_authenticated)


class CanManageRemoteAnalysis(BasePermission):
    """System Admin, Department Admin, Officer In Charge (and explicit RBAC grant)."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if isinstance(user, RemoteAnalysisAgentUser):
            return False
        if getattr(user, "is_superuser", False):
            return True
        user_type = str(getattr(user, "user_type", "") or "").lower()
        if user_type == UserType.ADMIN:
            return True
        if user_type in MANAGE_USER_TYPES:
            return True
        return user_has_permission(user, PERMISSION_REMOTE_ANALYSIS_MANAGE)


class CanViewRemoteAnalysis(BasePermission):
    """Managers can view; lab operators may read; students denied by default."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if isinstance(user, RemoteAnalysisAgentUser):
            return False
        if getattr(user, "is_superuser", False):
            return True
        user_type = str(getattr(user, "user_type", "") or "").lower()
        if user_type in {*MANAGE_USER_TYPES, UserType.OPERATOR}:
            return True
        if user_has_permission(user, PERMISSION_REMOTE_ANALYSIS_VIEW):
            return True
        if user_has_permission(user, PERMISSION_REMOTE_ANALYSIS_MANAGE):
            return True
        if request.method in SAFE_METHODS and user_type == UserType.OPERATOR:
            return True
        return False
