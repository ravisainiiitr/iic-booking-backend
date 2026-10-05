import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0226_multimode_data_cleanup"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RecurringSlotBlockRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "weekdays",
                    models.JSONField(default=list, help_text="Weekdays of the slot's local date: 0 = Monday … 6 = Sunday."),
                ),
                (
                    "slot_times",
                    models.JSONField(
                        default=list,
                        help_text='Local slot start times "HH:MM", picked from the equipment\'s Slot Masters.',
                    ),
                ),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                (
                    "label",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Stored in DailySlot.blocked_label of every slot this rule blocks.",
                        max_length=255,
                    ),
                ),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                (
                    "summary",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Result when the rule was created: blocked count and skipped slots (booked / other status).",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("removed_at", models.DateTimeField(blank=True, null=True)),
                ("removal_summary", models.JSONField(blank=True, default=dict)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recurring_slot_block_rules",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "removed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Recurring slot block rule",
                "verbose_name_plural": "Recurring slot block rules",
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["equipment", "is_active"], name="equip_rsbr_equipment_active")],
            },
        ),
        migrations.CreateModel(
            name="RecurringSlotBlockRuleSlot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("CREATED", "Blocked when the rule was created"),
                            ("GENERATED", "Blocked when the slot was generated"),
                            ("SHARED", "Already blocked by another repeat rule"),
                        ],
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "daily_slot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recurring_block_links",
                        to="equipment.dailyslot",
                    ),
                ),
                (
                    "rule",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="slot_links",
                        to="equipment.recurringslotblockrule",
                    ),
                ),
            ],
            options={
                "verbose_name": "Recurring slot block rule slot",
                "verbose_name_plural": "Recurring slot block rule slots",
                "constraints": [
                    models.UniqueConstraint(fields=("rule", "daily_slot"), name="uniq_recurring_block_rule_slot"),
                ],
            },
        ),
    ]
