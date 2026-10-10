"""Facility user groups: who booked which equipment / category / lab, plus Main Administrator custom lists.

Distinct from ``users.UserGroup`` (equipment visibility). Automatic groups are keyed by ``auto_key`` and their
membership is recomputed from bookings whenever a booking is created or changes status (see ``membership``).
Group emails are stored as a campaign with one recipient row per address so a retry never sends twice.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class GroupKind(models.TextChoices):
    ALL = "all", _("All booking users")
    LAB = "lab", _("Laboratory / facility")
    CATEGORY = "category", _("Equipment category")
    EQUIPMENT_GROUP = "equipment_group", _("Equipment group")
    EQUIPMENT = "equipment", _("Equipment")
    CUSTOM = "custom", _("Custom")


AUTOMATIC_KINDS = (
    GroupKind.ALL,
    GroupKind.LAB,
    GroupKind.CATEGORY,
    GroupKind.EQUIPMENT_GROUP,
    GroupKind.EQUIPMENT,
)


class FacilityUserGroup(models.Model):
    name = models.CharField(_("Name"), max_length=255)
    kind = models.CharField(_("Kind"), max_length=20, choices=GroupKind.choices, db_index=True)
    auto_key = models.CharField(
        _("Automatic key"),
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text=_("e.g. equipment:12 or category:3 for automatic groups; empty for custom groups."),
    )
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    category = models.ForeignKey(
        "equipment.EquipmentCategory", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    equipment_group = models.ForeignKey(
        "equipment.EquipmentGroup", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    lab = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=_("Internal department hosting the equipment (e.g. a centre or the Tinkering Lab)."),
    )
    description = models.TextField(_("Description"), blank=True, default="")
    is_archived = models.BooleanField(_("Archived"), default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Facility user group")
        verbose_name_plural = _("Facility user groups")
        ordering = ["kind", "name"]

    def __str__(self) -> str:
        return self.name

    @property
    def is_automatic(self) -> bool:
        return self.kind != GroupKind.CUSTOM


class FacilityUserGroupMember(models.Model):
    group = models.ForeignKey(FacilityUserGroup, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="facility_group_memberships"
    )
    booking_count = models.PositiveIntegerField(default=0)
    first_booked_at = models.DateTimeField(null=True, blank=True)
    last_booked_at = models.DateTimeField(null=True, blank=True)
    supervised_booking_count = models.PositiveIntegerField(
        default=0, help_text=_("Bookings in this group made by students / project staff linked to this faculty.")
    )
    last_supervised_at = models.DateTimeField(null=True, blank=True)
    added_manually = models.BooleanField(default=False)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Facility user group member")
        verbose_name_plural = _("Facility user group members")
        constraints = [
            models.UniqueConstraint(fields=["group", "user"], name="facility_group_member_unique"),
        ]
        indexes = [models.Index(fields=["group", "last_booked_at"], name="facility_grp_member_last")]

    def __str__(self) -> str:
        return f"{self.group_id}:{self.user_id}"


class CcMode(models.TextChoices):
    SUMMARY = "summary", _("One summary copy to CC / BCC")
    EACH = "each", _("CC / BCC on every recipient's email")


class CampaignStatus(models.TextChoices):
    QUEUED = "queued", _("Queued")
    SENDING = "sending", _("Sending")
    SENT = "sent", _("Sent")
    PARTIAL = "partial", _("Sent with failures")
    FAILED = "failed", _("Failed")
    CANCELLED = "cancelled", _("Cancelled")


class GroupEmailCampaign(models.Model):
    subject = models.CharField(max_length=255)
    body_html = models.TextField(blank=True, default="")
    body_text = models.TextField(blank=True, default="")
    groups = models.ManyToManyField(FacilityUserGroup, blank=True, related_name="email_campaigns")
    group_names = models.JSONField(default=list, blank=True)
    filters = models.JSONField(default=dict, blank=True)
    cc = models.JSONField(default=list, blank=True)
    bcc = models.JSONField(default=list, blank=True)
    cc_mode = models.CharField(max_length=10, choices=CcMode.choices, default=CcMode.SUMMARY)
    reply_to = models.CharField(max_length=254, blank=True, default="")
    status = models.CharField(max_length=20, choices=CampaignStatus.choices, default=CampaignStatus.QUEUED, db_index=True)
    total_recipients = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    summary_sent_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=64, unique=True, null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Group email")
        verbose_name_plural = _("Group emails")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.subject


def campaign_attachment_upload_to(instance, filename):
    from django.utils.text import get_valid_filename

    safe = get_valid_filename(filename)[:120] or "attachment"
    return f"group_email_attachments/{instance.campaign_id or 'new'}/{safe}"


class GroupEmailAttachment(models.Model):
    campaign = models.ForeignKey(GroupEmailCampaign, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=campaign_attachment_upload_to, max_length=300)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True, default="")
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.filename


class RecipientStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SENDING = "sending", _("Sending")
    SENT = "sent", _("Sent")
    FAILED = "failed", _("Failed")
    SKIPPED = "skipped", _("Skipped")


class GroupEmailRecipient(models.Model):
    campaign = models.ForeignKey(GroupEmailCampaign, on_delete=models.CASCADE, related_name="recipients")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    email = models.CharField(max_length=254)
    name = models.CharField(max_length=255, blank=True, default="")
    department_name = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=10, choices=RecipientStatus.choices, default=RecipientStatus.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Group email recipient")
        verbose_name_plural = _("Group email recipients")
        constraints = [
            models.UniqueConstraint(fields=["campaign", "email"], name="group_email_recipient_unique"),
        ]

    def __str__(self) -> str:
        return self.email
