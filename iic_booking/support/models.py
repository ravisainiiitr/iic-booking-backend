"""Support ticket models for managing user queries, requests, and complaints."""

from django.db import models
from django.db.models import (
    AutoField,
    BooleanField,
    CharField,
    DateTimeField,
    EmailField,
    ForeignKey,
    IntegerField,
    JSONField,
    PROTECT,
    SET_NULL,
    TextField,
    UniqueConstraint,
)
from django.utils.translation import gettext_lazy as _

from iic_booking.users.models import User


# Ticket Type Constants
class TicketTypeCode:
    """Constants for ticket type codes."""
    BOOKING = "booking"
    EQUIPMENT = "equipment"
    PAYMENT = "payment"
    ACCOUNT = "account"
    TECHNICAL = "technical"
    LABORATORY = "laboratory"
    GENERAL = "general"
    OTHER = "other"
    QUALITY_IMPROVEMENT = "quality_improvement"

    @classmethod
    def choices(cls):
        """Return choices tuple for Django model field."""
        return (
            (cls.BOOKING, _("Booking Issues")),
            (cls.EQUIPMENT, _("Equipment Support")),
            (cls.PAYMENT, _("Payment Issues")),
            (cls.ACCOUNT, _("Account Support")),
            (cls.TECHNICAL, _("Technical Problems")),
            (cls.LABORATORY, _("Laboratory Requests")),
            (cls.GENERAL, _("General Enquiries")),
            (cls.OTHER, _("Other")),
            (cls.QUALITY_IMPROVEMENT, _("Quality improvement suggestions/Bugs")),
        )

    @classmethod
    def get_display_name(cls, code):
        """Get display name for a ticket type code."""
        choices_dict = dict(cls.choices())
        return choices_dict.get(code, code)

    @classmethod
    def as_api_list(cls):
        return [{"code": code, "name": str(name)} for code, name in cls.choices()]


class Ticket(models.Model):
    """
    Support ticket model for managing user queries, requests, and complaints.

    Tickets can be created by:
    - Authenticated users (linked to their account)
    - Public users (using email/phone)
    """

    class TicketStatus(models.TextChoices):
        OPEN = "open", _("Open")
        IN_PROGRESS = "in_progress", _("In Progress")
        RESOLVED = "resolved", _("Resolved")
        CLOSED = "closed", _("Closed")
        CANCELLED = "cancelled", _("Cancelled")

    class TicketPriority(models.TextChoices):
        LOW = "low", _("Low")
        MEDIUM = "medium", _("Medium")
        HIGH = "high", _("High")
        URGENT = "urgent", _("Urgent")

    class TicketType(models.TextChoices):
        BOOKING = TicketTypeCode.BOOKING, _("Booking Issues")
        EQUIPMENT = TicketTypeCode.EQUIPMENT, _("Equipment Support")
        PAYMENT = TicketTypeCode.PAYMENT, _("Payment Issues")
        ACCOUNT = TicketTypeCode.ACCOUNT, _("Account Support")
        TECHNICAL = TicketTypeCode.TECHNICAL, _("Technical Problems")
        LABORATORY = TicketTypeCode.LABORATORY, _("Laboratory Requests")
        GENERAL = TicketTypeCode.GENERAL, _("General Enquiries")
        OTHER = TicketTypeCode.OTHER, _("Other")
        QUALITY_IMPROVEMENT = TicketTypeCode.QUALITY_IMPROVEMENT, _("Quality improvement suggestions/Bugs")

    ticket_id = AutoField(primary_key=True)

    user = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
        verbose_name=_("User"),
        help_text=_("Authenticated user who created the ticket (if applicable)"),
    )

    public_name = CharField(
        _("Name"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Name of public user (if not authenticated)"),
    )
    public_email = EmailField(
        _("Email"),
        blank=True,
        null=True,
        help_text=_("Email of public user (if not authenticated)"),
    )
    public_phone = CharField(
        _("Phone Number"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("Phone number of public user (if not authenticated)"),
    )

    ticket_type = CharField(
        _("Ticket Type"),
        max_length=50,
        choices=TicketType.choices,
        default=TicketType.OTHER,
        help_text=_("Type of ticket"),
        db_index=True,
    )
    subject = CharField(
        _("Subject"),
        max_length=255,
        help_text=_("Subject/title of the ticket"),
    )
    description = TextField(
        _("Description"),
        help_text=_("Detailed description of the issue/query/request"),
    )

    attachment = models.FileField(
        _("Attachment"),
        upload_to="support/ticket_attachments/%Y/%m/%d/",
        max_length=512,
        blank=True,
        null=True,
        help_text=_("Optional document/image attached by the requester"),
    )

    related_equipment = ForeignKey(
        "equipment.Equipment",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
        verbose_name=_("Related Equipment"),
        help_text=_("Equipment related to this ticket (if applicable)"),
    )
    related_booking = ForeignKey(
        "equipment.Booking",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
        verbose_name=_("Related Booking"),
        help_text=_("Booking related to this ticket (if applicable)"),
    )

    status = CharField(
        _("Status"),
        max_length=20,
        choices=TicketStatus.choices,
        default=TicketStatus.OPEN,
        help_text=_("Current status of the ticket"),
        db_index=True,
    )
    priority = CharField(
        _("Priority"),
        max_length=20,
        choices=TicketPriority.choices,
        default=TicketPriority.MEDIUM,
        help_text=_("Priority level of the ticket"),
        db_index=True,
    )

    assigned_to = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tickets",
        verbose_name=_("Assigned To"),
        help_text=_("Staff member assigned to handle this ticket"),
    )
    resolution_notes = TextField(
        _("Resolution Notes"),
        blank=True,
        null=True,
        help_text=_("Notes about how the ticket was resolved"),
    )

    created_at = DateTimeField(_("Created at"), auto_now_add=True, db_index=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)
    resolved_at = DateTimeField(
        _("Resolved at"),
        null=True,
        blank=True,
        help_text=_("Timestamp when the ticket was resolved"),
    )
    closed_at = DateTimeField(
        _("Closed at"),
        null=True,
        blank=True,
        help_text=_("Timestamp when the ticket was closed"),
    )

    class Meta:
        verbose_name = _("Ticket")
        verbose_name_plural = _("Tickets")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["ticket_type", "status"]),
            models.Index(fields=["priority", "status"]),
            models.Index(fields=["user", "status"]),
        ]

    def get_ticket_type_display(self) -> str:
        return TicketTypeCode.get_display_name(self.ticket_type)

    def __str__(self) -> str:
        user_info = self.user.email if self.user else (self.public_email or "Anonymous")
        return f"#{self.ticket_id} - {self.subject} ({user_info})"

    def get_user_email(self) -> str:
        if self.user:
            return self.user.email
        return self.public_email or ""

    def get_user_name(self) -> str:
        if self.user:
            return self.user.get_display_name()
        return self.public_name or "Anonymous"

    def get_user_phone(self) -> str:
        if self.user:
            return self.user.phone_number or ""
        return self.public_phone or ""


