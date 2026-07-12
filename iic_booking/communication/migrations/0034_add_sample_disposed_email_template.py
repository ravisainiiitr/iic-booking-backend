from django.db import migrations


def add_sample_disposed_email_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")

    CommunicationTemplate.objects.update_or_create(
        code="sample_disposed_email",
        communication_type="email",
        defaults={
            "name": "Sample disposed (user)",
            "subject": "[IIC Booking] Sample disposed for Booking {{ booking_id }}",
            "body_text": (
                "Dear {{ user_name }},\n\n"
                "This is to inform you that your sample for Booking {{ booking_id }} ({{ equipment_name }}) has been disposed "
                "after the retention period.\n\n"
                "Disposed at: {{ disposed_at }}\n"
                "Remarks (if any): {{ remarks }}\n\n"
                "If you believe this is an error, please contact the lab in-charge/OIC at the earliest.\n\n"
                "Regards,\n"
                "IIC Booking Team\n"
            ),
            "body_html": (
                "<p>Dear <b>{{ user_name }}</b>,</p>"
                "<p>This is to inform you that your sample for Booking <b>{{ booking_id }}</b> "
                "(<b>{{ equipment_name }}</b>) has been <b>disposed</b> after the retention period.</p>"
                "<p><b>Disposed at:</b> {{ disposed_at }}</p>"
                "<p><b>Remarks (if any):</b> {{ remarks }}</p>"
                "<p>If you believe this is an error, please contact the lab in-charge/OIC at the earliest.</p>"
                "<p>Regards,<br/>IIC Booking Team</p>"
            ),
            "description": "Sent to the booking user when lab/OIC marks the sample as DISPOSED after ARCHIVED.",
            "variable_help": (
                "Available variables:\n"
                "- {{ user_name }}\n"
                "- {{ user_email }}\n"
                "- {{ equipment_name }}\n"
                "- {{ booking_id }}\n"
                "- {{ disposed_at }}\n"
                "- {{ remarks }} (optional)\n"
            ),
            "is_active": True,
        },
    )


def remove_sample_disposed_email_template(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    CommunicationTemplate.objects.filter(code="sample_disposed_email", communication_type="email").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("communication", "0033_add_wallet_recharge_sric_office_email_template"),
    ]

    operations = [
        migrations.RunPython(add_sample_disposed_email_template, remove_sample_disposed_email_template),
    ]

