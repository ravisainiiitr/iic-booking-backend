"""
Create the "booking will be rescheduled automatically" update email template when missing, without running the
global ``sync_default_email_templates`` (which would overwrite admin-customised templates). Existing rows are never
touched.
"""

from django.db import migrations

CODES = ("booking_equipment_maintenance_hold_update_email",)


def create_missing(apps, schema_editor):
    from iic_booking.communication.default_email_templates import get_default_email_template

    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    for code in CODES:
        if CommunicationTemplate.objects.filter(code=code, communication_type="email").exists():
            continue
        spec = get_default_email_template(code)
        if spec is None:
            continue
        if CommunicationTemplate.objects.filter(name=spec["name"]).exists():
            continue
        CommunicationTemplate.objects.create(
            code=code,
            communication_type="email",
            name=spec["name"],
            subject=spec["subject"],
            body_text=spec["body_text"],
            body_html=spec["body_html"],
            description=spec["description"],
            variable_help=spec["variable_help"],
            is_active=True,
        )


def remove_untouched(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code__in=CODES, communication_type="email", updated_by__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("communication", "0062_maintenance_auto_reschedule_email_templates"),
    ]

    operations = [
        migrations.RunPython(create_missing, remove_untouched),
    ]
