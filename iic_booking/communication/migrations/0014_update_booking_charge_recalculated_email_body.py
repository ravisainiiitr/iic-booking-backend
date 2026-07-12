# Update booking charge recalculated email template with breakup and refund/extra action

from django.db import migrations


def update_charge_recalculated_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    t = CommunicationTemplate.objects.filter(code="booking_charge_recalculated_email").first()
    if not t:
        return
    t.body_text = """Hello {{ user_name }},

Your booking details were updated and the charges have been recalculated.

Booking ID: {{ booking_id }}
Equipment: {{ equipment_name }} ({{ equipment_code }})

Previous charge: ₹{{ previous_charge }}
New charge: ₹{{ new_charge }}

Charge breakdown:
{{ charge_breakdown_text }}

{{ comment }}

If a refund is due, please click the Refund button in the booking details to credit the amount to your wallet.
If an extra amount is due, please click Pay Now to debit the amount from your wallet.

You can view your booking and wallet balance here: {{ link }}

Thank you for using IIC Booking."""
    t.variable_help = "Variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, {{ equipment_code }}, {{ previous_charge }}, {{ new_charge }}, {{ charge_breakdown_text }}, {{ refund_amount }}, {{ extra_amount }}, {{ comment }}, {{ link }}."
    t.save()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0013_add_waitlist_email_templates"),
    ]

    operations = [
        migrations.RunPython(update_charge_recalculated_template, noop_reverse),
    ]
