"""
Switch off the "Booking Charges Updated" email (booking_charge_recalculated_email).

The charge-recalculated booking event and its in-app notice are unaffected. Re-enable by setting
the template active again (Django admin / communication templates); reversing this migration does the same.
"""

from django.db import migrations

CODE = "booking_charge_recalculated_email"


def _set_active(apps, active):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code=CODE, communication_type="email").update(is_active=active)


def forwards(apps, schema_editor):
    _set_active(apps, False)


def backwards(apps, schema_editor):
    _set_active(apps, True)


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0055_charge_recalculated_intro_production_digest"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
