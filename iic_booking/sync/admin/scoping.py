"""
Future RBAC hooks for the Department Sync Operations Console.

Milestone 3 prepares the structure only — no RBAC is enforced yet.
Intended roles:
- Main Administrator → full access
- Department Administrator → own department only
- Lab In-Charge → own laboratory only
"""

from __future__ import annotations

from django.db.models import QuerySet


class SyncAdminScope:
    """Placeholder scope resolved from the request user (future RBAC)."""

    def __init__(
        self,
        *,
        is_full_access: bool = True,
        department_id: int | None = None,
        laboratory_id=None,
    ):
        self.is_full_access = is_full_access
        self.department_id = department_id
        self.laboratory_id = laboratory_id


def resolve_sync_admin_scope(request) -> SyncAdminScope:
    """
    Resolve admin scope for sync ModelAdmins.

    Currently always full access. Wire to users.rbac later without changing
    call sites in ModelAdmin.get_queryset.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return SyncAdminScope(is_full_access=False)

    # Future:
    # - Main Admin / superuser → full access
    # - Dept Admin → department_id = user.department_id
    # - Lab In-Charge → laboratory_id from assignment
    return SyncAdminScope(is_full_access=True)


def scope_agents(queryset: QuerySet, scope: SyncAdminScope) -> QuerySet:
    if scope.is_full_access:
        return queryset
    qs = queryset
    if scope.department_id is not None:
        qs = qs.filter(department_id=scope.department_id)
    if scope.laboratory_id is not None:
        qs = qs.filter(laboratory_id=scope.laboratory_id)
    return qs


def scope_profiles(queryset: QuerySet, scope: SyncAdminScope) -> QuerySet:
    if scope.is_full_access:
        return queryset
    qs = queryset
    if scope.department_id is not None:
        qs = qs.filter(equipment__internal_department_id=scope.department_id)
    if scope.laboratory_id is not None:
        qs = qs.filter(
            assignments__is_active=True,
            assignments__sync_agent__laboratory_id=scope.laboratory_id,
        ).distinct()
    return qs


def scope_by_agent_department(queryset: QuerySet, scope: SyncAdminScope, *, agent_lookup: str = "sync_agent") -> QuerySet:
    if scope.is_full_access:
        return queryset
    qs = queryset
    if scope.department_id is not None:
        qs = qs.filter(**{f"{agent_lookup}__department_id": scope.department_id})
    if scope.laboratory_id is not None:
        qs = qs.filter(**{f"{agent_lookup}__laboratory_id": scope.laboratory_id})
    return qs
