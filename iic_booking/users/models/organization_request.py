from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.utils.translation import gettext_lazy as _

from .department import ExternalDepartmentSubcategory, IndianState, Department, DepartmentType


class OrganizationRequest(models.Model):
    """Request to add a new external organization (e.g. Govt R&D)."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")

    name = models.CharField(_("Requested organization name"), max_length=255)
    state = models.CharField(
        _("State / Union Territory"),
        max_length=50,
        choices=IndianState.get_choices(),
    )
    external_subcategory = models.CharField(
        _("External subcategory"),
        max_length=50,
        choices=ExternalDepartmentSubcategory.get_choices(),
        default=ExternalDepartmentSubcategory.GOVT_RND,
        help_text=_("External subcategory (e.g. Educational Institute, Govt R&D Organizations, Industries)."),
    )
    email = models.EmailField(
        _("Requester email"),
        blank=True,
        null=True,
        help_text=_("Email of the user requesting this organization."),
    )
    requester_name = models.CharField(
        _("Requester name"),
        max_length=255,
        blank=True,
        default="",
        help_text=_("Name of the user requesting this organization (optional)."),
    )
    web_page = models.URLField(
        _("Organization web page"),
        blank=True,
        default="",
        help_text=_("Web page URL entered by the requester (optional)."),
    )
    notes = models.TextField(_("Additional details"), blank=True)

    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    approved_name = models.CharField(
        _("Approved organization name"),
        max_length=255,
        blank=True,
        help_text=_("Admin-edited final name used to create the Department."),
    )
    created_department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="organization_requests",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="approved_organization_requests",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Organization request")
        verbose_name_plural = _("Organization requests")
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.status == self.Status.APPROVED and self.created_department_id:
            User = get_user_model()
            User.objects.filter(organization_request=self).update(
                department=self.created_department,
                organization_request=None,
            )

    def __str__(self) -> str:  # pragma: no cover - simple representation
        return self.approved_name or self.name

