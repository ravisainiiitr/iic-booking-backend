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


# --------------------------------------------------------------------------- publication-to-facility mapping
class AliasSource(models.TextChoices):
    MANUAL = "MANUAL", _("Added by an administrator")
    NAME = "NAME", _("Department or equipment name")
    CODE = "CODE", _("Department or equipment code")
    CATEGORY = "CATEGORY", _("Equipment category")
    POLICY = "POLICY", _("Acknowledgement policy phrase")
    FUNDING = "FUNDING", _("Funding acknowledgement phrase")


class FacilityAlias(models.Model):
    """A phrase that, found in a paper's acknowledgement, points to a department/centre or to one equipment item.

    Exactly one of ``department`` / ``equipment`` is set. Generated aliases (name, code, category, policy and funding
    phrases) can be edited or switched off; administrators add their own (MANUAL)."""

    department = models.ForeignKey(
        "users.Department", on_delete=models.CASCADE, null=True, blank=True, related_name="facility_aliases"
    )
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.CASCADE, null=True, blank=True, related_name="facility_aliases"
    )
    phrase = models.CharField(max_length=200)
    case_sensitive = models.BooleanField(
        default=False, help_text=_("Match the exact case, e.g. for short acronyms that are also ordinary words.")
    )
    source = models.CharField(max_length=10, choices=AliasSource.choices, default=AliasSource.MANUAL)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["department_id", "equipment_id", "phrase"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(department__isnull=False, equipment__isnull=True)
                           | models.Q(department__isnull=True, equipment__isnull=False)),
                name="impact_alias_one_target",
            ),
            models.UniqueConstraint(fields=["department", "phrase"], condition=models.Q(equipment__isnull=True),
                                    name="impact_alias_department_phrase"),
            models.UniqueConstraint(fields=["equipment", "phrase"], condition=models.Q(department__isnull=True),
                                    name="impact_alias_equipment_phrase"),
        ]

    def __str__(self):
        return f"{self.phrase} -> {'equipment ' + str(self.equipment_id) if self.equipment_id else 'department ' + str(self.department_id)}"


class AuthorLinkStatus(models.TextChoices):
    AUTO_LINKED = "AUTO_LINKED", _("Linked automatically (unambiguous)")
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION", _("Needs confirmation")
    CONFIRMED = "CONFIRMED", _("Confirmed")
    REJECTED = "REJECTED", _("Not this person")


class ResearcherAuthorCandidate(models.Model):
    """An OpenAlex author record that may be this portal user (found by name + the institute's ROR ID)."""

    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="author_candidates")
    openalex_author_id = models.CharField(max_length=40)
    display_name = models.CharField(max_length=255)
    orcid = models.CharField(max_length=19, blank=True, default="")
    works_count = models.PositiveIntegerField(default=0)
    institutions = models.JSONField(default=list, blank=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=3)
    rationale = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=AuthorLinkStatus.choices, default=AuthorLinkStatus.NEEDS_CONFIRMATION)
    decided_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user_id", "-confidence"]
        constraints = [models.UniqueConstraint(fields=["user", "openalex_author_id"], name="impact_author_candidate_once")]

    @property
    def is_linked(self) -> bool:
        return self.status in (AuthorLinkStatus.AUTO_LINKED, AuthorLinkStatus.CONFIRMED)


class EvidenceStatus(models.TextChoices):
    NO_OPEN_ACCESS = "NO_OPEN_ACCESS", _("No legal open-access copy")
    FETCH_FAILED = "FETCH_FAILED", _("Open-access copy could not be read")
    NO_ACKNOWLEDGEMENT = "NO_ACKNOWLEDGEMENT", _("No acknowledgement section found")
    NO_MATCH = "NO_MATCH", _("Acknowledgement names no portal facility")
    MATCHED = "MATCHED", _("Acknowledgement names a portal facility")


class PublicationEvidence(models.Model):
    """The result of reading one paper's open-access acknowledgement for one researcher. One row per paper and user,
    however many facilities it names (see PublicationFacilityMapping)."""

    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="publication_evidence")
    suggestion = models.ForeignKey(
        PublicationSuggestion, on_delete=models.SET_NULL, null=True, blank=True, related_name="evidence"
    )
    doi = models.CharField(max_length=255, blank=True, default="", db_index=True)
    openalex_id = models.CharField(max_length=120, blank=True, default="")
    title = models.CharField(max_length=500)
    year = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=EvidenceStatus.choices)
    source_url = models.CharField(max_length=1000, blank=True, default="")
    oa_status = models.CharField(max_length=20, blank=True, default="")
    licence = models.CharField(max_length=60, blank=True, default="")
    acknowledgement_snippet = models.TextField(
        blank=True, default="", help_text=_("The acknowledgement text around the match (short excerpt only).")
    )
    detail = models.CharField(max_length=500, blank=True, default="")
    checked_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-checked_at"]
        constraints = [models.UniqueConstraint(fields=["user", "doi"], condition=~models.Q(doi=""),
                                               name="impact_evidence_user_doi")]


class MappingLevel(models.TextChoices):
    DEPARTMENT = "DEPARTMENT", _("Department / centre")
    EQUIPMENT = "EQUIPMENT", _("Equipment")


class MappingStatus(models.TextChoices):
    VERIFIED = "VERIFIED", _("Verified")
    CLAIM_PENDING = "CLAIM_PENDING", _("Equipment claim sent to the OIC")
    BELOW_THRESHOLD = "BELOW_THRESHOLD", _("Evidence below the confidence threshold")


class PublicationFacilityMapping(models.Model):
    """One facility a paper's acknowledgement names: a department/centre, or one equipment item.

    Department-level rows are the verified department-publication mapping. Equipment-level rows with enough evidence
    are sent to the OIC as one pending EquipmentPublicationClaim per paper (covering every matched equipment item);
    approval creates the EquipmentPublication rows as usual, which publication_dedupe counts once per paper."""

    evidence = models.ForeignKey(PublicationEvidence, on_delete=models.CASCADE, related_name="mappings")
    level = models.CharField(max_length=10, choices=MappingLevel.choices)
    department = models.ForeignKey("users.Department", on_delete=models.CASCADE, related_name="publication_mappings")
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.CASCADE, null=True, blank=True, related_name="publication_mappings"
    )
    matched_phrases = models.JSONField(default=list)
    rationale = models.TextField()
    confidence = models.DecimalField(max_digits=4, decimal_places=3)
    status = models.CharField(max_length=20, choices=MappingStatus.choices)
    claim = models.ForeignKey(
        "equipment.EquipmentPublicationClaim", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="facility_mappings",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["evidence_id", "level", "-confidence"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(level="DEPARTMENT", equipment__isnull=True)
                           | models.Q(level="EQUIPMENT", equipment__isnull=False)),
                name="impact_mapping_level_target",
            ),
            models.UniqueConstraint(fields=["evidence", "department"], condition=models.Q(level="DEPARTMENT"),
                                    name="impact_mapping_department_once"),
            models.UniqueConstraint(fields=["evidence", "equipment"], condition=models.Q(level="EQUIPMENT"),
                                    name="impact_mapping_equipment_once"),
        ]
