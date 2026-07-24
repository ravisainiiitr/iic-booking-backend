# Generated manually — redesign all default email CommunicationTemplates

from django.db import migrations


def sync_branded_email_templates(apps, schema_editor):
    """Apply Welcome-email-aligned redesign to all catalog email templates."""
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    # Import catalog at runtime so HTML is built with current branding helpers.
    from iic_booking.communication.default_email_templates import get_default_email_templates

    for spec in get_default_email_templates():
        code = spec["code"]
        # Prefer unique code-derived names; fall back if a colliding name already exists.
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
            # Another row may already own this display name from a prior seed.
            if CommunicationTemplate.objects.filter(name=defaults["name"]).exists():
                defaults["name"] = f"{defaults['name']} ({code})"
            CommunicationTemplate.objects.create(code=code, **defaults)
            continue
        for key, value in defaults.items():
            setattr(obj, key, value)
        obj.save()


def noop_reverse(apps, schema_editor):
    # Content redesign is not safely reversible without storing prior bodies.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0049_institute_equipment_booking_portal_branding"),
    ]

    operations = [
        migrations.RunPython(sync_branded_email_templates, noop_reverse),
    ]
