"""Communication models for managing templates, logs, and webhook events."""

from django.db import models
from django.db.models import (
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKey,
    JSONField,
    PROTECT,
    SET_NULL,
    TextField,
)
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from iic_booking.users.models import User
from .utils import get_current_user


class CommunicationTemplate(models.Model):
    """
    Unified template model for managing email, push notification, and SMS templates.
    
    Templates support variable substitution using {{ variable_name }} syntax.
    """

    class CommunicationType(models.TextChoices):
        EMAIL = "email", _("Email")
        PUSH_NOTIFICATION = "push_notification", _("Push Notification")
        SMS = "sms", _("SMS")

    name = CharField(
        _("Template Name"),
        max_length=255,
        unique=True,
        help_text=_("Unique name/identifier for this template"),
    )
    code = CharField(
        _("Template Code"),
        max_length=100,
        unique=True,
        help_text=_("Unique code for programmatic access to this template"),
        db_index=True,
    )
    communication_type = CharField(
        _("Communication Type"),
        max_length=50,
        choices=CommunicationType.choices,
        help_text=_("Type of communication (email, push notification, or SMS)"),
        db_index=True,
    )
    # Email-specific fields
    subject = CharField(
        _("Subject/Title"),
        max_length=255,
        blank=True,
        help_text=_("Email subject or push notification title (supports template variables)"),
    )
    # Content fields (used for all types)
    body_text = TextField(
        _("Plain Text Body"),
        help_text=_("Plain text body for email/SMS or message for push notification (supports template variables)"),
    )
    body_html = TextField(
        _("HTML Body"),
        blank=True,
        help_text=_("HTML body for email (supports template variables, not used for push/SMS)"),
    )
    # SMS-specific fields
    sms_body = TextField(
        _("SMS Body"),
        blank=True,
        help_text=_("SMS message body (supports template variables, used only for SMS)"),
    )
    # Push notification specific fields
    push_data = JSONField(
        _("Push Notification Data"),
        default=dict,
        blank=True,
        help_text=_("Additional data payload for push notifications"),
    )
    description = TextField(
        _("Description"),
        blank=True,
        help_text=_("Description of when and how this template is used"),
    )
    variable_help = TextField(
        _("Variable Help"),
        blank=True,
        help_text=_("Documentation of available template variables (e.g., {{ user_name }}, {{ verification_url }})"),
    )
    is_active = BooleanField(
        _("Is Active"),
        default=True,
        help_text=_("Whether this template is active and can be used"),
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)
    created_by = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="created_communication_templates",
        verbose_name=_("Created By"),
    )
    updated_by = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="updated_communication_templates",
        verbose_name=_("Updated By"),
    )

    class Meta:
        verbose_name = _("Communication Template")
        verbose_name_plural = _("Communication Templates")
        ordering = ["communication_type", "name"]
        indexes = [
            models.Index(fields=["communication_type", "is_active"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["code", "communication_type"],
                name="unique_template_code_per_type",
            ),
        ]

    def save(self, *args, **kwargs):
        """Override save to automatically set created_by and updated_by.
        
        These fields should always be set to the logged-in admin user.
        Only admin users should be able to create/update templates.
        """
        current_user = get_current_user()
        
        # Validate that only admin users can create/update templates
        # (This is a safety check - admin panel should enforce this)
        if current_user and not current_user.is_staff:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied(
                "Only admin users can create or update communication templates. "
                "Templates should only be managed through the admin panel."
            )
        
        # Set created_by for new instances (only if not already set)
        if self.pk is None and current_user and not self.created_by:
            self.created_by = current_user
        
        # Always set updated_by to current user if available
        if current_user:
            self.updated_by = current_user
        
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.get_communication_type_display()}: {self.name}"


