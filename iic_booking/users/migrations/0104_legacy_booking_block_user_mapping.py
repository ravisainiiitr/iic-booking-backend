# Phase 10D — legacy booking block user/equipment metadata (occupancy independent of user mapping)

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0103_migration_notification_batch"),
    ]

    operations = [
        migrations.AddField(
            model_name="legacybookingblock",
            name="duration_minutes",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="legacybookingblock",
            name="legacy_employee_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="legacybookingblock",
            name="legacy_equipment_id",
            field=models.PositiveBigIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="legacybookingblock",
            name="legacy_user_id",
            field=models.PositiveBigIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="legacybookingblock",
            name="resolved_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="resolved_legacy_booking_blocks",
                to="users.user",
            ),
        ),
        migrations.AddField(
            model_name="legacybookingblock",
            name="source_status",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="legacybookingblock",
            name="user_mapping_source",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="legacybookingblock",
            name="user_mapping_status",
            field=models.CharField(
                choices=[
                    ("UNRESOLVED", "Unresolved"),
                    ("RESOLVED_CHANNEL_I", "Resolved via Channel-I"),
                    ("NOT_REQUIRED_FOR_BLOCK", "Not required for block"),
                ],
                db_index=True,
                default="NOT_REQUIRED_FOR_BLOCK",
                max_length=32,
            ),
        ),
    ]
