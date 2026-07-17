"""Replace 'IIC Booking' branding with 'IIT Roorkee' in email/push templates."""

from django.db import migrations


def forwards(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    fields = ("subject", "body_text", "body_html", "sms_body", "description", "variable_help")
    for tpl in CommunicationTemplate.objects.all().iterator(chunk_size=100):
        updates = []
        for field in fields:
            val = getattr(tpl, field) or ""
            if not isinstance(val, str) or "IIC Booking" not in val:
                continue
            setattr(tpl, field, val.replace("IIC Booking", "IIT Roorkee"))
            updates.append(field)
        if updates:
            tpl.save(update_fields=updates)


def backwards(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    fields = ("subject", "body_text", "body_html", "sms_body", "description", "variable_help")
    for tpl in CommunicationTemplate.objects.all().iterator(chunk_size=100):
        updates = []
        for field in fields:
            val = getattr(tpl, field) or ""
            if not isinstance(val, str) or "IIT Roorkee" not in val:
                continue
            setattr(tpl, field, val.replace("IIT Roorkee", "IIC Booking"))
            updates.append(field)
        if updates:
            tpl.save(update_fields=updates)


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0045_operator_leave_email_templates"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
