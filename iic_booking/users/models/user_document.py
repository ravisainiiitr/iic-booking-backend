"""UserDocument model for storing user registration documents."""

from django.db import models
from django.db.models import CASCADE
from django.db.models import CharField
from django.db.models import DateTimeField
from django.db.models import FileField
from django.db.models import ForeignKey
from django.db.models import Model
from django.utils.translation import gettext_lazy as _

from .user import User


class UserDocument(Model):
    """Model for storing documents uploaded during user registration."""
    
    user = ForeignKey(
        User,
        on_delete=CASCADE,
        related_name='documents',
        verbose_name=_("User"),
        help_text=_("User who uploaded this document"),
    )
    file = FileField(
        _("Document File"),
        upload_to="user_documents/%Y/%m/%d/",
        help_text=_("The document file"),
    )
    document_type = CharField(
        _("Document Type"),
        max_length=100,
        blank=True,
        help_text=_("Type of document (e.g., 'identity_proof', 'address_proof', 'institution_id')"),
    )
    description = CharField(
        _("Description"),
        max_length=255,
        blank=True,
        help_text=_("Optional description of the document"),
    )
    uploaded_at = DateTimeField(_("Uploaded at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)
    
    class Meta:
        verbose_name = _("User Document")
        verbose_name_plural = _("User Documents")
        ordering = ['-uploaded_at']
    
    def __str__(self) -> str:
        return f"{self.user.email} - {self.document_type or 'Document'} ({self.uploaded_at.date()})"

