# Generated for Phase 8C migration notification batch + T0 audit.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0102_legacy_equipment_booking_bridge"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MigrationNotificationBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("dry_run", models.BooleanField(default=False)),
                ("status", models.CharField(db_index=True, default="DRAFT", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("counts", models.JSONField(blank=True, default=dict)),
                ("notes", models.TextField(blank=True, default="")),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="migration_notification_batches_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "migration_batch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="notification_batches",
                        to="users.legacybookingmigrationbatch",
                    ),
                ),
            ],
            options={
                "verbose_name": "Migration notification batch",
                "verbose_name_plural": "Migration notification batches",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="MigrationNotificationRecipient",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("recipient_email", models.EmailField(max_length=254)),
                ("role", models.CharField(blank=True, default="", max_length=32)),
                (
                    "template",
                    models.CharField(
                        choices=[
                            ("FACULTY_MIGRATION", "Faculty migration"),
                            ("STUDENT_MIGRATION", "Student migration"),
                            ("OIC_MIGRATION", "OIC migration"),
                            ("ADMIN_MIGRATION", "Main Administrator migration"),
                        ],
                        db_index=True,
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("QUEUED", "Queued"),
                            ("SENT", "Sent"),
                            ("FAILED", "Failed"),
                            ("SKIPPED", "Skipped"),
                        ],
                        db_index=True,
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("queued_at", models.DateTimeField(blank=True, null=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("retry_count", models.PositiveSmallIntegerField(default=0)),
                ("failure_reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recipients",
                        to="users.migrationnotificationbatch",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="migration_notifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Migration notification recipient",
                "verbose_name_plural": "Migration notification recipients",
            },
        ),
        migrations.CreateModel(
            name="MigrationT0Event",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("environment", models.CharField(default="STAGING", max_length=32)),
                ("t0_at", models.DateTimeField(blank=True, null=True)),
                ("booking_migration_mode", models.CharField(blank=True, default="", max_length=16)),
                ("steps", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("notes", models.TextField(blank=True, default="")),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="migration_t0_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "migration_batch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="t0_events",
                        to="users.legacybookingmigrationbatch",
                    ),
                ),
                (
                    "notification_batch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="t0_events",
                        to="users.migrationnotificationbatch",
                    ),
                ),
            ],
            options={
                "verbose_name": "Migration T0 event",
                "verbose_name_plural": "Migration T0 events",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="migrationnotificationrecipient",
            index=models.Index(fields=["status", "template"], name="users_migr_status_8c_idx"),
        ),
        migrations.AddConstraint(
            model_name="migrationnotificationrecipient",
            constraint=models.UniqueConstraint(fields=("batch", "user"), name="uniq_migration_notification_batch_user"),
        ),
        migrations.AddConstraint(
            model_name="migrationnotificationrecipient",
            constraint=models.UniqueConstraint(
                fields=("batch", "recipient_email", "template"),
                name="uniq_migration_notification_batch_email_template",
            ),
        ),
    ]
