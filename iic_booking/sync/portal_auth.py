"""Portal authentication helpers for Department Sync admin APIs."""

from __future__ import annotations

from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated

from iic_booking.sync.permissions import CanManageDepartmentSync
from iic_booking.users.api.token_auth import TokenAuthenticationWithInactivity

# Accept portal Token auth (Main Admin dashboard) and Django session (staff).
PORTAL_ADMIN_AUTH = [TokenAuthenticationWithInactivity, SessionAuthentication]
PORTAL_ADMIN_PERM = [IsAuthenticated, CanManageDepartmentSync]
