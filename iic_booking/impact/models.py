"""Research impact for funders: equipment funding lines, the acknowledgement wording a paper must carry, researchers'
ORCID iDs and publications found for them (suggested, never claimed automatically)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

USER = settings.AUTH_USER_MODEL


class EquipmentFunding(models.Model):
    """Where an instrument's money came from (e.g. DST - FIST, SERB, ANRF, institute), for funder reports."""

    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.CASCADE, related_name="funding_lines")
    funder = models.CharField(max_length=120, help_text=_("Funding agency, e.g. DST, SERB, ANRF, institute."))
    scheme = models.CharField(max_length=160, blank=True, default="", help_text=_("Scheme / programme, e.g. FIST."))
    sanction_reference = models.CharField(max_length=160, blank=True, default="")
    sanction_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    acknowledgement_phrase = models.CharField(
        max_length=300, blank=True, default="",
        help_text=_("Words the funder expects in a paper's acknowledgement, e.g. the scheme and sanction number. "
                    "Alternatives separated by |."),
    )
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["funder", "scheme", "pk"]
        indexes = [models.Index(fields=["funder", "scheme"])]

    def __str__(self) -> str:
        return f"{self.funder} {self.scheme} ({self.equipment_id})".strip()


class AcknowledgementPolicy(models.Model):
    """The facility's standard acknowledgement (one active row). ``required_phrases``: each item must appear;
    an item may list alternatives separated by |."""

    wording = models.TextField(help_text=_("Suggested acknowledgement sentence shown to users."))
    required_phrases = models.JSONField(default=list, blank=True)
    include_equipment_name = models.BooleanField(
        default=False, help_text=_("Also require the name (or code) of an instrument used."),
    )
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)


class AcknowledgementCheck(models.Model):
    """Latest check of a publication claim's acknowledgement text (kept so reviewers see the result)."""

    claim = models.OneToOneField("equipment.EquipmentPublicationClaim", on_delete=models.CASCADE, related_name="ack_check")
    text = models.TextField()
    result = models.CharField(max_length=10)  # COMPLETE / PARTIAL / MISSING
    found = models.JSONField(default=list)
    missing = models.JSONField(default=list)
    checked_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    checked_at = models.DateTimeField(auto_now=True)


class ResearcherOrcid(models.Model):
    user = models.OneToOneField(USER, on_delete=models.CASCADE, related_name="orcid_record")
    orcid = models.CharField(max_length=19, unique=True)
    discovery_enabled = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class SuggestionStatus(models.TextChoices):
    NEW = "NEW", _("New")
    CLAIMED = "CLAIMED", _("Claimed")
    DISMISSED = "DISMISSED", _("Dismissed")


class PublicationSuggestion(models.Model):
    """A paper found for a researcher (OpenAlex by ORCID) that may have used the facility; the user decides."""

    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="publication_suggestions")
    source = models.CharField(max_length=20, default="openalex")
    external_id = models.CharField(max_length=120)
    doi = models.CharField(max_length=255, blank=True, default="")
    title = models.CharField(max_length=500)
    journal = models.CharField(max_length=500, blank=True, default="")
    year = models.PositiveIntegerField(null=True, blank=True)
    authors = models.TextField(blank=True, default="")
    equipment_ids = models.JSONField(default=list, blank=True, help_text=_("Instruments the user booked before it."))
    status = models.CharField(max_length=10, choices=SuggestionStatus.choices, default=SuggestionStatus.NEW)
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-year", "-created_at"]
        constraints = [models.UniqueConstraint(fields=["user", "source", "external_id"], name="impact_suggestion_once")]
