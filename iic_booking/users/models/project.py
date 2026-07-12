"""Project model for faculty research projects."""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import BooleanField
from django.db.models import CharField
from django.db.models import DateField
from django.db.models import DateTimeField
from django.db.models import ForeignKey
from django.db.models import Model
from django.db.models import PROTECT
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .user import User
from .user_type import UserType


class Project(Model):
    """Project model for faculty research projects."""

    faculty = ForeignKey(
        User,
        on_delete=PROTECT,
        related_name="projects",
        verbose_name=_("Faculty"),
        help_text=_("Faculty member who owns this project"),
    )
    name = CharField(
        _("Project Name"),
        max_length=255,
        help_text=_("Name of the research project"),
    )
    project_code = CharField(
        _("Project Code"),
        max_length=100,
        help_text=_("Unique code for the project"),
    )
    agency = CharField(
        _("Funding Agency"),
        max_length=255,
        help_text=_("Name of the funding agency"),
    )
    start_date = DateField(
        _("Start Date"),
        blank=True,
        null=True,
        help_text=_("Project start date"),
    )
    end_date = DateField(
        _("End Date"),
        blank=True,
        null=True,
        help_text=_("Project end date"),
    )
    is_active = BooleanField(
        _("Is Active"),
        default=True,
        help_text=_("Whether the project is currently active"),
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Project")
        verbose_name_plural = _("Projects")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["faculty", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.project_code})"

    def clean(self) -> None:
        """Validate that user is faculty."""
        if self.faculty and self.faculty.user_type != UserType.FACULTY:
            raise ValidationError(
                _("Only faculty members can have projects.")
            )
        
        # Validate that end_date is after start_date if both are provided
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError(
                _("End date must be after start date.")
            )

    def save(self, *args, **kwargs) -> None:
        """Override save to validate and auto-disable if end_date has passed."""
        self.full_clean()
        
        # Auto-disable project if end_date has passed
        if self.end_date and self.is_active:
            today = timezone.localdate()
            if self.end_date < today:
                self.is_active = False
        
        super().save(*args, **kwargs)
    
    @property
    def is_expired(self) -> bool:
        """Check if project has expired based on end_date."""
        if not self.end_date:
            return False
        today = timezone.localdate()
        return self.end_date < today
