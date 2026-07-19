"""Replace IIC-exclusive branding in live CommunicationTemplate rows."""

from django.db import migrations

# Ordered longest-first so shorter phrases do not partially undo longer ones.
REPLACEMENTS = [
    ("IIT Roorkee IIC Booking System", "Institute Equipment Booking Portal"),
    ("Thank you for using the IIC Booking System", "Thank you for using the Institute Equipment Booking Portal"),
    ("Thank you for using IIC Booking System", "Thank you for using the Institute Equipment Booking Portal"),
    ("IIC Booking System", "Institute Equipment Booking Portal"),
    ("IIC Booking portal", "Institute Equipment Booking Portal"),
    ("IIC Booking Portal", "Institute Equipment Booking Portal"),
    ("IIC Booking Team", "Institute Equipment Booking Portal Team"),
    ("[IIC Booking]", "[Institute Equipment Booking Portal]"),
    ("— IIC Booking", "— Institute Equipment Booking Portal"),
    ("- IIC Booking", "- Institute Equipment Booking Portal"),
    ("IIC Booking", "Institute Equipment Booking Portal"),
    ("Institute Instrumentation Centre", "IIT Roorkee"),
]


def _apply_replacements(val: str) -> str:
    out = val
    for old, new in REPLACEMENTS:
        if old in out:
            out = out.replace(old, new)
    return out


def forwards(apps, schema_editor):
    CommunicationTemplate = apps.get_model("communication", "CommunicationTemplate")
    fields = ("subject", "body_text", "body_html", "sms_body", "description", "variable_help")
    for tpl in CommunicationTemplate.objects.all().iterator(chunk_size=100):
        updates = []
        for field in fields:
            val = getattr(tpl, field) or ""
            if not isinstance(val, str) or not val:
                continue
            new_val = _apply_replacements(val)
            if new_val != val:
                setattr(tpl, field, new_val)
                updates.append(field)
        if updates:
            tpl.save(update_fields=updates)


def backwards(apps, schema_editor):
    # One-way branding update; reverse is intentionally a no-op.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("communication", "0048_polish_support_ticket_resolution_email"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
