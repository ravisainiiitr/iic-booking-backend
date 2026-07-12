# Update registration self-verification email to mention 10-minute validity

from django.db import migrations


def update_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    t = CommunicationTemplate.objects.filter(code="registration_self_verification_email").first()
    if not t:
        return

    # Only update if it doesn't already mention expiry
    marker = "valid for 10 minutes"
    if t.body_text and marker.lower() in t.body_text.lower():
        return

    t.body_text = (t.body_text or "").strip() + (
        "\n\nThis verification link is valid for 10 minutes. "
        "If no action is taken, the registration will be cancelled and your entry will be deleted."
    )
    if t.body_html:
        t.body_html = t.body_html + (
            "<p><strong>Note:</strong> This verification link is valid for 10 minutes. "
            "If no action is taken, the registration will be cancelled and your entry will be deleted.</p>"
        )
    t.save(update_fields=["body_text", "body_html"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0023_add_registration_and_approval_email_templates"),
    ]

    operations = [
        migrations.RunPython(update_template, noop_reverse),
    ]

