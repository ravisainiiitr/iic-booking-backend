"""
Create the two I-STEM OTP email templates when missing, and move the external services notice to the standard branded layout,
without running the global ``sync_default_email_templates`` (which would overwrite admin-customised templates). A notice an
administrator has changed is left as it is.
"""

from django.db import migrations

NEW_CODES = ("istem_registration_otp_email", "istem_account_link_otp_email")
NOTICE = "external_service_notice"
OLD_NOTICE_SUBJECT = "[IIC Booking] {{ title }}"
OLD_NOTICE_TEXT = "Dear {{ user_name }},\n\n{{ message }}\n\nOpen in the portal: {{ link }}\n\nRegards,\nIIC Booking Team\n"


def forwards(apps, schema_editor):
    from iic_booking.communication.default_email_templates import get_default_email_template

    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    for code in NEW_CODES:
        if CommunicationTemplate.objects.filter(code=code, communication_type="email").exists():
            continue
        spec = get_default_email_template(code)
        if spec is None or CommunicationTemplate.objects.filter(name=spec["name"]).exists():
            continue
        CommunicationTemplate.objects.create(
            code=code, communication_type="email", name=spec["name"], subject=spec["subject"], body_text=spec["body_text"], body_html=spec["body_html"],
            description=spec["description"], variable_help=spec["variable_help"], is_active=True,
        )
    spec = get_default_email_template(NOTICE)
    row = CommunicationTemplate.objects.filter(code=NOTICE, communication_type="email").first()
    if spec is not None and row is not None and row.subject == OLD_NOTICE_SUBJECT and row.body_text == OLD_NOTICE_TEXT:
        CommunicationTemplate.objects.filter(pk=row.pk).update(subject=spec["subject"], body_text=spec["body_text"], body_html=spec["body_html"],
                                                               description=spec["description"], variable_help=spec["variable_help"])


def backwards(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code__in=NEW_CODES, communication_type="email", updated_by__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("communication", "0064_rename_communicati_approva_9c2e1a_idx_communicati_approva_9e6a69_idx_and_more"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
