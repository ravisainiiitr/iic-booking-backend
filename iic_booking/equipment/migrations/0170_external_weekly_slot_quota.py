from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models
import django.db.models.deletion


def clear_reserved_for_external(apps, schema_editor):
    DailySlot = apps.get_model("equipment", "DailySlot")
    DailySlot.objects.filter(reserved_for_external=True).update(reserved_for_external=False)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0169_sample_submission_deadline_reminder_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="external_slot_quota_percent",
            field=models.IntegerField(
                default=0,
                help_text=(
                    "Maximum share of a week's bookable slots that external users may consume (0–100). "
                    "0 means external users cannot book. The weekly limit is snapshotted 15 minutes before "
                    "the external booking window opens and is not recalculated if schedules change later. "
                    "Configurable by Main Administrator and Department Administrator."
                ),
                validators=[MinValueValidator(0), MaxValueValidator(100)],
                verbose_name="External Slot Quota (%)",
            ),
        ),
        migrations.AlterField(
            model_name="dailyslot",
            name="reserved_for_external",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Deprecated. External booking is limited by Equipment.external_slot_quota_percent "
                    "and weekly snapshots; this flag is no longer enforced and should remain False."
                ),
                verbose_name="Reserved for External Users (deprecated)",
            ),
        ),
        migrations.CreateModel(
            name="ExternalWeeklySlotQuotaSnapshot",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "week_start",
                    models.DateField(help_text="Monday of the booking week (Mon–Sun)."),
                ),
                (
                    "week_end",
                    models.DateField(help_text="Sunday of the booking week."),
                ),
                (
                    "total_bookable_slots",
                    models.PositiveIntegerField(
                        help_text="Count of AVAILABLE + BOOKED slots in the week at snapshot time."
                    ),
                ),
                (
                    "external_quota_percent",
                    models.PositiveSmallIntegerField(
                        help_text=(
                            "Equipment external_slot_quota_percent copied at snapshot time (0–100)."
                        )
                    ),
                ),
                (
                    "max_external_slots",
                    models.PositiveIntegerField(
                        help_text="floor(total_bookable_slots * external_quota_percent / 100)."
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="external_weekly_slot_quota_snapshots",
                        to="equipment.equipment",
                    ),
                ),
            ],
            options={
                "verbose_name": "External Weekly Slot Quota Snapshot",
                "verbose_name_plural": "External Weekly Slot Quota Snapshots",
                "ordering": ["-week_start", "equipment_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="externalweeklyslotquotasnapshot",
            constraint=models.UniqueConstraint(
                fields=("equipment", "week_start"),
                name="uniq_ext_weekly_slot_quota_equip_week",
            ),
        ),
        migrations.AddIndex(
            model_name="externalweeklyslotquotasnapshot",
            index=models.Index(
                fields=["equipment", "week_start"],
                name="equip_ext_quota_week_idx",
            ),
        ),
        migrations.RunPython(clear_reserved_for_external, noop_reverse),
    ]
