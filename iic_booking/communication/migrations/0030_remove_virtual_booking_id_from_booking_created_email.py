from django.db import migrations


def remove_virtual_booking_id_line(apps, schema_editor):
    """Remove virtual booking ID line from booking confirmation email."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    template = CommunicationTemplate.objects.filter(code="booking_created_email").first()
    if not template:
        return

    template.body_text = template.body_text.replace(
        "\n- Virtual Booking ID: {{ virtual_booking_id }}",
        "",
    )
    template.body_html = template.body_html.replace(
        "\n                <div class=\"detail-row\"><span class=\"label\">Virtual Booking ID:</span> {{ virtual_booking_id }}</div>",
        "",
    )
    template.variable_help = (
        "Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, "
        "{{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ total_hours }}, "
        "{{ total_charge }}, {{ wallet_balance_after }}, {{ comment }}, {{ link }}"
    )
    template.save(update_fields=["body_text", "body_html", "variable_help"])


def restore_virtual_booking_id_line(apps, schema_editor):
    """Re-add virtual booking ID line to booking confirmation email."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    template = CommunicationTemplate.objects.filter(code="booking_created_email").first()
    if not template:
        return

    if "- Booking ID: {{ booking_id }}" in template.body_text and "Virtual Booking ID" not in template.body_text:
        template.body_text = template.body_text.replace(
            "- Booking ID: {{ booking_id }}",
            "- Booking ID: {{ booking_id }}\n- Virtual Booking ID: {{ virtual_booking_id }}",
        )
    if "<span class=\"label\">Booking ID:</span> {{ booking_id }}" in template.body_html and "Virtual Booking ID" not in template.body_html:
        template.body_html = template.body_html.replace(
            "<div class=\"detail-row\"><span class=\"label\">Booking ID:</span> {{ booking_id }}</div>",
            "<div class=\"detail-row\"><span class=\"label\">Booking ID:</span> {{ booking_id }}</div>\n                <div class=\"detail-row\"><span class=\"label\">Virtual Booking ID:</span> {{ virtual_booking_id }}</div>",
        )
    template.variable_help = (
        "Available variables: {{ user_name }}, {{ user_email }}, {{ booking_id }}, {{ virtual_booking_id }}, "
        "{{ equipment_name }}, {{ equipment_code }}, {{ start_time }}, {{ end_time }}, {{ total_hours }}, "
        "{{ total_charge }}, {{ wallet_balance_after }}, {{ comment }}, {{ link }}"
    )
    template.save(update_fields=["body_text", "body_html", "variable_help"])


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0029_add_urgent_reviewer_supervisor_email"),
    ]

    operations = [
        migrations.RunPython(remove_virtual_booking_id_line, restore_virtual_booking_id_line),
    ]
