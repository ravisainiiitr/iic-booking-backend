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


class SiteDocument(models.Model):
    """
    Named public documents (e.g. Analysis Charges PDF) linked from the home page.
    One row per key; admins replace the file via the Content Management panel.
    """

    class Key(models.TextChoices):
        ANALYSIS_CHARGES = "analysis_charges", _("Analysis Charges")

    key = models.CharField(
        _("Key"),
        max_length=64,
        unique=True,
        choices=Key.choices,
        db_index=True,
    )
    title = models.CharField(_("Title"), max_length=200, blank=True)
    document = models.FileField(
        _("Document"),
        upload_to="cms/site_documents/%Y/%m/",
        blank=True,
        null=True,
        help_text=_("PDF shown when visitors click the matching home-page button."),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        verbose_name = _("Site document")
        verbose_name_plural = _("Site documents")

    def __str__(self):
        return self.title or self.get_key_display()


class _ResearchRecognition(models.Model):
    """Fields shared by Hall of Frames entries and Researchers of the Month: who, what was published, and which IIC
    facility the publication acknowledges. Nothing appears on the home page until a Main Administrator approves it."""

    researcher_name = models.CharField(_("Researcher name"), max_length=200)
    photo = models.ImageField(
        _("Photo"), upload_to="cms/researchers/%Y/%m/", blank=True,
        help_text=_("Portrait, resized to at most 640 px on upload."),
    )
    affiliation = models.CharField(_("Department / institution"), max_length=255, blank=True)
    publication_title = models.CharField(_("Publication title"), max_length=500, blank=True)
    research_summary = models.TextField(_("Brief research description"), blank=True)
    journal = models.CharField(_("Journal / conference"), max_length=300, blank=True)
    year = models.PositiveSmallIntegerField(_("Publication year"), null=True, blank=True)
    publication_url = models.URLField(_("Publication link (DOI or URL)"), max_length=500, blank=True)
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name=_("Acknowledged equipment"),
    )
    facility = models.CharField(
        _("Acknowledged facility"), max_length=300, blank=True,
        help_text=_("Shown when no equipment is chosen, or in addition to it (e.g. 'IIC FE-SEM facility')."),
    )
    is_approved = models.BooleanField(_("Approved"), default=False)
    approved_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def __str__(self):
        return self.researcher_name


class HallOfFrameEntry(_ResearchRecognition):
    """Researchers who acknowledged IIT Roorkee facilities (IIC or a department hosted on the portal) in a publication, shown in the home page carousel (approved only,
    by ``order``)."""

    publication = models.ForeignKey(
        "equipment.EquipmentPublication", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        verbose_name=_("Linked equipment publication"),
    )
    order = models.PositiveIntegerField(_("Order"), default=0, help_text=_("Lower number = shown first."))

    class Meta:
        ordering = ["order", "-year", "id"]
        verbose_name = _("Hall of Frames entry")
        verbose_name_plural = _("Hall of Frames entries")


class ResearcherOfTheMonth(_ResearchRecognition):
    """The researcher featured for one month, per department (or institute-wide when no department). The home page
    shows the current month's approved entries; earlier months stay as history."""

    month = models.DateField(_("Month"), help_text=_("Any day of the month; stored as its first day."))
    department = models.ForeignKey(
        "users.Department", on_delete=models.CASCADE, null=True, blank=True, related_name="+",
        verbose_name=_("Department"), help_text=_("Leave empty for an institute-wide Researcher of the Month."),
    )
    created_by = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    user = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text=_("The researcher, when chosen automatically from approved publications."),
    )
    publication_count = models.PositiveIntegerField(default=0, help_text=_("Approved publications that earned it."))
    auto = models.BooleanField(default=False, help_text=_("Chosen automatically from approved publications."))

    class Meta:
        ordering = ["-month", "department__name", "id"]
        verbose_name = _("Researcher of the Month")
        verbose_name_plural = _("Researchers of the Month")
        constraints = [
            models.UniqueConstraint(fields=["month", "department"], name="cms_rotm_one_per_department_month"),
            models.UniqueConstraint(fields=["month"], condition=models.Q(department__isnull=True),
                                    name="cms_rotm_one_institute_wide_per_month"),
        ]

    def save(self, *args, **kwargs):
        if self.month:
            self.month = self.month.replace(day=1)
        super().save(*args, **kwargs)


class RecognitionHide(models.Model):
    """The Main Administrator took a researcher off the home page: from the Hall of Frames (scope hall, until
    shown again), or as one month's Researcher of the Month for a department (scope month: the next eligible
    researcher is chosen instead). Kept as the record of who did it and why."""

    class Scope(models.TextChoices):
        HALL = "hall", _("Hall of Frames")
        MONTH = "month", _("Researcher of the Month")

    scope = models.CharField(max_length=8, choices=Scope.choices)
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="+")
    month = models.DateField(null=True, blank=True)
    department = models.ForeignKey("users.Department", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    reason = models.CharField(max_length=300, blank=True, default="")
    created_by = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Hidden researcher")
        verbose_name_plural = _("Hidden researchers")
