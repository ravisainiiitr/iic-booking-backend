# Replace single status HOLD_AT_OFFICE with HELD_AT_OFFICE / FORWARDED_TO_LAB (data migration)

from django.db import migrations


def update_hold_to_held(apps, schema_editor):
    BookingSampleTrace = apps.get_model("equipment", "BookingSampleTrace")
    BookingSampleTrace.objects.filter(status="HOLD_AT_OFFICE").update(status="HELD_AT_OFFICE")


def reverse_held_to_hold(apps, schema_editor):
    BookingSampleTrace = apps.get_model("equipment", "BookingSampleTrace")
    BookingSampleTrace.objects.filter(status="HELD_AT_OFFICE").update(status="HOLD_AT_OFFICE")


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0036_bookingsampletrace_reason"),
    ]

    operations = [
        migrations.RunPython(update_hold_to_held, reverse_held_to_hold),
    ]
