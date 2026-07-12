# Email template when a waitlisted user is auto-confirmed (FCFS).

from django.db import migrations


def add_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    if CommunicationTemplate.objects.filter(code="booking_waitlist_confirmed_email").exists():
        return
    CommunicationTemplate.objects.create(
        name="Booking Confirmed – From Waitlist",
        code="booking_waitlist_confirmed_email",
        communication_type="email",
        subject="Waitlist booking confirmed – {{ equipment_name }} ({{ virtual_booking_id }})",
        body_text="""Hello {{ user_name }},

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

— IIC Booking""",
        body_html="",
        description="Sent to the booking user and to the wallet owner (when different) when a waitlist entry is auto-confirmed and the wallet is debited.",
        variable_help=(
            "Variables: {{ user_name }}, {{ user_email }}, {{ booked_for_user_name }}, {{ booked_for_user_email }}, "
            "{{ waitlist_position }}, {{ booking_id }}, {{ virtual_booking_id }}, {{ equipment_name }}, {{ equipment_code }}, "
            "{{ start_time }}, {{ end_time }}, {{ total_charge }}, {{ wallet_balance_after }}, {{ link }}."
        ),
        is_active=True,
    )


def remove_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="booking_waitlist_confirmed_email").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0025_communicationlog_recipient_email_and_more"),
    ]

    operations = [
        migrations.RunPython(add_template, remove_template),
    ]
