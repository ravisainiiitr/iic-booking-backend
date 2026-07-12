"""Support ticket models for managing user queries, requests, and complaints."""

from django.db import models
from django.db.models import (
    AutoField,
    BooleanField,
    CharField,
    DateTimeField,
    EmailField,
    ForeignKey,
    PROTECT,
    SET_NULL,
    TextField,
)
from django.utils.translation import gettext_lazy as _

from iic_booking.users.models import User


# Ticket Type Constants
class TicketTypeCode:
    """Constants for ticket type codes."""
    BOOKING = "booking"
    EQUIPMENT = "equipment"
    OTHER = "other"
    QUALITY_IMPROVEMENT = "quality_improvement"

    @classmethod
    def choices(cls):
        """Return choices tuple for Django model field."""
        return (
            (cls.BOOKING, _("Booking")),
            (cls.EQUIPMENT, _("Equipment")),
            (cls.OTHER, _("Other")),
            (cls.QUALITY_IMPROVEMENT, _("Quality improvement suggestions/Bugs")),
        )
    
    @classmethod
    def get_display_name(cls, code):
        """Get display name for a ticket type code."""
        choices_dict = dict(cls.choices())
        return choices_dict.get(code, code)


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
        BOOKING = TicketTypeCode.BOOKING, _("Booking")
        EQUIPMENT = TicketTypeCode.EQUIPMENT, _("Equipment")
        OTHER = TicketTypeCode.OTHER, _("Other")
        QUALITY_IMPROVEMENT = TicketTypeCode.QUALITY_IMPROVEMENT, _("Quality improvement suggestions/Bugs")
    
    ticket_id = AutoField(primary_key=True)
    
    # User information (can be authenticated user or public user)
    user = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
        verbose_name=_("User"),
        help_text=_("Authenticated user who created the ticket (if applicable)"),
    )
    
    # Public user information (for non-authenticated users)
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
    
    # Ticket details
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

    # Optional attachment (screenshot / document)
    attachment = models.FileField(
        _("Attachment"),
        upload_to="support/ticket_attachments/%Y/%m/%d/",
        blank=True,
        null=True,
        help_text=_("Optional document/image attached by the requester"),
    )
    
    # Related entities (optional)
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
    
    # Status and priority
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
    
    # Assignment and resolution
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
    
    # Timestamps
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
        """Get the display name of the ticket type."""
        return TicketTypeCode.get_display_name(self.ticket_type)
    
    def __str__(self) -> str:
        user_info = self.user.email if self.user else (self.public_email or "Anonymous")
        return f"#{self.ticket_id} - {self.subject} ({user_info})"
    
    def get_user_email(self) -> str:
        """Get the email of the user (authenticated or public)."""
        if self.user:
            return self.user.email
        return self.public_email or ""
    
    def get_user_name(self) -> str:
        """Get the name of the user (authenticated or public)."""
        if self.user:
            return self.user.name or self.user.email
        return self.public_name or "Anonymous"
    
    def get_user_phone(self) -> str:
        """Get the phone number of the user (authenticated or public)."""
        if self.user:
            return self.user.phone_number or ""
        return self.public_phone or ""


class TicketComment(models.Model):
    """
    Comments on tickets for communication between users and staff.
    """
    
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
