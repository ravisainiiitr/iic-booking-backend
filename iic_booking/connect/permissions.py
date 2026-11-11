"""DRF permission classes for IITR Connect.

``ConnectEnabled`` answers 404 (not 403) when the module is off or not enabled for the user, so the API does
not reveal that it exists. Object access is never decided here: views load objects only through the visible
querysets in ``access`` and therefore 404 for anything the user may not read.
"""

from __future__ import annotations

from django.http import Http404
from rest_framework.permissions import BasePermission

from . import access


class ConnectEnabled(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False  # 401 from the authentication layer
        if not access.user_enabled(user):
            raise Http404
        return True


class ConnectAdmin(BasePermission):
    """Portal Admin or Community Admin."""

    def has_permission(self, request, view) -> bool:
        if not access.snapshot(request.user).is_admin:
            raise Http404
        return True


class PortalAdmin(BasePermission):
    def has_permission(self, request, view) -> bool:
        if not access.snapshot(request.user).portal_admin:
            raise Http404
        return True
