"""Test-data marks (models). Imported at the end of ``iic_booking.equipment.models``.

Equipment and equipment categories used only for testing are marked here, so dashboards, overviews and reports can
leave them out without changing who can see or book them (``Equipment.visible_to_test_accounts_only`` does that).
Users are marked with ``User.is_test_account``. See ``iic_booking.equipment.testdata`` for the query helpers.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class TestDataKind(models.TextChoices):
    __test__ = False

    EQUIPMENT = "equipment", _("Equipment")
    CATEGORY = "category", _("Equipment category")


class TestDataFlag(models.Model):
    __test__ = False

    kind = models.CharField(_("Kind"), max_length=16, choices=TestDataKind.choices)
    object_id = models.PositiveIntegerField(_("Record id"))
    label = models.CharField(_("Name when marked"), max_length=255, blank=True, default="")
    reason = models.CharField(_("Why it is test data"), max_length=255, blank=True, default="")
    flagged_at = models.DateTimeField(_("Marked at"), auto_now_add=True)
    flagged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Marked by"),
    )

    class Meta:
        verbose_name = _("Test data mark")
        verbose_name_plural = _("Test data marks")
        ordering = ["kind", "object_id"]
        constraints = [models.UniqueConstraint(fields=["kind", "object_id"], name="equipment_testdataflag_unique")]

    def __str__(self) -> str:
        return f"Test {self.kind} {self.object_id} ({self.label})"
