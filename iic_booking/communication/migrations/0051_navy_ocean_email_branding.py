# Generated manually — recolor all email templates to Navy Ocean branding

from django.db import migrations


def sync_navy_ocean_email_templates(apps, schema_editor):
    """Re-apply default email HTML so stored templates pick up Navy Ocean colors."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    from iic_booking.communication.default_email_templates import get_default_email_templates

    for spec in get_default_email_templates():
        code = spec["code"]
        desired_name = spec.get("name") or code
        if (
            CommunicationTemplate.objects.filter(name=desired_name)
            .exclude(code=code)
            .exists()
        ):
            desired_name = f"{desired_name} ({code})"
        defaults = {
            "name": desired_name,
            "subject": spec["subject"],
            "body_text": spec["body_text"],
            "body_html": spec["body_html"],
            "description": spec.get("description") or "",
            "variable_help": spec.get("variable_help") or "",
            "is_active": bool(spec.get("is_active", True)),
            "communication_type": "email",
        }
        obj = CommunicationTemplate.objects.filter(code=code, communication_type="email").first()
        if obj is None:
            if CommunicationTemplate.objects.filter(name=defaults["name"]).exists():
                defaults["name"] = f"{defaults['name']} ({code})"
            CommunicationTemplate.objects.create(code=code, **defaults)
            continue
        for key, value in defaults.items():
            setattr(obj, key, value)
        obj.save()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0050_redesign_all_email_templates"),
    ]

    operations = [
        migrations.RunPython(sync_navy_ocean_email_templates, noop_reverse),
    ]
