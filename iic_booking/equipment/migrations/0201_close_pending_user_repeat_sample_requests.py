"""Repeat samples are now arranged by the OIC; close repeat requests users submitted that are still pending."""

from django.db import migrations
from django.utils import timezone

CLOSE_NOTE = (
    "Closed automatically: repeat samples are now arranged by the Officer In Charge. "
    "Please visit the lab; if a repeat is justified the OIC will book it for you free of charge."
)


def close_pending(apps, schema_editor):
    RepeatSampleRequest = apps.get_model("equipment", "RepeatSampleRequest")
    RepeatSampleRequest.objects.filter(status="PENDING").update(
        status="REJECTED",
        responded_at=timezone.now(),
        admin_notes=CLOSE_NOTE,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0200_urgent_supervisor_caps_and_staff_email_optout"),
    ]

    operations = [
        migrations.RunPython(close_pending, migrations.RunPython.noop),
    ]
