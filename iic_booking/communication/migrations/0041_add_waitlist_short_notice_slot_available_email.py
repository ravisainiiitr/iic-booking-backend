from django.db import migrations


def add_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    if CommunicationTemplate.objects.filter(code="waitlist_short_notice_slot_available_email").exists():
        return
    CommunicationTemplate.objects.create(
        name="Waitlist — slot available (short notice)",
        code="waitlist_short_notice_slot_available_email",
        communication_type="email",
        subject="{{ equipment_name }} Slot Available – Short Notice Booking Opportunity",
        body_text="""Dear {{ user_name }},

This is to inform you that a slot for the {{ equipment_name }} has become available at short notice (within the next {{ lead_hours }} hours). As the available time is limited, the system will not automatically allocate this slot from the waiting list.

All users currently on the waiting list are being notified of this availability. If your samples will be ready and you wish to utilize this slot, you may log in to the booking portal and reserve the slot on a first-come, first-served basis.

Slot Details:
Equipment Name: {{ equipment_name }}
Date: {{ slot_date }}
Time: {{ slot_time }}

Important: Since this is a short-notice allocation, users who book this slot are expected to utilize it. Cancellation or no-show after booking will not be permitted, and such cases may attract applicable user charges/penalties as per facility policy.

Please ensure that your samples are fully prepared and all necessary requirements are completed before booking.

If you are unable to commit to the slot, kindly refrain from booking so that other waitlisted users may utilize the opportunity.

For any queries, please contact {{ contact_line }}.

Booking portal: {{ link }}

Best regards,
Institute Instrumentation Centre
Indian Institute of Technology Roorkee
""",
        body_html="",
        description=(
            "Sent to all ACTIVE waitlist users when a slot becomes available inside reschedule_hours_threshold; "
            "auto FCFS allocation is skipped and users may book manually."
        ),
        variable_help=(
            "Variables: {{ user_name }}, {{ user_email }}, {{ equipment_name }}, {{ lead_hours }}, "
            "{{ slot_date }}, {{ slot_time }}, {{ contact_line }}, {{ link }}."
        ),
        is_active=True,
    )


def remove_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="waitlist_short_notice_slot_available_email").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0040_update_waitlist_confirmed_email"),
    ]

    operations = [
        migrations.RunPython(add_template, remove_template),
    ]

