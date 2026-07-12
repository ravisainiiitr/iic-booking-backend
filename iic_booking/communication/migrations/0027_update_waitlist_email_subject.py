from django.db import migrations


def update_waitlist_subject(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="booking_unsuccessful_waitlist_email").update(
        subject="Booking Waitlisted – {{ equipment_name }} (Queue: {{ waitlist_position }})"
    )


def revert_waitlist_subject(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="booking_unsuccessful_waitlist_email").update(
        subject="Booking Unsuccessful – You Are on the Waitlist for {{ equipment_name }}"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0026_add_booking_waitlist_confirmed_email"),
    ]

    operations = [
        migrations.RunPython(update_waitlist_subject, revert_waitlist_subject),
    ]
