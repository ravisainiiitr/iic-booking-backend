"""Email invitations from students to supervisors who are not on the portal yet."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class SupervisorInviteStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    ACCEPTED = "accepted", _("Accepted")
    EXPIRED = "expired", _("Expired")
    CANCELLED = "cancelled", _("Cancelled")


class SupervisorInvite(models.Model):
    """A student's request, by email, for a supervisor to sign in and review a wallet link request.

    The raw token is only ever sent in the email; ``token_hash`` stores its SHA-256 digest. When a faculty
    member with ``email`` signs in, pending invites become ordinary pending ``WalletJoinRequest`` rows.
    """

    student = models.ForeignKey(
        "users.User",
        on_delete=models.CASCADE,
        related_name="supervisor_invites_sent",
        verbose_name=_("Student"),
    )
    email = models.EmailField(_("Supervisor email"), max_length=254, db_index=True)
    supervisor_name = models.CharField(_("Supervisor name"), max_length=255, blank=True, default="")
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supervisor_invites",
        verbose_name=_("Department"),
    )
    message = models.TextField(_("Message"), blank=True, default="")
    status = models.CharField(
        _("Status"),
        max_length=16,
        choices=SupervisorInviteStatus.choices,
        default=SupervisorInviteStatus.PENDING,
        db_index=True,
    )
    token_hash = models.CharField(_("Token hash"), max_length=64, unique=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    expires_at = models.DateTimeField(_("Expires at"), db_index=True)
    last_sent_at = models.DateTimeField(_("Last sent at"), null=True, blank=True)
    send_count = models.PositiveIntegerField(_("Times sent"), default=0)
    accepted_at = models.DateTimeField(_("Accepted at"), null=True, blank=True)
    accepted_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supervisor_invites_accepted",
        verbose_name=_("Accepted by"),
    )
    join_request = models.ForeignKey(
        "users.WalletJoinRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supervisor_invites",
        verbose_name=_("Wallet link request"),
    )
    cancelled_at = models.DateTimeField(_("Cancelled at"), null=True, blank=True)

    class Meta:
        verbose_name = _("Supervisor invite")
        verbose_name_plural = _("Supervisor invites")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email", "status"], name="users_supinv_email_status"),
            models.Index(fields=["student", "status"], name="users_supinv_student_status"),
        ]

    def __str__(self) -> str:
        return f"Invite {self.pk} → {self.email} ({self.status})"


class SupervisorInviteEvent(models.Model):
    """Audit trail for supervisor invites (no tokens are stored here)."""

    class Action(models.TextChoices):
        CREATED = "created", _("Created")
        RESENT = "resent", _("Resent")
        CANCELLED = "cancelled", _("Cancelled")
        EXPIRED = "expired", _("Expired")
        ACCEPTED = "accepted", _("Accepted")
        REFUSED = "refused", _("Refused")
        EMAIL_FAILED = "email_failed", _("Email failed")

    invite = models.ForeignKey(
        SupervisorInvite,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="events",
        verbose_name=_("Invite"),
    )
    action = models.CharField(_("Action"), max_length=16, choices=Action.choices, db_index=True)
    actor = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Actor"),
    )
    email = models.EmailField(_("Supervisor email"), max_length=254, blank=True, default="", db_index=True)
    details = models.JSONField(_("Details"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Supervisor invite event")
        verbose_name_plural = _("Supervisor invite events")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} {self.email}"
