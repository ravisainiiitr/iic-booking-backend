import math

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def working_days_from_timer_hours(absent_hours, unavailable_hours):
    """Old timers were clock hours after the slot; N calendar days ~ ceil(N * 5 / 7) working days, never earlier."""
    hours = absent_hours or unavailable_hours or 0
    if hours <= 0:
        return 0
    calendar_days = math.ceil(hours / 24)
    return max(1, math.ceil(calendar_days * 5 / 7))


def initialise_results_deadline(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    rows = Equipment.objects.values_list(
        "equipment_id",
        "operator_absent_disruption_after_booking_end_hours",
        "operator_unavailable_after_booking_end_hours",
    )
    for equipment_id, absent_hours, unavailable_hours in rows:
        Equipment.objects.filter(equipment_id=equipment_id).update(
            results_deadline_value=working_days_from_timer_hours(absent_hours, unavailable_hours),
            results_deadline_unit="WORKING_DAYS",
        )


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0221_dynamicinputfield_table_config"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="results_deadline_value",
            field=models.PositiveSmallIntegerField(
                default=2,
                help_text=(
                    "How long after the last slot ends the laboratory shares the results, in the unit below. "
                    "Working days skip Saturdays, Sundays and institute holidays; the deadline is the end of the last "
                    "working day. Bookings still open after it are listed as Results overdue for the Lab Operators and "
                    "the Officer In-Charge and, when the results-deadline automation is on, enter the Operator Absent "
                    "disruption flow (refund or reschedule choice). Set to 0 for no results deadline."
                ),
                validators=[django.core.validators.MaxValueValidator(720)],
                verbose_name="Results deadline",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="results_deadline_unit",
            field=models.CharField(
                choices=[("WORKING_DAYS", "Working days"), ("HOURS", "Hours")],
                default="WORKING_DAYS",
                help_text="Working days (default) or clock hours after the slot, for fast instruments.",
                max_length=16,
                verbose_name="Results deadline unit",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="show_results_deadline_to_users",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When on, the sample submission policy and the booking details tell users when to expect results "
                    '("Results expected by <date>"). Off by default: users see the generic wording only.'
                ),
                verbose_name="Show results deadline to users",
            ),
        ),
        migrations.CreateModel(
            name="ResultsDeadlinePolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "automation_enabled",
                    models.BooleanField(
                        default=False,
                        help_text=(
                            "When on, bookings whose slot ends after the switch-on time enter the Operator Absent "
                            "disruption flow once their results deadline passes (instead of the old fixed-hour timers)."
                        ),
                        verbose_name="Results-deadline automation enabled",
                    ),
                ),
                (
                    "automation_since",
                    models.DateTimeField(
                        blank=True,
                        help_text="Set automatically when the automation is switched on. Earlier bookings keep the old timers.",
                        null=True,
                        verbose_name="Applies to slots ending from",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
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
                "verbose_name": "Results deadline policy",
                "verbose_name_plural": "Results deadline policy",
            },
        ),
        migrations.RunPython(initialise_results_deadline, migrations.RunPython.noop),
    ]
