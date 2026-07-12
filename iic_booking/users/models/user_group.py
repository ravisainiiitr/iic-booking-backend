"""User group model for equipment visibility.

User groups allow restricting equipment visibility to specific members.
Equipment with a visibility_group is only visible to members of that group.
Equipment without a visibility_group is visible to everyone (public).
"""

from django.db.models import CharField
from django.db.models import DateTimeField
from django.db.models import ForeignKey
from django.db.models import Model
from django.db.models import PROTECT
from django.db.models import TextField
from django.utils.translation import gettext_lazy as _

from .user import User


class UserGroup(Model):
    """
    A group of users. Equipment can be assigned to a group for visibility control.
    Only members of the group can see equipment bound to this group.
    """

    name = CharField(
        _("Group Name"),
        max_length=255,
        help_text=_("Name of the user group"),
    )
    code = CharField(
        _("Group Code"),
        max_length=50,
        unique=True,
        help_text=_("Short unique code for the group"),
    )
    description = TextField(
        _("Description"),
        blank=True,
        null=True,
        help_text=_("Optional description of the group"),
    )
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("User Group")
        verbose_name_plural = _("User Groups")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class UserGroupMember(Model):
    """Membership of a user in a user group."""

    user_group = ForeignKey(
        UserGroup,
        on_delete=PROTECT,
        related_name="members",
        verbose_name=_("User Group"),
    )
    user = ForeignKey(
        User,
        on_delete=PROTECT,
        related_name="user_group_memberships",
        verbose_name=_("User"),
    )
    created_at = DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("User Group Member")
        verbose_name_plural = _("User Group Members")
        unique_together = [["user_group", "user"]]
        ordering = ["user_group", "user"]

    def __str__(self):
        return f"{self.user_group.code} - {self.user.email}"
