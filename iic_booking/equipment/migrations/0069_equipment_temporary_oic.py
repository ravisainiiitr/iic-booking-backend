# Temporary OIC delegation: primary OIC can assign another OIC to manage equipment until resume_at

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0068_urgent_expired_and_hold_expiry_config"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EquipmentTemporaryOIC",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "resume_at",
                    models.DateTimeField(
                        help_text="Date and time after which the primary OIC resumes and the temporary OIC loses access."
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="temporary_oic_delegations",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "primary_oic",
                    models.ForeignKey(
                        help_text="The OIC who is on leave and has delegated management.",
                        limit_choices_to={"user_type": "manager"},
                        on_delete=models.deletion.CASCADE,
                        related_name="temporary_oic_delegations_as_primary",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "temporary_oic",
                    models.ForeignKey(
                        help_text="The OIC who will temporarily manage the equipment until resume_at.",
                        limit_choices_to={"user_type": "manager"},
                        on_delete=models.deletion.CASCADE,
                        related_name="temporary_oic_delegations_as_temp",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Temporary OIC delegation",
                "verbose_name_plural": "Temporary OIC delegations",
                "ordering": ["resume_at"],
            },
        ),
    ]
