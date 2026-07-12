from django.db import migrations


CODE = "waitlist_short_notice_slot_available_email"


NEW_BODY = """Dear {{ user_name }},

This is to inform you that a slot for the {{ equipment_name }} has become available at short notice (within the next {{ lead_hours }} hours). As the available time is limited, the system will not automatically allocate this slot from the waiting list.

All users currently on the waiting list are being notified of this availability. If your samples will be ready and you wish to utilize this slot, you may log in to the booking portal and reserve the slot on a first-come, first-served basis.

Slot Details:
Equipment Name: {{ equipment_name }}
Date: {{ slot_date }}
Time: {{ slot_time }}

Important: Since this is a short-notice allocation, users who book this slot are expected to utilize it. Cancellation or no-show after booking will not be permitted, and such cases may attract applicable user charges.

Please ensure that your samples are fully prepared and all necessary requirements are completed before booking.

If you are unable to commit to the slot, kindly refrain from booking so that other waitlisted users may utilize the opportunity.

For any queries, please contact {{ contact_line }}.

Booking portal: {{ link }}

Best regards,
Institute Instrumentation Centre
Indian Institute of Technology Roorkee
"""


def forwards(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code=CODE).update(body_text=NEW_BODY)


def backwards(apps, schema_editor):
    # Keep backwards as no-op (template may have been edited in admin UI).
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0041_add_waitlist_short_notice_slot_available_email"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

