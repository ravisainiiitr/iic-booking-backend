"""RBAC helpers for remote desktop sessions."""

from __future__ import annotations

from iic_booking.remote_analysis.constants import MANAGE_USER_TYPES
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis
from iic_booking.remote_analysis.session_models import RemoteDesktopSession
from iic_booking.users.models.user_type import UserType
from iic_booking.users.rbac import user_has_permission
from iic_booking.remote_analysis.constants import PERMISSION_REMOTE_ANALYSIS_MANAGE


def is_session_owner(user, session: RemoteDesktopSession) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    return session.user_id == getattr(user, "pk", None)


def can_launch_session(user, session: RemoteDesktopSession) -> bool:
    """Only the reservation owner may launch (managers may assist via override terminate/observe)."""
    return is_session_owner(user, session)


def can_terminate_session(user, session: RemoteDesktopSession) -> bool:
    if is_session_owner(user, session):
        return True
    return CanManageRemoteAnalysis().has_permission(
        type("R", (), {"user": user, "method": "POST"})(), None
    )


def can_observe_session(user, session: RemoteDesktopSession) -> bool:
    if is_session_owner(user, session):
        return True
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    user_type = str(getattr(user, "user_type", "") or "").lower()
    if user_type == UserType.ADMIN or user_type in MANAGE_USER_TYPES:
        return True
    return user_has_permission(user, PERMISSION_REMOTE_ANALYSIS_MANAGE)


def can_create_for_reservation(user, reservation) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if reservation.user_id == getattr(user, "pk", None):
        return True
    # Managers may create on behalf of reservation owner (lab assist)
    return CanManageRemoteAnalysis().has_permission(
        type("R", (), {"user": user, "method": "POST"})(), None
    )
