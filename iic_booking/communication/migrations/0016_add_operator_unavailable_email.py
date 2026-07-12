# Add Operator Unavailable email template (full refund issued to user)

from django.db import migrations


def create_operator_unavailable_template(apps, schema_editor):
    """Create email template for Operator Unavailable (full refund issued)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if not CommunicationTemplate.objects.filter(code="operator_unavailable_email").exists():
        CommunicationTemplate.objects.create(
            name="Operator Unavailable – Full Refund",
            code="operator_unavailable_email",
            communication_type="email",
            subject="Operator Unavailable – Booking Refunded ({{ equipment_name }})",
            body_text="""Hello {{ user_name }},

Due to operator unavailability, your booking could not be completed.

Booking details:
- Booking ID: {{ booking_id }}
- Equipment: {{ equipment_name }} ({{ equipment_code }})
- Start Time: {{ start_time }}
- End Time: {{ end_time }}

A full refund of ₹{{ refund_amount }} has been issued to your wallet.

{{ comment }}

If you have any questions, please contact the facility.

— IIC Booking""",
            body_html="",
            description="Sent to the user when a booking is marked as Operator Unavailable. A full refund is issued to the wallet. Editable in Admin Settings > Communication.",
            variable_help="Variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ refund_amount }}, {{ comment }}.",
            is_active=True,
        )


def remove_operator_unavailable_template(apps, schema_editor):
    """Remove Operator Unavailable template (reverse migration)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="operator_unavailable_email").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0015_add_oic_monthly_report_template"),
    ]

    operations = [
        migrations.RunPython(create_operator_unavailable_template, remove_operator_unavailable_template),
    ]
