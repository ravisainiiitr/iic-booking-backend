"""Recurring slot block rules (models). Imported at the end of ``iic_booking.equipment.models``."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class RecurringSlotBlockRule(models.Model):
    """OIC / Main Admin rule: block AVAILABLE slots on chosen weekdays and slot times within a date range."""

    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.CASCADE,
        related_name="recurring_slot_block_rules",
    )
    weekdays = models.JSONField(
        default=list,
        help_text=_("Weekdays of the slot's local date: 0 = Monday … 6 = Sunday."),
    )
    slot_times = models.JSONField(
        default=list,
        help_text=_('Local slot start times "HH:MM", picked from the equipment\'s Slot Masters.'),
    )
    start_date = models.DateField()
    end_date = models.DateField()
    label = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=_("Stored in DailySlot.blocked_label of every slot this rule blocks."),
    )
    is_active = models.BooleanField(default=True, db_index=True)
    summary = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Result when the rule was created: blocked count and skipped slots (booked / other status)."),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    removed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    removed_at = models.DateTimeField(null=True, blank=True)
    removal_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Recurring slot block rule")
        verbose_name_plural = _("Recurring slot block rules")
        indexes = [models.Index(fields=["equipment", "is_active"], name="equip_rsbr_equipment_active")]

    def __str__(self):
        return f"Repeat block {self.pk} on {self.equipment_id}: {self.start_date}..{self.end_date}"


class RecurringSlotBlockRuleSlot(models.Model):
    """A slot a rule blocked (or shares with another rule). Removing a rule only unblocks slots linked here."""

    class Source(models.TextChoices):
        CREATED = "CREATED", _("Blocked when the rule was created")
        GENERATED = "GENERATED", _("Blocked when the slot was generated")
        SHARED = "SHARED", _("Already blocked by another repeat rule")

    rule = models.ForeignKey(RecurringSlotBlockRule, on_delete=models.CASCADE, related_name="slot_links")
    daily_slot = models.ForeignKey(
        "equipment.DailySlot",
        on_delete=models.CASCADE,
        related_name="recurring_block_links",
    )
    source = models.CharField(max_length=16, choices=Source.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Recurring slot block rule slot")
        verbose_name_plural = _("Recurring slot block rule slots")
        constraints = [
            models.UniqueConstraint(fields=["rule", "daily_slot"], name="uniq_recurring_block_rule_slot"),
        ]

    def __str__(self):
        return f"Rule {self.rule_id} -> slot {self.daily_slot_id} ({self.source})"
