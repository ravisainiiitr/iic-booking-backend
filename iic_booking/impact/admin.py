from django.contrib import admin

from .models import (
    AcknowledgementCheck,
    AcknowledgementPolicy,
    EquipmentFunding,
    FacilityAlias,
    PublicationEvidence,
    PublicationFacilityMapping,
    PublicationSuggestion,
    ResearcherAuthorCandidate,
    ResearcherOrcid,
)


@admin.register(EquipmentFunding)
class EquipmentFundingAdmin(admin.ModelAdmin):
    list_display = ("equipment", "funder", "scheme", "sanction_reference", "amount", "is_active")
    list_filter = ("funder", "is_active")
    raw_id_fields = ("equipment", "created_by")


admin.site.register(AcknowledgementPolicy)
admin.site.register(AcknowledgementCheck)
admin.site.register(ResearcherOrcid)
admin.site.register(PublicationSuggestion)


@admin.register(FacilityAlias)
class FacilityAliasAdmin(admin.ModelAdmin):
    list_display = ("phrase", "department", "equipment", "source", "case_sensitive", "is_active")
    list_filter = ("source", "is_active", "case_sensitive")
    search_fields = ("phrase",)
    raw_id_fields = ("department", "equipment", "created_by")


@admin.register(ResearcherAuthorCandidate)
class ResearcherAuthorCandidateAdmin(admin.ModelAdmin):
    list_display = ("user", "display_name", "openalex_author_id", "confidence", "status")
    list_filter = ("status",)
    raw_id_fields = ("user", "decided_by")


class MappingInline(admin.TabularInline):
    model = PublicationFacilityMapping
    extra = 0
    raw_id_fields = ("department", "equipment", "claim")


@admin.register(PublicationEvidence)
class PublicationEvidenceAdmin(admin.ModelAdmin):
    list_display = ("title", "doi", "user", "status", "checked_at")
    list_filter = ("status",)
    search_fields = ("title", "doi")
    raw_id_fields = ("user", "suggestion")
    inlines = [MappingInline]
