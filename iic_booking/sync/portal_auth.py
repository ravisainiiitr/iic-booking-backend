"""Portal authentication helpers for Department Sync admin APIs."""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated

from iic_booking.sync.permissions import CanManageDepartmentSync
from iic_booking.users.api.token_auth import TokenAuthenticationWithInactivity

# Token only — SessionAuthentication would enforce CSRF on leftover desktop-handoff cookies.
PORTAL_ADMIN_AUTH = [TokenAuthenticationWithInactivity]
PORTAL_ADMIN_PERM = [IsAuthenticated, CanManageDepartmentSync]
