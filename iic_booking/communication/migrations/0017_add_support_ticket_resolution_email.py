# Add Support Ticket Resolution email template (notify user when admin marks resolved/unresolved)

from django.db import migrations


def create_support_ticket_resolution_template(apps, schema_editor):
    """Create email template for support ticket resolution/unresolved notification."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    if not CommunicationTemplate.objects.filter(code="support_ticket_resolution_email").exists():
        CommunicationTemplate.objects.create(
            name="Support Ticket Resolution / Unresolved",
            code="support_ticket_resolution_email",
            communication_type="email",
            subject="Your support ticket #{{ ticket_id }} – {{ status_display }}",
            body_text="""Hello {{ user_name }},

Your support ticket has been updated.

Ticket #{{ ticket_id }}
Subject: {{ subject }}
Status: {{ status_display }}

Notes / Reason:
{{ resolution_notes }}

{{ link }}

Thank you for your feedback.

— IIC Booking""",
            body_html="",
            description="Sent to the user who raised a support ticket when an admin marks it as Resolved or Unresolved (Closed). Editable in Admin Settings > Communication.",
            variable_help="Variables: {{ user_name }}, {{ user_email }}, {{ ticket_id }}, {{ subject }}, {{ status_display }}, {{ resolution_notes }}, {{ link }}.",
            is_active=True,
        )


def remove_support_ticket_resolution_template(apps, schema_editor):
    """Remove Support Ticket Resolution template (reverse migration)."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="support_ticket_resolution_email").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0016_add_operator_unavailable_email"),
    ]

    operations = [
        migrations.RunPython(create_support_ticket_resolution_template, remove_support_ticket_resolution_template),
    ]
