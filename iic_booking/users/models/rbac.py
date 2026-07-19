"""Hierarchical RBAC models and helpers for internal staff roles."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


DEFAULT_PERMISSION_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    ("users.manage", "Manage users", "Create, update, activate, and map users inside a department."),
    ("equipment.manage", "Manage equipment", "Create and update equipment within a department."),
    ("equipment.request_add", "Request equipment addition", "Submit equipment addition requests for Main Admin approval."),
    ("bookings.manage", "Manage bookings", "Manage departmental booking operations and exceptions."),
    ("wallet.manage", "Manage wallet and billing", "Access financial and wallet actions for the department."),
    ("reports.view", "View reports", "View departmental reports and summaries."),
    ("oic.assign", "Assign OIC", "Assign Officer In Charge users to departmental equipment."),
    ("lab.assign", "Assign Lab In-Charge", "Assign Lab In-Charge users to departmental equipment."),
    ("finance.assign", "Assign Accounts In-Charge", "Assign Accounts In-Charge users inside the department."),
    ("permissions.manage_staff", "Manage subordinate permissions", "Grant or revoke staff permissions within department caps."),
    ("admin_settings.communication", "Admin Settings: Communication", "Access Communication settings for the department."),
    ("admin_settings.equipment", "Admin Settings: Equipment", "Access Equipment admin-settings modules for the department."),
    ("admin_settings.wallet", "Admin Settings: Wallet", "Access wallet-related Admin Settings modules for the department."),
    ("admin_settings.reports", "Admin Settings: Reports", "Access report tools from Admin Settings for the department."),
)


class PermissionDefinition(models.Model):
    """Stable permission catalog row."""

    code = models.CharField(max_length=100, unique=True, db_index=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]
        verbose_name = _("Permission definition")
        verbose_name_plural = _("Permission definitions")

    def __str__(self) -> str:
        return self.code


class DeptAdminPermissionGrant(models.Model):
    """Main Admin permission cap granted to a Department Administrator."""

    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        related_name="dept_admin_permission_grants",
    )
    dept_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dept_admin_permission_grants",
    )
    permission = models.ForeignKey(
        PermissionDefinition,
        on_delete=models.CASCADE,
        related_name="dept_admin_grants",
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_dept_admin_permission_caps",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["department__name", "dept_admin__email", "permission__code"]
        unique_together = ("department", "dept_admin", "permission")
        verbose_name = _("Department admin permission grant")
        verbose_name_plural = _("Department admin permission grants")

    def __str__(self) -> str:
        return f"{self.dept_admin_id}:{self.permission.code}"


class StaffPermissionGrant(models.Model):
    """Department Administrator permission grant to OIC/Lab/Accounts staff."""

    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        related_name="staff_permission_grants",
    )
    dept_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="staff_permission_caps_granted",
    )
    staff_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="staff_permission_grants",
    )
    permission = models.ForeignKey(
        PermissionDefinition,
        on_delete=models.CASCADE,
        related_name="staff_grants",
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_staff_permissions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["department__name", "staff_user__email", "permission__code"]
        unique_together = ("department", "staff_user", "permission")
        verbose_name = _("Staff permission grant")
        verbose_name_plural = _("Staff permission grants")

    def __str__(self) -> str:
        return f"{self.staff_user_id}:{self.permission.code}"