class CommunicationLog(models.Model):
    """
    Communication log model for tracking all communications sent to users.
    
    This table tracks which communication was sent to which user and its status.
    """

    class CommunicationType(models.TextChoices):
        EMAIL = "email", _("Email")
        PUSH_NOTIFICATION = "push_notification", _("Push Notification")
        SMS = "sms", _("SMS")

    class CommunicationStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        QUEUED = "queued", _("Queued")
        SENT = "sent", _("Sent")
        DELIVERED = "delivered", _("Delivered")
        READ = "read", _("Read")
        FAILED = "failed", _("Failed")
        BOUNCED = "bounced", _("Bounced")
        REJECTED = "rejected", _("Rejected")

    communication_type = CharField(
        _("Communication Type"),
        max_length=50,
        choices=CommunicationType.choices,
        help_text=_("Type of communication (email, push notification, or SMS)"),
        db_index=True,
    )
    recipient = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="communication_logs",
        verbose_name=_("Recipient"),
        help_text=_("User who received or should receive this communication"),
    )
    recipient_email = CharField(
        _("Recipient Email"),
        max_length=255,
        blank=True,
        help_text=_("Snapshot of recipient email (kept if user is deleted)"),
        db_index=True,
    )
    template = ForeignKey(
        CommunicationTemplate,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="communication_logs",
        verbose_name=_("Template"),
        help_text=_("Template used for this communication"),
    )
    subject = CharField(
        _("Subject/Title"),
        max_length=255,
        blank=True,
        help_text=_("Email subject or push notification title"),
    )
    message = TextField(
        _("Message"),
        blank=True,
        help_text=_("Message content of the communication"),
    )
    status = CharField(
        _("Status"),
        max_length=50,
        choices=CommunicationStatus.choices,
        default=CommunicationStatus.PENDING,
        help_text=_("Current status of the communication"),
        db_index=True,
    )
    # Tracking fields
    sent_at = DateTimeField(
        _("Sent At"),
        null=True,
        blank=True,
        help_text=_("Timestamp when the communication was sent"),
    )
    delivered_at = DateTimeField(
        _("Delivered At"),
        null=True,
        blank=True,
        help_text=_("Timestamp when the communication was delivered"),
    )
    read_at = DateTimeField(
        _("Read At"),
        null=True,
        blank=True,
        help_text=_("Timestamp when the communication was read (for push notifications)"),
    )
    # Error tracking
    error_message = TextField(
        _("Error Message"),
        blank=True,
        help_text=_("Error message if the communication failed"),
    )
    # Additional metadata
    metadata = JSONField(
        _("Metadata"),
        default=dict,
        blank=True,
        help_text=_("Additional metadata about the communication (e.g., provider response, tracking IDs)"),
    )
    # External provider IDs for tracking
    provider_message_id = CharField(
        _("Provider Message ID"),
        max_length=255,
        blank=True,
        help_text=_("Message ID from the communication provider (e.g., email service, push service)"),
        db_index=True,
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)
    created_by = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="created_communication_logs",
        verbose_name=_("Created By"),
    )

    class Meta:
        verbose_name = _("Communication Log")
        verbose_name_plural = _("Communication Logs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["communication_type", "status"]),
            models.Index(fields=["recipient", "status"]),
            models.Index(fields=["recipient_email", "status"]),
        ]

    def save(self, *args, **kwargs):
        """Override save to snapshot recipient email and set created_by for new instances."""
        if self.recipient and not self.recipient_email:
            try:
                self.recipient_email = self.recipient.email or ""
            except Exception:
                pass
        # Set created_by for new instances if not already set
        if self.pk is None and not self.created_by:
            current_user = get_current_user()
            if current_user:
                self.created_by = current_user
        
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        email = ""
        if self.recipient_email:
            email = self.recipient_email
        elif self.recipient:
            email = getattr(self.recipient, "email", "") or ""
        else:
            email = "(deleted user)"
        return f"{self.get_communication_type_display()} - {email} - {self.get_status_display()}"


