# Generated manually to add Booking Not Utilized email templates.

from django.db import migrations


def create_booking_not_utilized_templates(apps, schema_editor):
    """Create email templates for Booking Not Utilized (user + Supervisor)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if not CommunicationTemplate.objects.filter(code="booking_not_utilized_email").exists():
        CommunicationTemplate.objects.create(
            name="Booking Not Utilized (User)",
            code="booking_not_utilized_email",
            communication_type="email",
            subject="Booking Not Utilized – IIC Booking",
            body_text="""Hello {{ user_name }},

Your booking (ID: {{ booking_id }}) for {{ equipment_name }} on {{ slot_details }} has been marked as "Booking Not Utilized" because the slot was not used.

No refund will be issued for this booking. Please ensure optimum utilization of facility resources in future.

If you have any questions, please contact the facility.

— IIC Booking""",
            body_html="",
            description="Sent to the user when their booked slot is marked as Booking Not Utilized. No refund. Editable in Admin Settings > Communication.",
            variable_help="Variables: {{ user_name }}, {{ user_email }}, {{ equipment_name }}, {{ slot_details }}, {{ booking_id }}.",
            is_active=True,
        )

    if not CommunicationTemplate.objects.filter(code="booking_not_utilized_wallet_owner_email").exists():
        CommunicationTemplate.objects.create(
            name="Booking Not Utilized – Supervisor",
            code="booking_not_utilized_wallet_owner_email",
            communication_type="email",
            subject="Booking Not Utilized – Student/Wallet User Notice",
            body_text="""Hello {{ wallet_owner_name }},

A booking made using your wallet has been marked as "Booking Not Utilized."

Student/User: {{ student_name }} ({{ student_email }})
Equipment: {{ equipment_name }}
Slot: {{ slot_details }}
Booking ID: {{ booking_id }}

No refund has been issued. Please advise the student to utilize booked slots in future for optimum facility use.

— IIC Booking""",
            body_html="",
            description="Sent to the Supervisor when a student's booking is marked as Booking Not Utilized. For issuing appropriate warning. Editable in Admin Settings > Communication.",
            variable_help="Variables: {{ wallet_owner_name }}, {{ student_name }}, {{ student_email }}, {{ equipment_name }}, {{ slot_details }}, {{ booking_id }}. (wallet_owner_name is the Supervisor.)",
            is_active=True,
        )


def remove_booking_not_utilized_templates(apps, schema_editor):
    """Remove Booking Not Utilized templates (reverse migration)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(
        code__in=["booking_not_utilized_email", "booking_not_utilized_wallet_owner_email"]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0011_add_booking_charge_recalculated_templates"),
    ]

    operations = [
        migrations.RunPython(create_booking_not_utilized_templates, remove_booking_not_utilized_templates),
    ]
