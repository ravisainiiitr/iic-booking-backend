import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone

TASK_NAME = "Expire OIC substitute delegations (every 10 minutes)"


def mark_past_delegations_expired(apps, schema_editor):
    """Delegations already past resume_at become history quietly, so the job does not notify for them."""
    EquipmentTemporaryOIC = apps.get_model("equipment", "EquipmentTemporaryOIC")
    for row in EquipmentTemporaryOIC.objects.filter(status="active", resume_at__lte=timezone.now()).only(
        "pk", "resume_at"
    ):
        EquipmentTemporaryOIC.objects.filter(pk=row.pk).update(status="expired", ended_at=row.resume_at)


def create_expiry_schedule(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    every_10_minutes, _ = IntervalSchedule.objects.get_or_create(every=10, period="minutes")
    if not PeriodicTask.objects.filter(name=TASK_NAME).exists():
        PeriodicTask.objects.create(
            name=TASK_NAME,
            task="equipment.expire_oic_substitutions",
            interval=every_10_minutes,
            enabled=True,
        )


def remove_expiry_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=TASK_NAME).delete()


class Migration(migrations.Migration):
    """OIC Substitute: start date, reason, status and end details on temporary OIC delegations, an audit
    event table and the 10-minute expiry job. Additive only; existing delegations keep working."""

    dependencies = [
        ("equipment", "0230_fabrication_rejection_workflow"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipmenttemporaryoic",
            name="batch_id",
            field=models.UUIDField(
                blank=True,
                db_index=True,
                help_text="Shared by the rows created in one request (several substitutes).",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="equipmenttemporaryoic",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="equipmenttemporaryoic",
            name="end_reason",
            field=models.TextField(blank=True, db_default="", default=""),
        ),
        migrations.AddField(
            model_name="equipmenttemporaryoic",
            name="ended_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="equipmenttemporaryoic",
            name="ended_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="equipmenttemporaryoic",
            name="reason",
            field=models.TextField(blank=True, db_default="", default=""),
        ),
        migrations.AddField(
            model_name="equipmenttemporaryoic",
            name="start_at",
            field=models.DateTimeField(
                blank=True, help_text="Access starts at this time; empty means from creation.", null=True
            ),
        ),
        migrations.AddField(
            model_name="equipmenttemporaryoic",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "Active or scheduled"),
                    ("cancelled", "Cancelled before start"),
                    ("revoked", "Revoked early"),
                    ("expired", "Expired"),
                ],
                db_default="active",
                db_index=True,
                default="active",
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="EquipmentTemporaryOICEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("created", "Created"),
                            ("period_changed", "Period changed"),
                            ("cancelled", "Cancelled"),
                            ("revoked", "Revoked"),
                            ("expired", "Expired"),
                        ],
                        max_length=20,
                    ),
                ),
                ("reason", models.TextField(blank=True, default="")),
                ("details", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "delegation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="equipment.equipmenttemporaryoic",
                    ),
                ),
            ],
            options={
                "verbose_name": "Temporary OIC delegation event",
                "verbose_name_plural": "Temporary OIC delegation events",
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.RunPython(mark_past_delegations_expired, migrations.RunPython.noop),
        migrations.RunPython(create_expiry_schedule, remove_expiry_schedule),
    ]
