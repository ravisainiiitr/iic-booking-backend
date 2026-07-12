# Add waitlist email templates: unsuccessful + waitlist position, slots available

from django.db import migrations


def create_waitlist_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if not CommunicationTemplate.objects.filter(code="booking_unsuccessful_waitlist_email").exists():
        CommunicationTemplate.objects.create(
            name="Booking Unsuccessful – Added to Waitlist",
            code="booking_unsuccessful_waitlist_email",
            communication_type="email",
            subject="Booking Unsuccessful – You Are on the Waitlist for {{ equipment_name }}",
            body_text="""Hello {{ user_name }},

Your booking attempt for {{ equipment_name }} was unsuccessful.

{{ failure_reason }}

You have been added to the waitlist for this equipment.

Your position in the queue: {{ waitlist_position }}

When slots become available, you will receive an email notification. Slots are then allocated on a first-come, first-served basis. Please log in to the booking portal to complete your booking when you receive the notification.

— IIC Booking""",
            body_html="",
            description="Sent to the user when their booking fails and they are added to the equipment waitlist. Includes queue position. Editable in Admin Settings > Communication.",
            variable_help="Variables: {{ user_name }}, {{ user_email }}, {{ equipment_name }}, {{ equipment_code }}, {{ waitlist_position }}, {{ failure_reason }}.",
            is_active=True,
        )

    if not CommunicationTemplate.objects.filter(code="waitlist_slots_available_email").exists():
        CommunicationTemplate.objects.create(
            name="Waitlist – Slots Available",
            code="waitlist_slots_available_email",
            communication_type="email",
            subject="Slots Available for {{ equipment_name }} – Book Now",
            body_text="""Hello {{ user_name }},

Slots have become available for {{ equipment_name }} ({{ equipment_code }}).

You were on the waitlist for this equipment. Slots are allocated on a first-come, first-served basis. Please log in to the booking portal as soon as possible to secure your slot.

— IIC Booking""",
            body_html="",
            description="Sent to all users on the equipment waitlist when slots become available (e.g. after a cancellation, reschedule, or admin marking slots as available). Editable in Admin Settings > Communication.",
            variable_help="Variables: {{ user_name }}, {{ user_email }}, {{ equipment_name }}, {{ equipment_code }}.",
            is_active=True,
        )


def remove_waitlist_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(
        code__in=["booking_unsuccessful_waitlist_email", "waitlist_slots_available_email"]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0012_add_booking_not_utilized_templates"),
    ]

    operations = [
        migrations.RunPython(create_waitlist_templates, remove_waitlist_templates),
    ]
