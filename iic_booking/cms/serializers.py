"""Serializers for CMS models."""

from rest_framework import serializers
from .models import CmsPage, HallOfFrameEntry, HeroSlide, HomePageContent, MenuItem, ResearcherOfTheMonth, SiteDocument


class CmsPageSerializer(serializers.ModelSerializer):
    class Meta:
        model = CmsPage
        fields = ["id", "title", "slug", "content", "is_published", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class CmsPagePublicSerializer(serializers.ModelSerializer):
    """Public read-only: title, slug, content (for rendering)."""

    class Meta:
        model = CmsPage
        fields = ["id", "title", "slug", "content"]


class MenuItemListSerializer(serializers.ModelSerializer):
    """Flat list for admin list action (no nested children to avoid 500 / large payloads)."""
    page_slug = serializers.SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = [
            "id",
            "label",
            "link_type",
            "url",
            "document",
            "page",
            "page_slug",
            "parent",
            "priority",
            "is_active",
            "open_in_new_tab",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_page_slug(self, obj):
        return obj.page.slug if obj.page_id else None


class MenuItemSerializer(serializers.ModelSerializer):
    """Full serializer with nested children for retrieve/create/update."""
    children = serializers.SerializerMethodField()
    page_slug = serializers.SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = [
            "id",
            "label",
            "link_type",
            "url",
            "document",
            "page",
            "page_slug",
            "parent",
            "priority",
            "is_active",
            "open_in_new_tab",
            "children",
        ]
        read_only_fields = ["id"]

    def get_page_slug(self, obj):
        return obj.page.slug if obj.page_id else None

    def get_children(self, obj):
        if not obj.pk:
            return []
        if not obj.children.filter(is_active=True).exists():
            return []
        qs = obj.children.filter(is_active=True).order_by("priority", "id")
        return MenuItemListSerializer(qs, many=True, context=self.context).data


class MenuItemTreeSerializer(serializers.ModelSerializer):
    """For public API: root items with nested children; includes document_url, page_slug when applicable."""

    children = serializers.SerializerMethodField()
    document_url = serializers.SerializerMethodField()
    page_slug = serializers.SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = ["id", "label", "link_type", "url", "document_url", "page_slug", "priority", "open_in_new_tab", "children"]

    def get_document_url(self, obj):
        if obj and obj.link_type == "document" and obj.document:
            request = self.context.get("request")
            if request:
                return request.build_absolute_uri(obj.document.url)
            return obj.document.url
        return None

    def get_page_slug(self, obj):
        return obj.page.slug if obj.page_id else None

    def get_children(self, obj):
        qs = obj.children.filter(is_active=True).order_by("priority", "id")
        return MenuItemTreeSerializer(qs, many=True, context=self.context).data


class HomePageContentSerializer(serializers.ModelSerializer):
    class Meta:
        model = HomePageContent
        fields = ["id", "key", "value", "font_size", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class HeroSlideSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = HeroSlide
        fields = ["id", "order", "alt_text", "is_active", "image", "image_url", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at", "image_url"]

    def get_image_url(self, obj):
        if not obj or not obj.image:
            return None
        url = obj.image.url
        request = self.context.get("request")
        if request and url.startswith("/"):
            base = request.build_absolute_uri("/").rstrip("/")
            return base + url
        return url


class SiteDocumentSerializer(serializers.ModelSerializer):
    document_url = serializers.SerializerMethodField()

    class Meta:
        model = SiteDocument
        fields = ["id", "key", "title", "document", "document_url", "created_at", "updated_at"]
        read_only_fields = ["id", "key", "created_at", "updated_at", "document_url"]

    def get_document_url(self, obj):
        if not obj or not obj.document:
            return None
        url = obj.document.url
        request = self.context.get("request")
        if request and url.startswith("/"):
            base = request.build_absolute_uri("/").rstrip("/")
            return base + url
        return url


# --- Hall of Frames and Researcher of the Month -----------------------------------------------------------------------

def _absolute(request, field) -> str | None:
    if not field:
        return None
    url = field.url
    if request and url.startswith("/"):
        return request.build_absolute_uri("/").rstrip("/") + url
    return url


def _facility_label(obj) -> str:
    eq = getattr(obj, "equipment", None)
    parts = [eq.name if eq else "", (obj.facility or "").strip()]
    return " · ".join(p for p in parts if p)


class RecognitionPublicSerializer(serializers.Serializer):
    """Public view of an approved Hall of Frames entry or Researcher of the Month."""

    id = serializers.IntegerField()
    researcher_name = serializers.CharField()
    photo_url = serializers.SerializerMethodField()
    affiliation = serializers.CharField()
    publication_title = serializers.CharField()
    research_summary = serializers.CharField()
    journal = serializers.CharField()
    year = serializers.IntegerField(allow_null=True)
    publication_url = serializers.CharField()
    facility = serializers.SerializerMethodField()
    equipment_id = serializers.IntegerField(allow_null=True)

    def get_photo_url(self, obj):
        return _absolute(self.context.get("request"), obj.photo)

    def get_facility(self, obj):
        return _facility_label(obj)


class ResearcherOfTheMonthPublicSerializer(RecognitionPublicSerializer):
    month = serializers.DateField()
    department_id = serializers.IntegerField(allow_null=True)
    department_name = serializers.SerializerMethodField()

    def get_department_name(self, obj):
        return obj.department.name if obj.department_id else "Institute-wide"


class _RecognitionAdminSerializer(serializers.ModelSerializer):
    """Admin CRUD. Photos are resized on upload; approval is set by the view (Main Administrator only)."""

    photo_url = serializers.SerializerMethodField()
    facility_label = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()

    COMMON = [
        "id", "researcher_name", "photo", "photo_url", "affiliation", "publication_title", "research_summary",
        "journal", "year", "publication_url", "equipment", "facility", "facility_label", "is_approved",
        "approved_by_name", "approved_at", "created_at", "updated_at",
    ]
    READ_ONLY = ["id", "photo_url", "facility_label", "approved_by_name", "approved_at", "created_at", "updated_at"]

    def get_photo_url(self, obj):
        return _absolute(self.context.get("request"), obj.photo)

    def get_facility_label(self, obj):
        return _facility_label(obj)

    def get_approved_by_name(self, obj):
        u = obj.approved_by
        return (u.name or u.email) if u else ""

    def validate_photo(self, value):
        if not value:
            return value
        from .images import optimised_portrait

        try:
            return optimised_portrait(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_year(self, value):
        if value is not None and not (1900 <= int(value) <= 2100):
            raise serializers.ValidationError("Enter a year between 1900 and 2100.")
        return value


class HallOfFrameEntryAdminSerializer(_RecognitionAdminSerializer):
    class Meta:
        model = HallOfFrameEntry
        fields = _RecognitionAdminSerializer.COMMON + ["publication", "order"]
        read_only_fields = _RecognitionAdminSerializer.READ_ONLY


class ResearcherOfTheMonthAdminSerializer(_RecognitionAdminSerializer):
    department_name = serializers.SerializerMethodField()

    class Meta:
        model = ResearcherOfTheMonth
        fields = _RecognitionAdminSerializer.COMMON + ["month", "department", "department_name"]
        read_only_fields = _RecognitionAdminSerializer.READ_ONLY
        validators = []  # one per department and month: checked in validate() with a clearer message

    def get_department_name(self, obj):
        return obj.department.name if obj.department_id else "Institute-wide"

    def validate_month(self, value):
        return value.replace(day=1)

    def validate(self, attrs):
        month = attrs.get("month", getattr(self.instance, "month", None))
        dept = attrs["department"] if "department" in attrs else getattr(self.instance, "department", None)
        own = self.context.get("own_department_only")  # Department Administrators: their department only
        if own is not None and getattr(dept, "pk", None) != own:
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied("You can feature researchers of your own department only.")
        if month:
            clash = ResearcherOfTheMonth.objects.filter(month=month.replace(day=1), department=dept)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"month": "This month already has a Researcher of the Month for this department."}
                )
        return attrs
