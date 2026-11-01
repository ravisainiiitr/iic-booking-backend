"""Django admin for CMS (optional; main management is via frontend admin)."""

from django.contrib import admin
from django.utils.html import format_html
from .models import CmsPage, HallOfFrameEntry, HeroSlide, HomePageContent, MenuItem, ResearcherOfTheMonth, SiteDocument


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ["label", "link_type", "url_or_document", "parent", "priority", "is_active"]
    list_filter = ["is_active", "link_type"]
    search_fields = ["label", "url"]
    ordering = ["priority", "id"]

    def url_or_document(self, obj):
        if obj.link_type == "document" and obj.document:
            return format_html('<a href="{}" target="_blank">Document</a>', obj.document.url)
        if obj.link_type == "page" and obj.page:
            return format_html("Page: {}", obj.page.title)
        return obj.url or "—"

    url_or_document.short_description = "URL / Document"


@admin.register(CmsPage)
class CmsPageAdmin(admin.ModelAdmin):
    list_display = ["title", "slug", "is_published", "updated_at"]
    list_filter = ["is_published"]
    search_fields = ["title", "slug"]
    prepopulated_fields = {"slug": ("title",)}
    ordering = ["title"]


@admin.register(HomePageContent)
class HomePageContentAdmin(admin.ModelAdmin):
    list_display = ["key", "value_preview", "updated_at"]
    list_display_links = ["key"]
    search_fields = ["key", "value"]
    ordering = ["key"]

    def value_preview(self, obj):
        if obj is None:
            return ""
        raw = obj.value if obj.value is not None else ""
        if len(raw) > 80:
            return raw[:80] + "..."
        return raw

    value_preview.short_description = "Value"


@admin.register(HeroSlide)
class HeroSlideAdmin(admin.ModelAdmin):
    list_display = ["order", "image_preview", "alt_text", "is_active", "updated_at"]
    list_display_links = ["alt_text", "image_preview"]
    list_editable = ["order", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["alt_text"]
    ordering = ["order", "id"]

    def image_preview(self, obj):
        if obj and obj.image:
            return format_html(
                '<img src="{}" style="max-height: 48px; max-width: 120px; object-fit: cover;" alt="{}" />',
                obj.image.url,
                obj.alt_text or "Hero slide",
            )
        return "—"

    image_preview.short_description = "Preview"


@admin.register(SiteDocument)
class SiteDocumentAdmin(admin.ModelAdmin):
    list_display = ["key", "title", "document_link", "updated_at"]
    list_display_links = ["key", "title"]
    search_fields = ["key", "title"]
    ordering = ["key"]
    readonly_fields = ["created_at", "updated_at"]

    def document_link(self, obj):
        if obj and obj.document:
            return format_html('<a href="{}" target="_blank">Open PDF</a>', obj.document.url)
        return "—"

    document_link.short_description = "Document"


@admin.register(HallOfFrameEntry)
class HallOfFrameEntryAdmin(admin.ModelAdmin):
    list_display = ["order", "researcher_name", "affiliation", "year", "is_approved", "updated_at"]
    list_filter = ["is_approved", "year"]
    search_fields = ["researcher_name", "affiliation", "publication_title", "journal"]
    raw_id_fields = ["equipment", "publication", "approved_by"]
    readonly_fields = ["approved_by", "approved_at", "created_at", "updated_at"]


@admin.register(ResearcherOfTheMonth)
class ResearcherOfTheMonthAdmin(admin.ModelAdmin):
    list_display = ["month", "department", "researcher_name", "is_approved", "updated_at"]
    list_filter = ["is_approved", "department"]
    search_fields = ["researcher_name", "affiliation", "publication_title"]
    raw_id_fields = ["equipment", "department", "approved_by", "created_by"]
    readonly_fields = ["approved_by", "approved_at", "created_by", "created_at", "updated_at"]
    date_hierarchy = "month"
