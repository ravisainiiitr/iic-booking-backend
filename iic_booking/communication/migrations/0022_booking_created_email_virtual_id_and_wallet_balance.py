# Add virtual_booking_id and wallet_balance_after to booking confirmation email

from django.db import migrations


def update_booking_created_email(apps, schema_editor):
    """Add virtual_booking_id and wallet_balance_after to booking_created_email template."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    t = CommunicationTemplate.objects.filter(code="booking_created_email").first()
    if not t:
        return
    t.subject = "Booking Confirmed - {{ equipment_name }} (Booking #{{ virtual_booking_id }})"
    # body_text: add Virtual Booking ID and Final amount remaining in Wallet
    if "- Booking ID: {{ booking_id }}" in t.body_text and "Virtual Booking ID" not in t.body_text:
        t.body_text = t.body_text.replace(
            "- Booking ID: {{ booking_id }}",
            "- Booking ID: {{ booking_id }}\n- Virtual Booking ID: {{ virtual_booking_id }}",
        )
    if "Total Charge: ₹{{ total_charge }}" in t.body_text and "Final amount remaining" not in t.body_text:
        t.body_text = t.body_text.replace(
            "Total Charge: ₹{{ total_charge }}",
            "Total Charge: ₹{{ total_charge }}\n- Final amount remaining in Wallet: {{ wallet_balance_after }}",
        )
    # body_html: add detail rows for virtual_booking_id and wallet_balance_after
    if "<span class=\"label\">Booking ID:</span> {{ booking_id }}" in t.body_html and "Virtual Booking ID" not in t.body_html:
        t.body_html = t.body_html.replace(
            "<div class=\"detail-row\"><span class=\"label\">Booking ID:</span> {{ booking_id }}</div>",
            "<div class=\"detail-row\"><span class=\"label\">Booking ID:</span> {{ booking_id }}</div>\n                <div class=\"detail-row\"><span class=\"label\">Virtual Booking ID:</span> {{ virtual_booking_id }}</div>",
        )
    if "<span class=\"label\">Total Charge:</span> ₹{{ total_charge }}" in t.body_html and "wallet_balance_after" not in t.body_html:
        t.body_html = t.body_html.replace(
            "<div class=\"detail-row\"><span class=\"label\">Total Charge:</span> ₹{{ total_charge }}</div>",
            "<div class=\"detail-row\"><span class=\"label\">Total Charge:</span> ₹{{ total_charge }}</div>\n                <div class=\"detail-row\"><span class=\"label\">Final amount remaining in Wallet:</span> {{ wallet_balance_after }}</div>",
        )
    t.variable_help = (
        "Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ virtual_booking_id }}, "
        "{{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ total_hours }}, "
        "{{ total_charge }}, {{ wallet_balance_after }}, {{ comment }}, {{ link }}"
    )
    t.save(update_fields=["subject", "body_text", "body_html", "variable_help"])


def reverse_update(apps, schema_editor):
    """Revert template to previous content (optional)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    t = CommunicationTemplate.objects.filter(code="booking_created_email").first()
    if not t:
        return
    t.subject = "Booking Confirmed - {{ equipment_name }} (Booking #{{ booking_id }})"
    if "Virtual Booking ID: {{ virtual_booking_id }}" in t.body_text:
        t.body_text = t.body_text.replace(
            "\n- Virtual Booking ID: {{ virtual_booking_id }}",
            "",
        )
    if "Final amount remaining in Wallet: {{ wallet_balance_after }}" in t.body_text:
        t.body_text = t.body_text.replace(
            "\n- Final amount remaining in Wallet: {{ wallet_balance_after }}",
            "",
        )
    if "Virtual Booking ID" in t.body_html:
        t.body_html = t.body_html.replace(
            "\n                <div class=\"detail-row\"><span class=\"label\">Virtual Booking ID:</span> {{ virtual_booking_id }}</div>",
            "",
        )
    if "wallet_balance_after" in t.body_html:
        t.body_html = t.body_html.replace(
            "\n                <div class=\"detail-row\"><span class=\"label\">Final amount remaining in Wallet:</span> {{ wallet_balance_after }}</div>",
            "",
        )
    t.variable_help = (
        "Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ equipment_name }}, "
        "{{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ total_hours }}, {{ total_charge }}, {{ comment }}, {{ link }}"
    )
    t.save(update_fields=["subject", "body_text", "body_html", "variable_help"])


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0021_ta_nomination_call_email_expected_duty_time"),
    ]

    operations = [
        migrations.RunPython(update_booking_created_email, reverse_update),
    ]