class CommunicationWebhookEventLog(models.Model):
    """
    Webhook event log model for tracking webhook events from communication providers.
    
    This table manages webhook events from email, push notification, and SMS providers
    (e.g., Mailgun webhooks, Firebase Cloud Messaging webhooks, Twilio webhooks).
    """

    class WebhookEventType(models.TextChoices):
        # Email webhook events
        EMAIL_SENT = "email_sent", _("Email Sent")
        EMAIL_DELIVERED = "email_delivered", _("Email Delivered")
        EMAIL_OPENED = "email_opened", _("Email Opened")
        EMAIL_CLICKED = "email_clicked", _("Email Clicked")
        EMAIL_BOUNCED = "email_bounced", _("Email Bounced")
        EMAIL_COMPLAINED = "email_complained", _("Email Complained")
        EMAIL_FAILED = "email_failed", _("Email Failed")
        # Push notification webhook events
        PUSH_SENT = "push_sent", _("Push Sent")
        PUSH_DELIVERED = "push_delivered", _("Push Delivered")
        PUSH_OPENED = "push_opened", _("Push Opened")
        PUSH_FAILED = "push_failed", _("Push Failed")
        # SMS webhook events
        SMS_SENT = "sms_sent", _("SMS Sent")
        SMS_DELIVERED = "sms_delivered", _("SMS Delivered")
        SMS_FAILED = "sms_failed", _("SMS Failed")
        SMS_RECEIVED = "sms_received", _("SMS Received")
        # Generic
        OTHER = "other", _("Other")

    class ProviderType(models.TextChoices):
        MAILGUN = "mailgun", _("Mailgun")
        SENDGRID = "sendgrid", _("SendGrid")
        AWS_SES = "aws_ses", _("AWS SES")
        FIREBASE = "firebase", _("Firebase Cloud Messaging")
        APNS = "apns", _("Apple Push Notification Service")
        FCM = "fcm", _("Firebase Cloud Messaging")
        TWILIO = "twilio", _("Twilio")
        OTHER = "other", _("Other")

    webhook_event_type = CharField(
        _("Webhook Event Type"),
        max_length=50,
        choices=WebhookEventType.choices,
        help_text=_("Type of webhook event received"),
        db_index=True,
    )
    provider_type = CharField(
        _("Provider Type"),
        max_length=50,
        choices=ProviderType.choices,
        help_text=_("Communication provider that sent the webhook"),
        db_index=True,
    )
    communication_log = ForeignKey(
        CommunicationLog,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_events",
        verbose_name=_("Communication Log"),
        help_text=_("Related communication log entry (if matched)"),
    )
    provider_message_id = CharField(
        _("Provider Message ID"),
        max_length=255,
        blank=True,
        help_text=_("Message ID from the provider"),
        db_index=True,
    )
    recipient_email = CharField(
        _("Recipient Email"),
        max_length=255,
        blank=True,
        help_text=_("Recipient email address (from webhook payload)"),
    )
    recipient_phone = CharField(
        _("Recipient Phone"),
        max_length=20,
        blank=True,
        help_text=_("Recipient phone number (from webhook payload)"),
    )
    recipient_user = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="webhook_events",
        verbose_name=_("Recipient User"),
        help_text=_("Matched user from recipient email/phone"),
    )
    # Webhook payload
    webhook_payload = JSONField(
        _("Webhook Payload"),
        default=dict,
        help_text=_("Full webhook payload received from the provider"),
    )
    # Processing status
    processed = BooleanField(
        _("Processed"),
        default=False,
        help_text=_("Whether this webhook event has been processed"),
    )
    processed_at = DateTimeField(
        _("Processed At"),
        null=True,
        blank=True,
        help_text=_("Timestamp when the webhook was processed"),
    )
    processing_error = TextField(
        _("Processing Error"),
        blank=True,
        help_text=_("Error message if webhook processing failed"),
    )
    # Timestamps
    event_timestamp = DateTimeField(
        _("Event Timestamp"),
        null=True,
        blank=True,
        help_text=_("Timestamp of the event from the provider"),
    )
    received_at = DateTimeField(
        _("Received At"),
        auto_now_add=True,
        help_text=_("Timestamp when the webhook was received"),
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Communication Webhook Event Log")
        verbose_name_plural = _("Communication Webhook Event Logs")
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["provider_type", "webhook_event_type"]),
            models.Index(fields=["processed", "received_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_provider_type_display()} - {self.get_webhook_event_type_display()} - {self.received_at}"


class Notice(models.Model):
    """
    Public notice board model for displaying announcements and updates.
    
    Notices are displayed on the home page and can be of different types:
    - info: General information
    - warning: Important warnings
    - urgent: Urgent announcements
    """
    
    class NoticeType(models.TextChoices):
        INFO = "info", _("Info")
        WARNING = "warning", _("Warning")
        URGENT = "urgent", _("Urgent")

    notice_id = models.AutoField(primary_key=True)
    title = CharField(
        _("Title"),
        max_length=255,
        help_text=_("Title of the notice"),
    )
    description = TextField(
        _("Description"),
        help_text=_("Short description of the notice"),
    )
    content = TextField(
        _("Content"),
        blank=True,
        null=True,
        help_text=_("Full content of the notice (optional)"),
    )
    notice_type = CharField(
        _("Notice Type"),
        max_length=20,
        choices=NoticeType.choices,
        default=NoticeType.INFO,
        help_text=_("Type of notice"),
        db_index=True,
    )
    is_active = BooleanField(
        _("Is Active"),
        default=True,
        help_text=_("Whether the notice is active and visible"),
        db_index=True,
    )
    priority = models.IntegerField(
        _("Priority"),
        default=0,
        help_text=_("Priority for sorting (higher = more important)"),
    )
    created_by = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="notices_created",
        verbose_name=_("Created By"),
        help_text=_("User who created this notice"),
    )
    expiry_date = DateTimeField(
        _("Expiry Date"),
        null=True,
        blank=True,
        help_text=_("Date and time when the notice expires (optional)"),
        db_index=True,
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Notice")
        verbose_name_plural = _("Notices")
        ordering = ["-priority", "-created_at"]
        indexes = [
            models.Index(fields=["is_active", "created_at"]),
            models.Index(fields=["notice_type", "is_active"]),
        ]

    @property
    def is_expired(self):
        """Check if the notice has expired."""
        if self.expiry_date is None:
            return False
        return timezone.now() > self.expiry_date

    def save(self, *args, **kwargs):
        """Override save to automatically set created_by for new instances."""
        if self.pk is None and not self.created_by:
            current_user = get_current_user()
            if current_user:
                self.created_by = current_user
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.title
