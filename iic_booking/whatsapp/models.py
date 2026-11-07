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


class WhatsAppConsentEvent(models.Model):
    """Every change of consent, for the record: who, which number, when and how. Not deleted by the retention job."""

    class Action(models.TextChoices):
        OPT_IN = "opt_in", _("Opted in")
        OPT_OUT = "opt_out", _("Opted out")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="whatsapp_consent_events")
    phone_e164 = models.CharField(max_length=20)
    action = models.CharField(max_length=10, choices=Action.choices)
    source = models.CharField(max_length=20, help_text=_("profile, reply_stop, reply_start, admin"))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.action} {self.phone_e164[:6]}… ({self.source})"


class WhatsAppWebhookEvent(models.Model):
    """One Meta event already handled, so a repeat is ignored. Holds no message content or phone number."""

    event_key = models.CharField(max_length=64, unique=True, help_text=_("SHA-256 of the event's kind, id and status"))
    kind = models.CharField(max_length=10)
    detail = models.CharField(max_length=20, blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.kind} {self.detail}"


class WhatsAppInbound(models.Model):
    """A message someone with an opt-in record sent to the portal's WhatsApp number. Kept once per Meta message id."""

    wamid = models.CharField(max_length=128, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="whatsapp_inbound")
    from_e164 = models.CharField(max_length=20)
    kind = models.CharField(max_length=20, blank=True, default="", help_text=_("text, button, interactive, image, ..."))
    text = models.TextField(blank=True, default="")
    action = models.CharField(max_length=10, blank=True, default="", help_text=_("opt_out or opt_in when it changed the consent"))
    meta_timestamp = models.DateTimeField(null=True, blank=True,
                                          help_text=_("When Meta says the message was sent. Empty if missing or malformed, and on older rows."))
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)  # when this server stored it

    class Meta:
        ordering = ["-received_at"]
        verbose_name = _("WhatsApp incoming message")
        verbose_name_plural = _("WhatsApp incoming messages")

    def __str__(self):
        return f"{self.kind} from {self.from_e164[:6]}…"


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


class WhatsAppTemplate(models.Model):
    """What Meta last reported about one template. Only an APPROVED row for the configured WABA allows sending."""

    class Status(models.TextChoices):
        NOT_SUBMITTED = "NOT_SUBMITTED", _("Not submitted")
        PENDING = "PENDING", _("Pending")
        IN_APPEAL = "IN_APPEAL", _("In appeal")
        APPROVED = "APPROVED", _("Approved")
        REJECTED = "REJECTED", _("Rejected")
        PAUSED = "PAUSED", _("Paused")
        DISABLED = "DISABLED", _("Disabled")
        PENDING_DELETION = "PENDING_DELETION", _("Pending deletion")
        DELETED = "DELETED", _("Deleted")
        UNKNOWN = "UNKNOWN", _("Unknown")

    waba_id = models.CharField(max_length=32)
    name = models.CharField(max_length=512)
    language = models.CharField(max_length=20)
    event = models.CharField(max_length=40, blank=True, default="", help_text=_("Portal event, when it is in the catalogue"))
    category = models.CharField(max_length=30, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NOT_SUBMITTED, db_index=True)
    rejected_reason = models.CharField(max_length=200, blank=True, default="")
    meta_id = models.CharField(max_length=64, blank=True, default="")
    wording_hash = models.CharField(max_length=64, blank=True, default="", help_text=_("Catalogue wording last sent to Meta"))
    submitted_at = models.DateTimeField(null=True, blank=True)
    submission_attempts = models.PositiveSmallIntegerField(default=0)
    correction_attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["waba_id", "name", "language"], name="whatsapp_template_unique")]
        ordering = ["name", "language"]

    def __str__(self):
        return f"{self.name} ({self.language}): {self.status}"


class WhatsAppTemplateAudit(models.Model):
    """Every template action, successful or not. Holds no token, ID or phone number. Nothing in the code deletes these."""

    action = models.CharField(max_length=30, db_index=True)
    template_name = models.CharField(max_length=512, blank=True, default="")
    language = models.CharField(max_length=20, blank=True, default="")
    actor = models.CharField(max_length=100, blank=True, default="")
    ok = models.BooleanField(default=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    meta_code = models.CharField(max_length=20, blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.action} {self.template_name}"