class TicketComment(models.Model):
    """Comments on tickets for communication between users and staff."""

    comment_id = AutoField(primary_key=True)
    ticket = ForeignKey(
        Ticket,
        on_delete=PROTECT,
        related_name="comments",
        verbose_name=_("Ticket"),
        help_text=_("Ticket this comment belongs to"),
    )
    user = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_comments",
        verbose_name=_("User"),
        help_text=_("User who made the comment (staff or ticket creator)"),
    )
    comment = TextField(
        _("Comment"),
        help_text=_("Comment text"),
    )
    is_internal = BooleanField(
        _("Is Internal"),
        default=False,
        help_text=_("Whether this comment is internal (only visible to staff)"),
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Ticket Comment")
        verbose_name_plural = _("Ticket Comments")
        ordering = ["created_at"]

    def __str__(self) -> str:
        user_info = self.user.email if self.user else "Anonymous"
        return f"Comment on #{self.ticket.ticket_id} by {user_info}"


class TicketEvent(models.Model):
    """Structured audit trail for support tickets."""

    class EventType(models.TextChoices):
        CREATED = "created", _("Created")
        STATUS_CHANGED = "status_changed", _("Status Changed")
        ASSIGNED = "assigned", _("Assigned")
        COMMENT = "comment", _("Comment")
        INTERNAL_NOTE = "internal_note", _("Internal Note")
        RESOLVED = "resolved", _("Resolved")
        CLOSED = "closed", _("Closed")
        PRIORITY_CHANGED = "priority_changed", _("Priority Changed")
        NOTES_UPDATED = "notes_updated", _("Notes Updated")

    event_id = AutoField(primary_key=True)
    ticket = ForeignKey(
        Ticket,
        on_delete=PROTECT,
        related_name="events",
        verbose_name=_("Ticket"),
    )
    event_type = CharField(
        _("Event Type"),
        max_length=40,
        choices=EventType.choices,
        db_index=True,
    )
    actor = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_events",
        verbose_name=_("Actor"),
    )
    message = TextField(_("Message"), blank=True, default="")
    from_value = CharField(_("From"), max_length=255, blank=True, default="")
    to_value = CharField(_("To"), max_length=255, blank=True, default="")
    metadata = JSONField(_("Metadata"), default=dict, blank=True)
    is_internal = BooleanField(
        _("Is Internal"),
        default=False,
        help_text=_("Hide from non-staff requesters when True."),
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Ticket Event")
        verbose_name_plural = _("Ticket Events")
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"#{self.ticket_id} {self.event_type} @ {self.created_at}"


class PortalFeedback(models.Model):
    """Structured UX feedback from authenticated portal users (one row per user)."""

    feedback_id = AutoField(primary_key=True)
    user = ForeignKey(
        User,
        on_delete=PROTECT,
        related_name="portal_feedback",
        verbose_name=_("User"),
    )
    overall_rating = IntegerField(_("Overall rating"), help_text=_("1–5 stars"))
    ease_of_booking = IntegerField(_("Ease of booking"), help_text=_("1–5 stars"))
    website_usability = IntegerField(_("Website usability"), help_text=_("1–5 stars"))
    equipment_booking_experience = IntegerField(
        _("Equipment booking experience"),
        help_text=_("1–5 stars"),
    )
    suggestions = TextField(_("Suggestions for improvement"), blank=True, default="")
    comments = TextField(_("Additional comments"), blank=True, default="")
    created_at = DateTimeField(_("Created at"), auto_now_add=True, db_index=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Portal Feedback")
        verbose_name_plural = _("Portal Feedback")
        constraints = [
            UniqueConstraint(fields=["user"], name="unique_portal_feedback_per_user"),
        ]
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"Feedback #{self.feedback_id} by {self.user_id} ({self.overall_rating}★)"

    @property
    def average_rating(self) -> float:
        vals = [
            self.overall_rating,
            self.ease_of_booking,
            self.website_usability,
            self.equipment_booking_experience,
        ]
        return round(sum(vals) / len(vals), 2)