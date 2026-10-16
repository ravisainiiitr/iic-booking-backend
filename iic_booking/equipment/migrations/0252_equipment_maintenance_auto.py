# Additive only: two new tables and two nullable Booking columns for automatic maintenance handling.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0251_dedupe_booking_not_utilized_schedule"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EquipmentMaintenancePeriod",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("started_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("ended_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                (
                    "last_checked_on",
                    models.DateField(
                        blank=True, help_text="Last day whose slots and bookings the 08:30 check handled.", null=True
                    ),
                ),
                (
                    "auto_slot_ids",
                    models.JSONField(blank=True, default=list, help_text="Slots this mechanism set to Under Maintenance."),
                ),
                (
                    "adopted",
                    models.BooleanField(
                        default=False,
                        help_text="Created for equipment that was already Under Maintenance before this mechanism.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "disruption_event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="equipment.disruptionevent",
                    ),
                ),
                (
                    "ended_by",
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
                        related_name="maintenance_periods",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "started_by",
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
                "verbose_name": "Equipment maintenance period",
                "verbose_name_plural": "Equipment maintenance periods",
                "ordering": ["-started_at", "-id"],
                "indexes": [models.Index(fields=["equipment", "ended_at"], name="equip_maint_period_eq_end")],
            },
        ),
        migrations.CreateModel(
            name="MaintenanceNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("MARKED", "Equipment under maintenance: upcoming booking at risk"),
                            ("DAY", "Booking on a maintenance day: refund or reschedule"),
                            ("RESOLVED", "Equipment operational: booking stands"),
                            ("STAFF_MARKED", "Staff summary: marked under maintenance"),
                            ("STAFF_DAY", "Staff summary: daily check"),
                            ("STAFF_RESOLVED", "Staff summary: operational"),
                        ],
                        db_index=True,
                        max_length=20,
                    ),
                ),
                ("for_date", models.DateField(blank=True, null=True)),
                ("dedupe_key", models.CharField(max_length=120)),
                ("sent_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "booking",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="equipment.booking",
                    ),
                ),
                (
                    "period",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notifications",
                        to="equipment.equipmentmaintenanceperiod",
                    ),
                ),
                (
                    "user",
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
                "ordering": ["sent_at", "id"],
                "constraints": [
                    models.UniqueConstraint(fields=("period", "dedupe_key"), name="equip_maint_notif_period_key_uniq")
                ],
            },
        ),
        migrations.AddField(
            model_name="booking",
            name="results_paused_at",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "Set while the equipment is Under Maintenance: the results-overdue clock is paused and resumes "
                    "(deadline extended by the paused time) when the equipment is Operational again."
                ),
                null=True,
                verbose_name="Results overdue paused at",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="results_pause_period",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="paused_bookings",
                to="equipment.equipmentmaintenanceperiod",
                verbose_name="Results paused by maintenance period",
            ),
        ),
    ]
