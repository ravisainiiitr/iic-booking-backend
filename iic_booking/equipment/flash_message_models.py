"""Equipment flash messages (models). Imported at the end of ``iic_booking.equipment.models``.

A short, time-boxed message staff show at the top of an equipment page and its booking page, e.g.
"Sample submission closes at 4 PM today". Expired messages simply stop showing (filtered by time).
"""

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class FlashTone(models.TextChoices):
    INFO = "INFO", _("Info")
    NOTICE = "NOTICE", _("Notice")
    IMPORTANT = "IMPORTANT", _("Important")
    SUCCESS = "SUCCESS", _("Success")


class FlashAudience(models.TextChoices):
    ALL = "ALL", _("Everyone (including signed-out visitors)")
    INTERNAL = "INTERNAL", _("Internal (IITR) users")
    EXTERNAL = "EXTERNAL", _("External users")
    USER_TYPES = "USER_TYPES", _("Selected user types")


class EquipmentFlashMessage(models.Model):
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.CASCADE,
        related_name="flash_messages",
    )
    message = models.TextField(help_text=_("Short text; bold, italic and links only (sanitised)."))
    tone = models.CharField(max_length=16, choices=FlashTone.choices, default=FlashTone.INFO)
    start_at = models.DateTimeField(default=timezone.now)
    end_at = models.DateTimeField(db_index=True)
    is_active = models.BooleanField(default=True, help_text=_("Off hides the message without deleting it."))
    audience = models.CharField(max_length=16, choices=FlashAudience.choices, default=FlashAudience.ALL)
    audience_user_types = models.JSONField(default=list, blank=True)
    show_on_modes = models.BooleanField(
        default=False, help_text=_("Multi-mode base instrument: also show on the pages of all its modes.")
    )
    link_url = models.URLField(max_length=500, blank=True, default="")
    link_label = models.CharField(max_length=60, blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_at", "-id"]
        verbose_name = _("Equipment flash message")
        verbose_name_plural = _("Equipment flash messages")
        indexes = [models.Index(fields=["equipment", "is_active", "end_at"], name="equip_flash_eq_active_end")]

    def __str__(self):
        return f"Flash message {self.pk} equipment {self.equipment_id}"

    def save(self, *args, **kwargs):
        from .flash_message_service import invalidate_flash_cache

        super().save(*args, **kwargs)
        invalidate_flash_cache(self.equipment_id)

    def delete(self, *args, **kwargs):
        from .flash_message_service import invalidate_flash_cache

        equipment_id = self.equipment_id
        result = super().delete(*args, **kwargs)
        invalidate_flash_cache(equipment_id)
        return result


class EquipmentFlashMessageAudit(models.Model):
    """Who created, changed, ended or extended a flash message, in which role, and what changed."""

    flash_message = models.ForeignKey(EquipmentFlashMessage, on_delete=models.CASCADE, related_name="audit_entries")
    equipment_id_snapshot = models.PositiveIntegerField(null=True, blank=True)
    action = models.CharField(max_length=24)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    actor_role = models.CharField(max_length=32, blank=True, default="")
    changes = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["at", "id"]
