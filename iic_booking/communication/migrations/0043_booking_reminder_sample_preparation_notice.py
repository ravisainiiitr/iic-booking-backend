# Add optional sample preparation block to booking reminder email template.

from django.db import migrations


CODE = "booking_reminder_email"

SNIPPET_TEXT = """{{ user_sample_preparation_notice }}"""

SNIPPET_HTML = """{{ user_sample_preparation_notice_html }}"""


def add_sample_prep_placeholders(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    t = CommunicationTemplate.objects.filter(code=CODE).first()
    if not t:
        return

    fields_to_save = []

    body_text = t.body_text or ""
    if "user_sample_preparation_notice" not in body_text:
        needle = "You can view your booking details at: {{ link }}"
        if needle in body_text:
            body_text = body_text.replace(
                needle,
                f"{SNIPPET_TEXT}\n\n{needle}",
                1,
            )
        else:
            body_text = body_text.rstrip() + f"\n\n{SNIPPET_TEXT}\n"
        t.body_text = body_text
        fields_to_save.append("body_text")

    body_html = t.body_html or ""
    if "user_sample_preparation_notice_html" not in body_html:
        needle_p = '<p>You can view your booking details at: <a href="{{ link }}">{{ link }}</a></p>'
        if needle_p in body_html:
            t.body_html = body_html.replace(
                needle_p,
                f"<p>{SNIPPET_HTML}</p>\n            {needle_p}",
                1,
            )
            fields_to_save.append("body_html")
        elif "</body>" in body_html:
            t.body_html = body_html.replace(
                "</body>",
                f"<div>{SNIPPET_HTML}</div>\n</body>",
                1,
            )
            fields_to_save.append("body_html")

    help_extra = ", {{ user_sample_preparation_notice }}, {{ user_sample_preparation_notice_html }}"
    vh = t.variable_help or ""
    if "user_sample_preparation_notice_html" not in vh:
        t.variable_help = vh.rstrip() + help_extra
        fields_to_save.append("variable_help")

    if fields_to_save:
        t.save(update_fields=fields_to_save)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0042_update_waitlist_short_notice_email_body"),
    ]

    operations = [
        migrations.RunPython(add_sample_prep_placeholders, noop_reverse),
    ]
