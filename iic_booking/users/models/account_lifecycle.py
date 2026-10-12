"""Programme / employment end date of IITR students, post-docs, research associates and IITR Startups.

``AccountLifecycle`` is one row per user in scope, recomputed from the profile (degree, joining and graduation
dates, the end date given at registration) and approved extensions. Suspension is derived from it at request
time and only when ``ACCOUNT_END_DATE_ENFORCEMENT`` is on; it never touches ``is_active`` so a suspended user
can still sign in. A separate table keeps every ``User`` query working before the migration runs.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class LifecycleCategory(models.TextChoices):
    IITR_STUDENT = "iitr_student", _("IITR Student")
    IITR_POSTDOC = "iitr_postdoc", _("IITR Post Doctoral Fellow")
    IITR_RESEARCH_ASSOCIATE = "iitr_research_associate", _("IITR Research Associate in Projects")
    IITR_STARTUP = "iitr_startup", _("IITR Startup")


class DegreeCode(models.TextChoices):
    PHD = "phd", _("Ph.D. - Doctor of Philosophy")
    MSC = "msc", _("M.Sc. - Master of Science")
    MTECH = "mtech", _("M.Tech. - Master of Technology")
    INT_MTECH = "int_mtech", _("Int. M.Tech - Integrated Master of Technology")
    BTECH = "btech", _("B.Tech. - Bachelor of Technology")
    OTHER = "other", _("Other programme")


class EndDateSource(models.TextChoices):
    GRADUATION_DATE = "graduation_date", _("Graduation date")
    DERIVED_FROM_JOINING = "derived_from_joining", _("Derived from the joining date")
    USER_PROVIDED = "user_provided", _("Given by the user at registration")
    EXTENDED = "extended", _("Extended by the supervisor")
    MISSING = "missing", _("Not available")


class AccountLifecycle(models.Model):
    user = models.OneToOneField(
        "users.User", on_delete=models.CASCADE, related_name="account_lifecycle", verbose_name=_("User")
    )
    category = models.CharField(_("Category"), max_length=32, choices=LifecycleCategory.choices, db_index=True)
    degree_name = models.CharField(_("Degree name"), max_length=255, blank=True, default="")
    degree_code = models.CharField(_("Degree (normalised)"), max_length=16, choices=DegreeCode.choices, blank=True, default="")
    joining_date = models.DateField(_("Joining date used"), null=True, blank=True)
    base_end_date = models.DateField(
        _("End date before extensions"), null=True, blank=True, help_text=_("Graduation date, joining date + duration, or the date given at registration.")
    )
    extended_until = models.DateField(_("Extended until"), null=True, blank=True)
    programme_end_date = models.DateField(_("Programme / employment end date"), null=True, blank=True, db_index=True)
    end_date_source = models.CharField(
        _("End date source"), max_length=24, choices=EndDateSource.choices, default=EndDateSource.MISSING
    )
    suspended_at = models.DateTimeField(
        _("Suspended at"), null=True, blank=True, help_text=_("When the daily run recorded the suspension (enforcement on).")
    )
    suspended_for_end_date = models.DateField(_("Suspended for end date"), null=True, blank=True)
    reminders_sent = models.JSONField(
        _("Reminders sent"), default=dict, blank=True, help_text=_('{"<end date>": [30, 7, 1]} — reminders already sent for that end date.')
    )
    computed_at = models.DateTimeField(_("Computed at"), null=True, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Account lifecycle")
        verbose_name_plural = _("Account lifecycles")

    def __str__(self) -> str:
        return f"Lifecycle user={self.user_id} end={self.programme_end_date} ({self.end_date_source})"


class AccountLifecycleEvent(models.Model):
    class Action(models.TextChoices):
        SUSPENDED = "suspended", _("Suspended at programme end")
        RESUMED = "resumed", _("Resumed after extension")
        REMINDER_SENT = "reminder_sent", _("End date reminder sent")
        EXTENSION_REQUESTED = "extension_requested", _("Extension requested")
        EXTENDED = "extended", _("Extended")
        END_DATE_PROVIDED = "end_date_provided", _("End date provided by the user")
        WALLET_AUTO_LINKED = "wallet_auto_linked", _("Wallet linked automatically")
        WALLET_AUTO_LINK_SKIPPED = "wallet_link_skipped", _("Automatic wallet link skipped")
        WRITE_REFUSED = "write_refused", _("Action refused while suspended")

    user = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name=_("User")
    )
    action = models.CharField(_("Action"), max_length=32, choices=Action.choices, db_index=True)
    actor = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+", verbose_name=_("Actor")
    )
    actor_role = models.CharField(_("Actor role"), max_length=24, blank=True, default="")
    channel = models.CharField(_("Channel"), max_length=16, blank=True, default="")
    details = models.JSONField(_("Details"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Account lifecycle event")
        verbose_name_plural = _("Account lifecycle events")
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.action} user={self.user_id}"
