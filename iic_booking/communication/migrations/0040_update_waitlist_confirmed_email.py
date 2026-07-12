# Waitlist → booking: subject "Waitlist Confirmed" + waitlist request date/time in body.

from django.db import migrations

CODE = "booking_waitlist_confirmed_email"

NEW_SUBJECT = "Waitlist Confirmed – {{ equipment_name }} ({{ virtual_booking_id }})"

NEW_BODY = """Hello {{ user_name }},

Booked for: {{ booked_for_user_name }} ({{ booked_for_user_email }})

Your waitlisted request has been confirmed. A slot became available and was assigned to you on a first-come, first-served basis.

Waitlist request date and time: {{ waitlist_joined_at_display }}
Queue position when you joined: {{ waitlist_position }}

Allocated slot details
Start / slot: {{ start_time }}
End: {{ end_time }}

Booking ID: {{ booking_id }}
Virtual booking number: {{ virtual_booking_id }}
Equipment: {{ equipment_name }} ({{ equipment_code }})
Total charge: ₹{{ total_charge }}
Wallet balance after booking: {{ wallet_balance_after }}

Open your booking: {{ link }}

If you did not expect this message, please contact the IIC booking desk.

— IIC Booking"""

NEW_HELP = (
    "Variables: {{ user_name }}, {{ user_email }}, {{ booked_for_user_name }}, {{ booked_for_user_email }}, "
    "{{ waitlist_joined_at_display }}, {{ waitlist_position }}, {{ booking_id }}, {{ virtual_booking_id }}, "
    "{{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ total_charge }}, "
    "{{ wallet_balance_after }}, {{ link }}."
)

OLD_SUBJECT = "Waitlist booking confirmed – {{ equipment_name }} ({{ virtual_booking_id }})"

OLD_BODY = """Hello {{ user_name }},

Booked for: {{ booked_for_user_name }} ({{ booked_for_user_email }})

Your waitlisted booking has been confirmed. A slot became available and was assigned to you on a first-come, first-served basis.

Waitlist position (when confirmed): {{ waitlist_position }}
Booking ID: {{ booking_id }}
Virtual booking number: {{ virtual_booking_id }}
Equipment: {{ equipment_name }} ({{ equipment_code }})
Start / slot details: {{ start_time }}
End: {{ end_time }}
Total charge: ₹{{ total_charge }}
Wallet balance after booking: {{ wallet_balance_after }}

Open your booking: {{ link }}

If you did not expect this message, please contact the IIC booking desk.

— IIC Booking"""

OLD_HELP = (
    "Variables: {{ user_name }}, {{ user_email }}, {{ booked_for_user_name }}, {{ booked_for_user_email }}, "
    "{{ waitlist_position }}, {{ booking_id }}, {{ virtual_booking_id }}, {{ equipment_name }}, {{ equipment_code }}, "
    "{{ start_time }}, {{ end_time }}, {{ total_charge }}, {{ wallet_balance_after }}, {{ link }}."
)


def forwards(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code=CODE).update(
        name="Waitlist Confirmed",
        subject=NEW_SUBJECT,
        body_text=NEW_BODY,
        variable_help=NEW_HELP,
    )


def backwards(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code=CODE).update(
        name="Booking Confirmed – From Waitlist",
        subject=OLD_SUBJECT,
        body_text=OLD_BODY,
        variable_help=OLD_HELP,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0039_add_wallet_credit_facility_activated_email"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
