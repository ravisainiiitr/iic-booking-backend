# Equipment: waitlist_queue_depth + WaitlistEntry model

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0056_equipment_weekly_view_display"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="waitlist_queue_depth",
            field=models.PositiveIntegerField(
                blank=True,
                default=0,
                help_text="Maximum number of users in the waitlist for this equipment. When a booking attempt fails, the user is added to the waitlist and notified of their position. Set to 0 or leave empty to disable waitlist.",
                null=True,
                verbose_name="Waitlist queue depth",
            ),
        ),
        migrations.CreateModel(
            name="WaitlistEntry",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="waitlist_entries",
                        to="equipment.equipment",
                        help_text="Equipment this waitlist entry is for",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="waitlist_entries",
                        to=settings.AUTH_USER_MODEL,
                        help_text="User on the waitlist",
                    ),
                ),
            ],
            options={
                "verbose_name": "Waitlist entry",
                "verbose_name_plural": "Waitlist entries",
                "ordering": ["equipment", "created_at"],
                "unique_together": {("equipment", "user")},
            },
        ),
        migrations.AddIndex(
            model_name="waitlistentry",
            index=models.Index(fields=["equipment", "created_at"], name="equip_waitlist_equip_created_idx"),
        ),
    ]
