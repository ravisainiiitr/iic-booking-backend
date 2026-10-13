"""Search synonyms (models). Imported at the end of ``iic_booking.equipment.models``.

A technique (e.g. "X-ray diffraction") with the words people type for it ("crystal structure", "xrd") and the
categories / instruments that offer it. The header search uses them to find instruments by technique; see
``iic_booking.equipment.search_index``.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class SearchSynonym(models.Model):
    technique = models.CharField(_("Technique"), max_length=120, help_text=_('Shown to users, e.g. "X-ray diffraction"'))
    terms = models.TextField(
        _("Search terms"),
        blank=True,
        default="",
        help_text=_('Comma-separated words people search for, e.g. "xrd, crystal structure, phase identification"'),
    )
    categories = models.ManyToManyField(
        "equipment.EquipmentCategory", blank=True, related_name="search_synonyms", verbose_name=_("Categories")
    )
    equipment = models.ManyToManyField(
        "equipment.Equipment", blank=True, related_name="search_synonyms", verbose_name=_("Equipment")
    )
    is_active = models.BooleanField(_("Active"), default=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Updated by"),
    )

    class Meta:
        verbose_name = _("Search synonym")
        verbose_name_plural = _("Search synonyms")
        ordering = ["technique", "pk"]

    def __str__(self) -> str:
        return self.technique

    def term_list(self) -> list[str]:
        return split_terms(self.terms)


def split_terms(raw) -> list[str]:
    """Comma / newline separated terms, trimmed, lower-cased, without duplicates (order kept)."""
    if isinstance(raw, (list, tuple)):
        parts = [str(p) for p in raw]
    else:
        parts = str(raw or "").replace("\n", ",").replace(";", ",").split(",")
    seen: list[str] = []
    for part in parts:
        term = " ".join(part.split()).lower()[:80]
        if term and term not in seen:
            seen.append(term)
    return seen
