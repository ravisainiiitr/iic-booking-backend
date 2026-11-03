from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class WhatsAppOptIn(models.Model):
    """A user's consent to WhatsApp messages (Meta requires opt-in before business-initiated messages)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="whatsapp_opt_in")
    phone_e164 = models.CharField(max_length=20, help_text=_("WhatsApp number in E.164 form, e.g. +919876543210"))
    opted_in = models.BooleanField(default=False)
    opted_in_at = models.DateTimeField(null=True, blank=True)
    opted_out_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=20, blank=True, default="", help_text=_("profile, reply_stop, admin"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("WhatsApp opt-in")
        verbose_name_plural = _("WhatsApp opt-ins")

    def __str__(self):
        return f"{self.user_id} {'in' if self.opted_in else 'out'}"


class WhatsAppMessage(models.Model):
    """One template message to one user, with Meta's delivery status (from the webhook)."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        SENT = "sent", _("Sent")
        DELIVERED = "delivered", _("Delivered")
        READ = "read", _("Read")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="whatsapp_messages")
    to_e164 = models.CharField(max_length=20)
    event = models.CharField(max_length=40)
    template = models.CharField(max_length=80)
    params = models.JSONField(default=list, blank=True)
    event_key = models.CharField(max_length=120, unique=True, help_text=_("One message per portal event"))
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    wamid = models.CharField(max_length=128, blank=True, default="", db_index=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"], name="whatsapp_status_idx")]

    def __str__(self):
        return f"{self.event} → {self.to_e164[:6]}… ({self.status})"
