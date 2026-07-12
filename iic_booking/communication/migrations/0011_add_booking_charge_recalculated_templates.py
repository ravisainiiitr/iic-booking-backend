# Add booking charge recalculated email and push templates

from django.db import migrations


def create_charge_recalculated_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if CommunicationTemplate.objects.filter(code="booking_charge_recalculated_email").exists():
        return

    CommunicationTemplate.objects.create(
        name="Booking charge recalculated (email)",
        code="booking_charge_recalculated_email",
        communication_type="email",
        subject="Booking charges updated - {{ equipment_name }} (Booking #{{ booking_id }})",
        body_text="""Hello {{ user_name }},

Your booking details were updated and the charges have been recalculated.

Booking ID: {{ booking_id }}
Equipment: {{ equipment_name }} ({{ equipment_code }})

Previous charge: ₹{{ previous_charge }}
New charge: ₹{{ new_charge }}

{{ comment }}

You can view your booking and wallet balance here: {{ link }}

Thank you for using IIC Booking.""",
        body_html="",
        description="Email sent to the user when booking input fields are edited and charges are recalculated (equipment with Enable charge recalculation).",
        variable_help="Variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ previous_charge }}, {{ new_charge }}, {{ amount_debited }}, {{ amount_credited }}, {{ comment }}, {{ link }}.",
        is_active=True,
    )

    if not CommunicationTemplate.objects.filter(code="booking_charge_recalculated_push").exists():
        CommunicationTemplate.objects.create(
            name="Booking charge recalculated (push)",
            code="booking_charge_recalculated_push",
            communication_type="push_notification",
            subject="Charges updated - Booking #{{ booking_id }}",
            body_text="Booking charges recalculated: ₹{{ new_charge }}. {{ comment }}",
            body_html="",
            description="Push notification when booking charges are recalculated after user input edit.",
            variable_help="Variables: {{ user_name }}, {{ booking_id }}, {{ equipment_name }}, {{ new_charge }}, {{ comment }}, {{ link }}.",
            is_active=True,
        )


def remove_charge_recalculated_templates(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="booking_charge_recalculated_email").delete()
    CommunicationTemplate.objects.filter(code="booking_charge_recalculated_push").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0010_add_bulk_email_template"),
    ]

    operations = [
        migrations.RunPython(create_charge_recalculated_templates, remove_charge_recalculated_templates),
    ]
