from django.db import migrations, models
from django.utils import timezone


def seed_booking_opens_at(apps, schema_editor):
    PortalMigrationState = apps.get_model("users", "PortalMigrationState")
    # 4 October 2026 00:00 Asia/Kolkata (+05:30)
    opens = timezone.datetime(2026, 10, 4, 0, 0, 0, tzinfo=timezone.get_fixed_timezone(330))
    state, _ = PortalMigrationState.objects.get_or_create(
        singleton_key="default",
        defaults={
            "end_user_booking_enabled": False,
            "booking_opens_at": opens,
        },
    )
    update_fields = []
    if state.booking_opens_at is None:
        state.booking_opens_at = opens
        update_fields.append("booking_opens_at")
    if state.end_user_booking_enabled:
        state.end_user_booking_enabled = False
        update_fields.append("end_user_booking_enabled")
    if update_fields:
        state.save(update_fields=update_fields)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0105_legacy_equipment_capacity_split"),
    ]

    operations = [
        migrations.AddField(
            model_name="department",
            name="equipment_booking_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Main-administrator master switch: when disabled, equipment belonging to this "
                    "department cannot be booked on the portal. Defaults to disabled."
                ),
                verbose_name="Equipment booking enabled",
            ),
        ),
        migrations.RunPython(seed_booking_opens_at, noop_reverse),
    ]
