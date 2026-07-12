from django.db import migrations


WAITLIST_BODY = """Hello {{ user_name }},

Your booking attempt for {{ equipment_name }} was unsuccessful.

{{ failure_reason }}

You have been added to the waitlist for this equipment.

Request raised at: {{ waitlist_requested_at }}
Your position in the queue: {{ waitlist_position }}

When slots become available, you will receive an email notification. Slots are then allocated on a first-come, first-served basis. Please log in to the booking portal to complete your booking when you receive the notification.

— IIC Booking"""


PREVIOUS_BODY = """Hello {{ user_name }},

Your booking attempt for {{ equipment_name }} was unsuccessful.

{{ failure_reason }}

You have been added to the waitlist for this equipment.

Your position in the queue: {{ waitlist_position }}

When slots become available, you will receive an email notification. Slots are then allocated on a first-come, first-served basis. Please log in to the booking portal to complete your booking when you receive the notification.

— IIC Booking"""


def forwards(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="booking_unsuccessful_waitlist_email").update(
        body_text=WAITLIST_BODY,
        variable_help=(
            "Variables: {{ user_name }}, {{ user_email }}, {{ equipment_name }}, {{ equipment_code }}, "
            "{{ waitlist_position }}, {{ waitlist_requested_at }}, {{ failure_reason }}."
        ),
    )


def backwards(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="booking_unsuccessful_waitlist_email").update(
        body_text=PREVIOUS_BODY,
        variable_help=(
            "Variables: {{ user_name }}, {{ user_email }}, {{ equipment_name }}, {{ equipment_code }}, "
            "{{ waitlist_position }}, {{ failure_reason }}."
        ),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0027_update_waitlist_email_subject"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
