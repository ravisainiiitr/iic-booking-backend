from django.contrib import admin

from .models import AcknowledgementCheck, AcknowledgementPolicy, EquipmentFunding, PublicationSuggestion, ResearcherOrcid


@admin.register(EquipmentFunding)
class EquipmentFundingAdmin(admin.ModelAdmin):
    list_display = ("equipment", "funder", "scheme", "sanction_reference", "amount", "is_active")
    list_filter = ("funder", "is_active")
    raw_id_fields = ("equipment", "created_by")


admin.site.register(AcknowledgementPolicy)
admin.site.register(AcknowledgementCheck)
admin.site.register(ResearcherOrcid)
admin.site.register(PublicationSuggestion)
