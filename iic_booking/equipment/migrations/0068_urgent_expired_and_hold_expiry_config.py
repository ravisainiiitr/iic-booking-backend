# Add EXPIRED status for urgent requests and UrgentHoldExpiryConfig

from django.db import migrations, models


def _urgent_status_choices():
    return [
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("EXPIRED", "Expired"),
    ]


def create_default_expiry_config(apps, schema_editor):
    UrgentHoldExpiryConfig = apps.get_model("equipment", "UrgentHoldExpiryConfig")
    if not UrgentHoldExpiryConfig.objects.exists():
        UrgentHoldExpiryConfig.objects.create(hold_expiry_hours=24)


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0067_add_hold_status_and_urgent_hold_booking"),
    ]

    operations = [
        migrations.AlterField(
            model_name="urgentbookingrequest",
            name="status",
            field=models.CharField(
                choices=_urgent_status_choices(),
                default="PENDING",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="UrgentHoldExpiryConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "hold_expiry_hours",
                    models.PositiveIntegerField(
                        default=24,
                        help_text="After this many hours from request creation, PENDING requests with a hold booking are auto-expired (hold released, slots freed). Set to 0 to disable.",
                        verbose_name="Hold expiry (hours)",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Urgent hold expiry config",
                "verbose_name_plural": "Urgent hold expiry configs",
                "ordering": ["pk"],
            },
        ),
        migrations.RunPython(create_default_expiry_config, migrations.RunPython.noop),
    ]
