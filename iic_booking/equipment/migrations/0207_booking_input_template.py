import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0206_equipment_auto_allocate_alternative_default"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BookingInputTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=80)),
                ("input_values", models.JSONField(blank=True, default=dict)),
                (
                    "options",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Booking options (e.g. auto-select slots, book any available slots, waitlist, alternate equipment).",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="booking_input_templates",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="booking_input_templates",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Booking input template",
                "verbose_name_plural": "Booking input templates",
                "ordering": ["name", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("user", "equipment", "name"),
                        name="uniq_booking_input_template_name",
                    )
                ],
            },
        ),
    ]
