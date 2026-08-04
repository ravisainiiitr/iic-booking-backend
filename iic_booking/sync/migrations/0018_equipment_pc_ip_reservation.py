# Generated manually — optional Portal mirror of DSA soft IP reservations

import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("sync", "0017_equipment_sync_template"),
        ("equipment", "0184_equipment_analysis_checkin_policy"),
    ]

    operations = [
        migrations.CreateModel(
            name="EquipmentPcIpReservation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("computer_name", models.CharField(blank=True, default="", max_length=200, verbose_name="Computer name")),
                ("mac_address", models.CharField(db_index=True, max_length=32, unique=True, verbose_name="MAC address")),
                (
                    "preferred_ip",
                    models.GenericIPAddressField(blank=True, null=True, protocol="IPv4", verbose_name="Preferred IP"),
                ),
                (
                    "observed_ip",
                    models.GenericIPAddressField(blank=True, null=True, protocol="IPv4", verbose_name="Observed IP"),
                ),
                (
                    "network_mode",
                    models.CharField(
                        default="dhcp",
                        help_text="dhcp (default) or static (apply only when policy allows).",
                        max_length=16,
                        verbose_name="Network mode",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("released", "Released"),
                            ("conflict", "Conflict"),
                        ],
                        default="active",
                        max_length=16,
                    ),
                ),
                ("last_seen", models.DateTimeField(blank=True, null=True, verbose_name="Last seen")),
                ("notes", models.TextField(blank=True, default="", verbose_name="Notes")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                (
                    "equipment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pc_ip_reservations",
                        to="equipment.equipment",
                        verbose_name="Equipment",
                    ),
                ),
            ],
            options={
                "verbose_name": "Equipment PC IP reservation",
                "verbose_name_plural": "Equipment PC IP reservations",
                "ordering": ["-updated_at"],
            },
        ),
    ]
