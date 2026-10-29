"""Who the new external workflow applies to while it is being tested on the live portal.

Until the Head IIC / Dean SRIC approvals, the new rules (central window, checkout before booking, special requests,
Wednesday 20:30 free cancellation and reschedule cut-off, invoices, government credit) apply only to test accounts
(``User.is_test_account``). Every other external user keeps the existing booking flow, and internal users keep their
wallet flow. ``EXTERNAL_WORKFLOW_ALL_EXTERNAL_USERS`` opens the external rules to every external user;
``INTERNAL_GATEWAY_FALLBACK_ALL_USERS`` offers the online payment to every internal user whose wallet falls short.
"""

from __future__ import annotations

from django.conf import settings


def _is_external(user) -> bool:
    from iic_booking.users.models.user_type import UserType

    return bool(user is not None and UserType.is_external_user(getattr(user, "user_type", None) or ""))


def _is_test_account(user) -> bool:
    return bool(getattr(user, "is_test_account", False))


def external_workflow_user(user) -> bool:
    """An external user to whom the new external workflow applies."""
    if not _is_external(user):
        return False
    return bool(getattr(settings, "EXTERNAL_WORKFLOW_ALL_EXTERNAL_USERS", False)) or _is_test_account(user)


def internal_gateway_fallback_user(user) -> bool:
    """An internal user who may pay a booking online when the wallet falls short."""
    if user is None or _is_external(user):
        return False
    return bool(getattr(settings, "INTERNAL_GATEWAY_FALLBACK_ALL_USERS", False)) or _is_test_account(user)
