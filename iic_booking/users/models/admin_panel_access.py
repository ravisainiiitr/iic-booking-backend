"""Admin Panel access configuration by User Type + Department."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from .user_type import UserType


class AdminPanelRoleConfig(models.Model):
    """
    Main Administrator configures whether a User Type in a given Department
    may access the Admin Panel, and which Admin Settings modules they may use.

    Default when no row exists: Admin Panel disabled.
    """

    user_type = models.CharField(
        max_length=50,
        choices=UserType.get_choices(),
        help_text=_("User type this configuration applies to"),
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        related_name="admin_panel_role_configs",
        help_text=_("Department scope for this user-type configuration"),
    )
    admin_panel_enabled = models.BooleanField(
        default=False,
        help_text=_("When false (default), Admin Panel is hidden and APIs reject access."),
    )
    # Selected module keys from admin_settings_registry (parents and/or leaves).
    module_keys = models.JSONField(
        default=list,
        blank=True,
        help_text=_("List of Admin Settings module keys granted when panel is enabled."),
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_admin_panel_role_configs",
    )

    class Meta:
        verbose_name = _("Admin Panel Role Config")
        verbose_name_plural = _("Admin Panel Role Configs")
        ordering = ["department__name", "user_type"]
        unique_together = ("user_type", "department")

    def __str__(self) -> str:
        enabled = "on" if self.admin_panel_enabled else "off"
        return f"{self.user_type}@{self.department_id}:{enabled}"
