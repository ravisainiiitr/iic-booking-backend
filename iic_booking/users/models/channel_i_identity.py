"""Channel-I identity facts, classification config, department mapping, HoD, student lifecycle.

These are SOURCE / mapping / local-role tables. They must not collapse into User.user_type.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class DegreeClassificationKind(models.TextChoices):
    UNDERGRADUATE = "UNDERGRADUATE", _("Undergraduate")
    POSTGRADUATE = "POSTGRADUATE", _("Postgraduate")
    RESEARCH = "RESEARCH", _("Research")
    OTHER = "OTHER", _("Other")


class PortalUserClassification(models.TextChoices):
    FACULTY = "FACULTY", _("Faculty")
    HEAD_OF_DEPARTMENT = "HEAD_OF_DEPARTMENT", _("Head of Department")
    UNDERGRADUATE_STUDENT = "UNDERGRADUATE_STUDENT", _("Undergraduate student")
    OTHER_STUDENT = "OTHER_STUDENT", _("Other student")
    STAFF = "STAFF", _("Staff")
    UNKNOWN = "UNKNOWN", _("Unknown / unresolved")


class StudentValiditySource(models.TextChoices):
    CHANNEL_I_END_DATE = "CHANNEL_I_END_DATE", _("Channel-I end date")
    START_DATE_PLUS_5_YEARS = "START_DATE_PLUS_5_YEARS", _("Start date + 5 years")
    ADMIN_EXTENSION = "ADMIN_EXTENSION", _("Admin-approved extension")
    UNRESOLVED = "UNRESOLVED", _("Unresolved")


class DepartmentMappingStatus(models.TextChoices):
    MAPPED = "MAPPED", _("Mapped")
    UNMAPPED = "UNMAPPED", _("Unmapped")
    DISABLED = "DISABLED", _("Disabled")


class AffiliationKind(models.TextChoices):
    FACULTY = "FACULTY", _("Faculty")
    HEAD_OF_DEPARTMENT = "HEAD_OF_DEPARTMENT", _("Head of Department")
    LAB = "LAB", _("Lab")
    PROJECT = "PROJECT", _("Project")
    DEPARTMENT = "DEPARTMENT", _("Department")
    OTHER = "OTHER", _("Other")


class StudentDisableReason(models.TextChoices):
    CHANNEL_I_STUDENT_END_DATE = "CHANNEL_I_STUDENT_END_DATE", _("Channel-I / derived academic end date")
    MANUAL_ADMIN_DISABLE = "MANUAL_ADMIN_DISABLE", _("Manual administrator disable")
    STUDENT_VALIDITY_UNRESOLVED = "STUDENT_VALIDITY_UNRESOLVED", _("Student validity unresolved")


class ChannelIIdentityProfile(models.Model):
    """Current Channel-I identity facts for a portal user. Source data only."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="channel_i_identity",
    )
    channel_i_user_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    channel_i_username = models.CharField(max_length=128, blank=True, default="")
    student_degree_name = models.CharField(max_length=255, blank=True, default="")
    student_department_name = models.CharField(max_length=255, blank=True, default="")
    student_start_date = models.DateField(null=True, blank=True)
    student_end_date = models.DateField(null=True, blank=True)
    faculty_department_name = models.CharField(max_length=255, blank=True, default="")
    faculty_designation = models.CharField(max_length=255, blank=True, default="")
    has_student_payload = models.BooleanField(default=False)
    has_faculty_payload = models.BooleanField(default=False)
    derived_end_date = models.DateField(null=True, blank=True)
    validity_source = models.CharField(
        max_length=32,
        choices=StudentValiditySource.choices,
        default=StudentValiditySource.UNRESOLVED,
    )
    last_channel_i_sync = models.DateTimeField(null=True, blank=True)
    profile_last_changed_at = models.DateTimeField(null=True, blank=True)
    raw_student_keys = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Channel-I identity profile")
        verbose_name_plural = _("Channel-I identity profiles")

    def __str__(self) -> str:
        return f"ChannelIIdentity({self.user_id})"


class ChannelIIdentityHistory(models.Model):
    """Immutable history of Channel-I identity fact changes."""

    profile = models.ForeignKey(
        ChannelIIdentityProfile,
        on_delete=models.CASCADE,
        related_name="history",
    )
    field_name = models.CharField(max_length=64)
    previous_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Channel-I identity history")
        ordering = ["recorded_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Channel-I identity history is immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Channel-I identity history cannot be deleted")


class StudentDegreeClassification(models.Model):
    channel_i_degree_name = models.CharField(max_length=255)
    channel_i_degree_name_normalized = models.CharField(max_length=255, unique=True)
    normalized_degree_name = models.CharField(max_length=255, blank=True, default="")
    classification = models.CharField(max_length=32, choices=DegreeClassificationKind.choices)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="degree_classifications_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="degree_classifications_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Student degree classification")
        ordering = ["channel_i_degree_name"]


class ChannelIDepartmentMapping(models.Model):
    channel_i_department_name = models.CharField(max_length=255)
    channel_i_department_name_normalized = models.CharField(max_length=255, unique=True)
    internal_department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="channel_i_mappings",
    )
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="dept_mappings_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="dept_mappings_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Channel-I department mapping")
        ordering = ["channel_i_department_name"]

    @property
    def status(self) -> str:
        if not self.active:
            return DepartmentMappingStatus.DISABLED
        if self.internal_department_id:
            return DepartmentMappingStatus.MAPPED
        return DepartmentMappingStatus.UNMAPPED


class HeadOfDepartmentAssignment(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="hod_assignments",
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.PROTECT,
        related_name="hod_assignments",
    )
    active = models.BooleanField(default=True)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="hod_assignments_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Head of Department assignment")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["department"],
                condition=Q(active=True),
                name="one_active_hod_per_department",
            ),
        ]


class UserAffiliation(models.Model):
    """Portal-owned affiliation. Historical rows are not rewritten when Channel-I department changes."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="affiliations",
    )
    kind = models.CharField(max_length=32, choices=AffiliationKind.choices)
    related_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="affiliations_received",
    )
    department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="user_affiliations",
    )
    wallet_join_request = models.ForeignKey(
        "users.WalletJoinRequest",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="affiliations",
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("User affiliation")
        ordering = ["-created_at"]


class StudentValidityExtension(models.Model):
    student = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="validity_extensions",
    )
    previous_expiry = models.DateField()
    requested_expiry = models.DateField()
    approved_expiry = models.DateField(null=True, blank=True)
    extension_months = models.PositiveSmallIntegerField(default=6)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="validity_extensions_requested",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="validity_extensions_approved",
    )
    reason = models.TextField()
    status = models.CharField(max_length=24, default="SUBMITTED")
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Student validity extension")
        ordering = ["-created_at"]


class StudentLifecycleEvent(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="student_lifecycle_events",
    )
    action = models.CharField(max_length=64)
    reason = models.CharField(max_length=64, blank=True, default="")
    previous_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="student_lifecycle_events_acted",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Student lifecycle event")
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Student lifecycle events are immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Student lifecycle events cannot be deleted")
