"""Faculty approval of self-registered (non Channel i) accounts, programme expiry and extensions.

``RegistrationApproval`` is one row per self-registered user: who the request was sent to, its decision
and the expiry state. Extensions are separate rows so repeated extensions keep their own disclaimer.
Raw review tokens are only ever emailed; ``RegistrationApprovalToken.token_hash`` stores the SHA-256.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class RegistrationApprovalStatus(models.TextChoices):
    PENDING_FACULTY = "pending_faculty", _("Pending faculty")
    PENDING_ADMIN = "pending_admin", _("Pending admin")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")


class RegistrationExtensionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    APPROVED = "approved", _("Approved")
    DENIED = "denied", _("Denied")
    CANCELLED = "cancelled", _("Cancelled")


class RegistrationApprovalChannel(models.TextChoices):
    PORTAL = "portal", _("Portal")
    EMAIL_LINK = "email_link", _("Email link")
    LOGIN = "login", _("Sign-in page")
    SYSTEM = "system", _("System")


class RegistrationApproval(models.Model):
    user = models.OneToOneField(
        "users.User",
        on_delete=models.CASCADE,
        related_name="registration_approval",
        verbose_name=_("User"),
    )
    faculty = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="registration_approvals_as_faculty",
        verbose_name=_("Faculty the request is addressed to"),
    )
    status = models.CharField(
        _("Status"),
        max_length=20,
        choices=RegistrationApprovalStatus.choices,
        default=RegistrationApprovalStatus.PENDING_ADMIN,
        db_index=True,
    )
    forwarded_at = models.DateTimeField(_("Forwarded at"), null=True, blank=True)
    forwarded_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name=_("Forwarded by")
    )
    forward_count = models.PositiveIntegerField(_("Times forwarded"), default=0)
    decision_deadline = models.DateTimeField(
        _("Faculty must decide by"),
        null=True,
        blank=True,
        db_index=True,
        help_text=_(
            "Set each time the request is sent to the faculty. A pending request past this time is treated as "
            "declined. Empty for requests never sent (they are never timed out)."
        ),
    )
    last_reminder_at = models.DateTimeField(_("Last reminder at"), null=True, blank=True)
    reminder_count = models.PositiveIntegerField(_("Reminders sent"), default=0)
    first_viewed_at = models.DateTimeField(_("First viewed by faculty at"), null=True, blank=True)
    decided_at = models.DateTimeField(_("Decided at"), null=True, blank=True)
    decided_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name=_("Decided by")
    )
    decided_role = models.CharField(_("Decided as"), max_length=20, blank=True, default="")
    decision_reason = models.TextField(_("Decision reason"), blank=True, default="")
    decision_channel = models.CharField(
        _("Decision channel"), max_length=16, choices=RegistrationApprovalChannel.choices, blank=True, default=""
    )
    disclaimer_text = models.TextField(_("Disclaimer confirmed"), blank=True, default="")
    disclaimer_version = models.CharField(_("Disclaimer version"), max_length=32, blank=True, default="")
    expiry_disabled_at = models.DateTimeField(_("Disabled at programme expiry"), null=True, blank=True)
    expiry_set_force_inactive = models.BooleanField(
        _("Disabled by the expiry automation"),
        default=False,
        help_text=_("True only when the expiry automation turned on Force Inactive, so an extension can undo it."),
    )
    expiry_warnings_sent = models.JSONField(
        _("Expiry warnings sent"),
        default=dict,
        blank=True,
        help_text=_('{"<programme end date>": [30, 7, 1]} — warnings already sent for that end date.'),
    )
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Registration approval")
        verbose_name_plural = _("Registration approvals")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["faculty", "status"], name="users_regappr_fac_status")]

    def __str__(self) -> str:
        return f"Registration approval {self.pk} user={self.user_id} ({self.status})"


class RegistrationExtensionRequest(models.Model):
    user = models.ForeignKey(
        "users.User", on_delete=models.CASCADE, related_name="registration_extensions", verbose_name=_("User")
    )
    faculty = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="registration_extensions_as_faculty",
        verbose_name=_("Faculty"),
    )
    status = models.CharField(
        _("Status"),
        max_length=16,
        choices=RegistrationExtensionStatus.choices,
        default=RegistrationExtensionStatus.PENDING,
        db_index=True,
    )
    previous_end_date = models.DateField(_("Programme validity before the extension"), null=True, blank=True)
    max_until = models.DateField(_("Latest date allowed (6 months)"))
    approved_until = models.DateField(_("Extended until"), null=True, blank=True)
    user_reason = models.TextField(_("Reason given by the user"), blank=True, default="")
    requested_channel = models.CharField(
        _("Requested from"), max_length=16, choices=RegistrationApprovalChannel.choices, blank=True, default=""
    )
    decided_at = models.DateTimeField(_("Decided at"), null=True, blank=True)
    decided_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name=_("Decided by")
    )
    decided_role = models.CharField(_("Decided as"), max_length=20, blank=True, default="")
    decision_reason = models.TextField(_("Decision reason"), blank=True, default="")
    decision_channel = models.CharField(
        _("Decision channel"), max_length=16, choices=RegistrationApprovalChannel.choices, blank=True, default=""
    )
    disclaimer_text = models.TextField(_("Disclaimer confirmed"), blank=True, default="")
    disclaimer_version = models.CharField(_("Disclaimer version"), max_length=32, blank=True, default="")
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Programme extension request")
        verbose_name_plural = _("Programme extension requests")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["faculty", "status"], name="users_regext_fac_status")]

    def __str__(self) -> str:
        return f"Extension {self.pk} user={self.user_id} ({self.status})"


class RegistrationApprovalToken(models.Model):
    class Purpose(models.TextChoices):
        REGISTRATION = "registration", _("Registration")
        EXTENSION = "extension", _("Extension")

    token_hash = models.CharField(_("Token hash"), max_length=64, unique=True)
    purpose = models.CharField(_("Purpose"), max_length=16, choices=Purpose.choices)
    class Outcome(models.TextChoices):
        APPROVED = "approved", _("Approved")
        DECLINED = "declined", _("Declined")
        TIMED_OUT = "timed_out", _("Timed out")
        CLOSED = "closed", _("Closed")

    # SET_NULL so a link outlives a removed account and a late click can say why it no longer works.
    approval = models.ForeignKey(
        RegistrationApproval, on_delete=models.SET_NULL, null=True, blank=True, related_name="tokens"
    )
    extension = models.ForeignKey(
        RegistrationExtensionRequest, on_delete=models.CASCADE, null=True, blank=True, related_name="tokens"
    )
    faculty = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="+", verbose_name=_("Faculty"))
    expires_at = models.DateTimeField(_("Expires at"))
    used_at = models.DateTimeField(_("Used at"), null=True, blank=True)
    outcome = models.CharField(_("Request outcome"), max_length=16, choices=Outcome.choices, blank=True, default="")
    subject_name = models.CharField(_("User name"), max_length=255, blank=True, default="")
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Registration approval link")
        verbose_name_plural = _("Registration approval links")
        ordering = ["-created_at"]


class RegistrationApprovalEvent(models.Model):
    class Action(models.TextChoices):
        SUBMITTED = "submitted", _("Submitted")
        FORWARDED = "forwarded", _("Forwarded to faculty")
        REMINDER_SENT = "reminder_sent", _("Reminder sent")
        VIEWED = "viewed", _("Viewed by faculty")
        APPROVED = "approved", _("Approved")
        DISAPPROVED = "disapproved", _("Disapproved")
        ADMIN_OVERRIDE = "admin_override", _("Admin override")
        FACULTY_CHANGED = "faculty_changed", _("Faculty changed")
        EXPIRY_WARNING = "expiry_warning", _("Expiry warning sent")
        DISABLED = "disabled", _("Disabled at programme expiry")
        EXTENSION_REQUESTED = "extension_requested", _("Extension requested")
        EXTENSION_GRANTED = "extension_granted", _("Extension granted")
        EXTENSION_DENIED = "extension_denied", _("Extension denied")
        RE_ENABLED = "re_enabled", _("Re-enabled")
        TOKEN_REFUSED = "token_refused", _("Review link refused")
        EMAIL_FAILED = "email_failed", _("Email failed")
        AUTOMATION_CHANGED = "automation_changed", _("Expiry automation switched")
        TIMED_OUT = "timed_out", _("Timed out (no faculty decision)")
        ACCOUNT_REMOVED = "account_removed", _("Pending account removal")
        USER_NOTIFIED = "user_notified", _("User told about the decision window")

    user = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name=_("User")
    )
    subject_email = models.EmailField(_("User email"), blank=True, default="", db_index=True)
    subject_name = models.CharField(_("User name"), max_length=255, blank=True, default="")
    approval = models.ForeignKey(
        RegistrationApproval, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    extension = models.ForeignKey(
        RegistrationExtensionRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    action = models.CharField(_("Action"), max_length=24, choices=Action.choices, db_index=True)
    actor = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name=_("Actor")
    )
    actor_email = models.EmailField(_("Actor email"), blank=True, default="")
    actor_role = models.CharField(_("Actor role"), max_length=24, blank=True, default="")
    channel = models.CharField(
        _("Channel"), max_length=16, choices=RegistrationApprovalChannel.choices, blank=True, default=""
    )
    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    details = models.JSONField(_("Details"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Registration approval event")
        verbose_name_plural = _("Registration approval events")
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.action} {self.subject_email}"


class RegistrationApprovalPolicy(models.Model):
    """Singleton switch for the programme expiry automation (warnings and disabling). Off by default."""

    expiry_automation_enabled = models.BooleanField(_("Programme expiry automation enabled"), default=False)
    warning_days = models.CharField(_("Warning days before expiry"), max_length=64, default="30,7,1")
    token_valid_days = models.PositiveSmallIntegerField(_("Faculty review link valid for (days)"), default=14)
    decision_window_hours = models.PositiveSmallIntegerField(
        _("Faculty decision window (hours)"),
        default=24,
        help_text=_("Hours the faculty member has to decide after a request is sent; afterwards it is treated as declined."),
    )
    enabled_at = models.DateTimeField(_("Enabled at"), null=True, blank=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)
    updated_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name=_("Updated by")
    )

    class Meta:
        verbose_name = _("Registration approval policy")
        verbose_name_plural = _("Registration approval policy")

    def __str__(self) -> str:
        return f"Expiry automation {'on' if self.expiry_automation_enabled else 'off'}"
