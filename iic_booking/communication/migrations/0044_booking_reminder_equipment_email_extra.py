# Add per-equipment extra plain text placeholders to booking reminder email.

from django.db import migrations


CODE = "booking_reminder_email"

SNIPPET_TEXT = "{{ equipment_booking_email_extra }}"

SNIPPET_HTML = (
    '<div style="margin-top:12px;white-space:pre-wrap;font-family:inherit;">'
    "{{ equipment_booking_email_extra_html }}"
    "</div>"
)


def add_equipment_extra_placeholders(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    t = CommunicationTemplate.objects.filter(code=CODE).first()
    if not t:
        return

    fields_to_save = []

    body_text = t.body_text or ""
    if "equipment_booking_email_extra" not in body_text:
        needle = "You can view your booking details at: {{ link }}"
        if needle in body_text:
            t.body_text = body_text.replace(
                needle,
                f"{SNIPPET_TEXT}\n\n{needle}",
                1,
            )
        else:
            t.body_text = body_text.rstrip() + f"\n\n{SNIPPET_TEXT}\n"
        fields_to_save.append("body_text")

    body_html = t.body_html or ""
    if "equipment_booking_email_extra_html" not in body_html:
        needle_p = '<p>You can view your booking details at: <a href="{{ link }}">{{ link }}</a></p>'
        if needle_p in body_html:
            t.body_html = body_html.replace(
                needle_p,
                f"{SNIPPET_HTML}\n            {needle_p}",
                1,
            )
            fields_to_save.append("body_html")
        elif "</body>" in body_html:
            t.body_html = body_html.replace(
                "</body>",
                f"{SNIPPET_HTML}\n</body>",
                1,
            )
            fields_to_save.append("body_html")

    help_token = "{{ equipment_booking_email_extra }}"
    vh = t.variable_help or ""
    if help_token not in vh:
        t.variable_help = vh.rstrip() + ", {{ equipment_booking_email_extra }}, {{ equipment_booking_email_extra_html }}"
        fields_to_save.append("variable_help")

    if fields_to_save:
        t.save(update_fields=fields_to_save)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0043_booking_reminder_sample_preparation_notice"),
    ]

    operations = [
        migrations.RunPython(add_equipment_extra_placeholders, noop_reverse),
    ]
