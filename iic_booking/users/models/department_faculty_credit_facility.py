"""Department-configurable one-time credit facility for newly joined faculty (negative sub-wallet)."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class FacultyDepartmentCreditFacilityStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    EXHAUSTED = "exhausted", _("Exhausted")
    CLOSED = "closed", _("Closed")


class FacultyDepartmentCreditFacilityAuditEvent(models.TextChoices):
    CONFIG_UPDATED = "config_updated", _("Configuration updated")
    ACTIVATED = "activated", _("Facility activated")
    OUTSTANDING_CHANGED = "outstanding_changed", _("Outstanding credit changed")
    RECHARGE_RECOVERY = "recharge_recovery", _("Recharge recovery")
    STATUS_CHANGED = "status_changed", _("Status changed")
    CLOSED = "closed", _("Facility closed")


class DepartmentFacultyCreditFacilitySettings(models.Model):
    """Per-department configuration for the faculty credit facility."""

    department = models.OneToOneField(
        "users.Department",
        on_delete=models.CASCADE,
        related_name="faculty_credit_facility_settings",
        verbose_name=_("Department"),
    )
    enabled = models.BooleanField(
        _("Credit Facility Enabled"),
        default=False,
        help_text=_("When disabled, joining-date and credit-limit settings have no effect."),
    )
    joining_date_cutoff = models.DateField(
        _("Eligible Date of Joining (on or after)"),
        null=True,
        blank=True,
        help_text=_("Faculty are eligible only if their Date of Joining is on or after this date."),
    )
    max_credit_limit = models.DecimalField(
        _("Maximum Credit Limit (₹)"),
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text=_("Maximum controlled negative balance allowed on the department sub-wallet."),
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="department_faculty_credit_settings_updates",
        verbose_name=_("Updated By"),
    )
    created_at = models.DateTimeField(_("Created At"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated At"), auto_now=True)

    class Meta:
        db_table = "users_departmentfacultycreditfacilitysettings"
        verbose_name = _("Department faculty credit facility settings")
        verbose_name_plural = _("Department faculty credit facility settings")

    def __str__(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        return f"Faculty credit facility ({state}) — {self.department}"


class FacultyDepartmentCreditFacility(models.Model):
    """
    One-time faculty credit facility for a department sub-wallet.

    Implemented as a controlled negative balance (no upfront credit). Once closed,
    the row remains permanently so the benefit cannot be availed again.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="department_faculty_credit_facilities",
        verbose_name=_("Faculty"),
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        related_name="faculty_credit_facilities",
        verbose_name=_("Department"),
    )
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=FacultyDepartmentCreditFacilityStatus.choices,
        default=FacultyDepartmentCreditFacilityStatus.ACTIVE,
        db_index=True,
    )
    credit_limit = models.DecimalField(
        _("Credit Limit (₹)"),
        max_digits=10,
        decimal_places=2,
        help_text=_("Snapshot of the department max credit limit at activation."),
    )
    availed_at = models.DateTimeField(_("Date Availed"), db_index=True)
    closed_at = models.DateTimeField(_("Date Closed"), null=True, blank=True)
    created_at = models.DateTimeField(_("Created At"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated At"), auto_now=True)

    class Meta:
        db_table = "users_facultydepartmentcreditfacility"
        verbose_name = _("Faculty department credit facility")
        verbose_name_plural = _("Faculty department credit facilities")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "department"],
                name="uniq_faculty_department_credit_facility",
            ),
        ]
        indexes = [
            models.Index(fields=["department", "status"], name="fdcf_dept_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.department_id} ({self.status})"


class FacultyDepartmentCreditFacilityAuditLog(models.Model):
    """Audit trail for configuration, activation, balance recovery, and closure."""

    facility = models.ForeignKey(
        FacultyDepartmentCreditFacility,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
        verbose_name=_("Facility"),
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        related_name="faculty_credit_facility_audit_logs",
        verbose_name=_("Department"),
    )
    faculty_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="faculty_credit_facility_audit_as_faculty",
        verbose_name=_("Faculty"),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="faculty_credit_facility_audit_as_actor",
        verbose_name=_("Actor"),
    )
    event_type = models.CharField(
        _("Event Type"),
        max_length=40,
        choices=FacultyDepartmentCreditFacilityAuditEvent.choices,
        db_index=True,
    )
    message = models.TextField(_("Message"), blank=True, default="")
    metadata = models.JSONField(_("Metadata"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created At"), auto_now_add=True, db_index=True)

    class Meta:
        db_table = "users_facultydepartmentcreditfacilityauditlog"
        verbose_name = _("Faculty department credit facility audit log")
        verbose_name_plural = _("Faculty department credit facility audit logs")
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.event_type} @ {self.created_at}"
