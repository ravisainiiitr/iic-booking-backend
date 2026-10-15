"""AI and automation: admin switches, the automation action log and support ticket triage suggestions.

Every automation that sends a message or changes a record is off until the Main Administrator turns it on, can be
previewed as a dry run, and writes one ``AutomationActionLog`` row per action (sent, skipped, assigned, ...).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class AutomationSettings(models.Model):
    """Single row (pk=1) of AI and automation switches."""

    instrument_finder_enabled = models.BooleanField(
        _("Instrument finder"),
        default=True,
        help_text=_("Plain-language instrument suggestions (read-only; sends nothing)."),
    )
    instrument_finder_llm_enabled = models.BooleanField(
        _("Instrument finder: ask the AI model"),
        default=False,
        help_text=_("When the rules find no technique, ask the configured AI model to classify the request."),
    )
    llm_daily_call_limit = models.PositiveIntegerField(
        _("AI model calls per day (automation)"),
        default=200,
        help_text=_("Hard daily cap for AI model calls made by these automations. 0 = never call the model."),
    )
    smart_slots_enabled = models.BooleanField(
        _("Best slots for you"),
        default=True,
        help_text=_("Suggest fitting slots on the booking page (read-only; the user still confirms)."),
    )
    ticket_triage_enabled = models.BooleanField(
        _("Support ticket triage suggestions"),
        default=False,
        help_text=_("Categorise new tickets and suggest who should handle them and a reply draft. Staff approve; nothing is sent."),
    )
    ticket_auto_assign_enabled = models.BooleanField(
        _("Assign new tickets automatically"),
        default=False,
        help_text=_("Assign a new ticket to the suggested person when triage is confident. Needs triage on."),
    )
    oic_alerts_enabled = models.BooleanField(
        _("Weekly anomaly alerts to Officers in Charge"),
        default=False,
        help_text=_("Monday 09:15 email: utilisation drop, cancellation spike, repeated disruptions."),
    )
    oic_alert_utilisation_drop_pct = models.PositiveSmallIntegerField(
        _("Utilisation drop threshold (%)"), default=40
    )
    oic_alert_min_cancellations = models.PositiveSmallIntegerField(_("Cancellations in a week to alert"), default=3)
    oic_alert_min_disruptions = models.PositiveSmallIntegerField(_("Disruptions in a week to alert"), default=2)
    no_show_prompts_enabled = models.BooleanField(
        _("Likely no-show prompts to operators"),
        default=False,
        help_text=_("Daily 17:30 email to lab operators listing today's ended bookings with no sample recorded."),
    )
    no_show_grace_minutes = models.PositiveSmallIntegerField(_("Minutes after slot end before prompting"), default=60)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name = _("AI and automation settings")
        verbose_name_plural = _("AI and automation settings")

    def __str__(self) -> str:
        return "AI and automation settings"

    @classmethod
    def get_singleton(cls) -> "AutomationSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class AutomationKind(models.TextChoices):
    INSTRUMENT_FINDER_LLM = "instrument_finder_llm", _("Instrument finder AI call")
    TICKET_TRIAGE = "ticket_triage", _("Ticket triage")
    TICKET_ASSIGN = "ticket_assign", _("Ticket assignment")
    OIC_ALERT = "oic_alert", _("Weekly anomaly alert")
    NO_SHOW_PROMPT = "no_show_prompt", _("No-show prompt")
    SETTINGS = "settings", _("Settings change")


class AutomationAction(models.TextChoices):
    SUGGESTED = "suggested", _("Suggested")
    SENT = "sent", _("Sent")
    SKIPPED = "skipped", _("Skipped")
    ASSIGNED = "assigned", _("Assigned")
    ACCEPTED = "accepted", _("Accepted")
    DISMISSED = "dismissed", _("Dismissed")
    CHANGED = "changed", _("Changed")
    CALLED = "called", _("Called")
    FAILED = "failed", _("Failed")


class AutomationActionLog(models.Model):
    """Append-only audit trail of every automation action (and every switch change)."""

    kind = models.CharField(_("Automation"), max_length=32, choices=AutomationKind.choices, db_index=True)
    action = models.CharField(_("Action"), max_length=16, choices=AutomationAction.choices)
    target_type = models.CharField(_("Target type"), max_length=32, blank=True, default="")
    target_id = models.CharField(_("Target"), max_length=64, blank=True, default="", db_index=True)
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    summary = models.CharField(_("Summary"), max_length=255, blank=True, default="")
    detail = models.JSONField(_("Detail"), default=dict, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("Automation action")
        verbose_name_plural = _("Automation actions")
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["kind", "target_type", "target_id"], name="automation_target_idx")]

    def __str__(self) -> str:
        return f"{self.kind} {self.action} {self.target_type}:{self.target_id}"


class TicketTriage(models.Model):
    """Suggested category, handler and reply draft for one support ticket. Staff accept or dismiss it."""

    class Status(models.TextChoices):
        SUGGESTED = "suggested", _("Suggested")
        ACCEPTED = "accepted", _("Accepted")
        DISMISSED = "dismissed", _("Dismissed")

    ticket = models.OneToOneField("support.Ticket", on_delete=models.CASCADE, related_name="triage")
    category = models.CharField(_("Suggested type"), max_length=50, blank=True, default="")
    topic = models.CharField(_("Topic"), max_length=40, blank=True, default="")
    priority = models.CharField(_("Suggested priority"), max_length=20, blank=True, default="")
    route_role = models.CharField(_("Route to"), max_length=20, blank=True, default="")
    suggested_assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    confidence = models.FloatField(_("Confidence"), default=0)
    reasons = models.JSONField(_("Reasons"), default=list, blank=True)
    reply_draft = models.TextField(_("Reply draft"), blank=True, default="")
    draft_sources = models.JSONField(_("Draft sources"), default=list, blank=True)
    status = models.CharField(_("Status"), max_length=16, choices=Status.choices, default=Status.SUGGESTED)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(_("Decided at"), null=True, blank=True)
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Ticket triage")
        verbose_name_plural = _("Ticket triage")

    def __str__(self) -> str:
        return f"Ticket {self.ticket_id}: {self.category} -> {self.route_role}"
