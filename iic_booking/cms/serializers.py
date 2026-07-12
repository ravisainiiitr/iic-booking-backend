"""Serializers for CMS models."""

from rest_framework import serializers
from .models import MenuItem, HomePageContent, HeroSlide, CmsPage


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
