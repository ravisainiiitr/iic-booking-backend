"""CMS models for menu and home page content."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class MenuItem(models.Model):
    """
    Navigation menu item. Supports hierarchy via parent (for submenus).
    Order controlled by priority (lower = first).
    """

    class LinkType(models.TextChoices):
        INTERNAL_ANCHOR = "internal_anchor", _("Internal anchor (#section)")
        INTERNAL_ROUTE = "internal_route", _("Internal route (/path)")
        EXTERNAL_URL = "external_url", _("External URL")
        TRIGGER = "trigger", _("Trigger (e.g. Contact / Ticket form)")
        DOCUMENT = "document", _("Document (PDF upload)")
        PAGE = "page", _("CMS page")

    label = models.CharField(_("Label"), max_length=100)
    link_type = models.CharField(
        _("Link type"),
        max_length=20,
        choices=LinkType.choices,
        default=LinkType.INTERNAL_ANCHOR,
    )
    url = models.CharField(
        _("URL or anchor"),
        max_length=500,
        blank=True,
        help_text=_("Anchor (#section), path (/equipments), or full URL. Leave blank for trigger or document."),
    )
    document = models.FileField(
        _("Document (PDF)"),
        upload_to="cms/menu_documents/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("Upload a PDF or other document. Used when Link type is 'Document (PDF upload)'."),
    )
    page = models.ForeignKey(
        "CmsPage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="menu_items",
        help_text=_("CMS page to link to. Used when Link type is 'CMS page'."),
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="children",
        help_text=_("Set for submenu items."),
    )
    priority = models.PositiveIntegerField(
        _("Priority"),
        default=0,
        help_text=_("Lower number = higher position in menu."),
    )
    is_active = models.BooleanField(_("Active"), default=True)
    open_in_new_tab = models.BooleanField(_("Open in new tab"), default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "id"]
        verbose_name = _("Menu item")
        verbose_name_plural = _("Menu items")

    def __str__(self):
        return self.label


class HomePageContent(models.Model):
    """
    Key-value content for the main page (hero, stats, CTAs).
    Single row per key; use get_or_create for defaults.
    """

    key = models.CharField(_("Key"), max_length=100, unique=True, db_index=True)
    value = models.TextField(_("Value"), blank=True)
    font_size = models.CharField(
        _("Font size"),
        max_length=30,
        blank=True,
        null=True,
        help_text=_("Optional CSS font size for this text (e.g. 16px, 1.2rem, 120%). Leave blank for default."),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        verbose_name = _("Home page content")
        verbose_name_plural = _("Home page content")

    def __str__(self):
        return self.key


class CmsPage(models.Model):
    """
    CMS page with block-based content. Link from menu items via link_type 'page'.
    Content is a JSON list of blocks: heading, paragraph, image, list, quote, divider.
    """

    title = models.CharField(_("Title"), max_length=200)
    slug = models.SlugField(_("Slug"), max_length=200, unique=True)
    content = models.JSONField(
        _("Content blocks"),
        default=list,
        blank=True,
        help_text=_("List of blocks: heading, paragraph, image, list, quote, divider."),
    )
    is_published = models.BooleanField(_("Published"), default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]
        verbose_name = _("CMS page")
        verbose_name_plural = _("CMS pages")

    def __str__(self):
        return self.title


class HeroSlide(models.Model):
    """
    Hero section background image for the main page carousel.
    Order by 'order'; carousel uses autoscroll.
    """

    image = models.ImageField(
        _("Image"),
        upload_to="cms/hero/%Y/%m/",
        help_text=_("Background image for hero carousel. Recommended: landscape, high resolution."),
    )
    alt_text = models.CharField(
        _("Alt text"),
        max_length=255,
        blank=True,
        help_text=_("Short description for accessibility (e.g. 'Laboratory equipment')."),
    )
    order = models.PositiveIntegerField(
        _("Order"),
        default=0,
        help_text=_("Lower number = shown first. Use to control slide order."),
    )
    is_active = models.BooleanField(_("Active"), default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name = _("Hero background image")
        verbose_name_plural = _("Hero background images")

    def __str__(self):
        return self.alt_text or f"Hero slide {self.order}"
