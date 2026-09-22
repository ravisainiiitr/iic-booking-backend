# Seed Analysis Charges PDF from cms/seed_documents if present.

import os
from datetime import datetime

from django.core.files import File
from django.db import migrations


def seed_analysis_charges(apps, schema_editor):
    SiteDocument = apps.get_model("cms", "SiteDocument")
    seed_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "seed_documents",
        "External_charges_Sept_2026.pdf",
    )
    seed_path = os.path.normpath(seed_path)
    obj, _created = SiteDocument.objects.get_or_create(
        key="analysis_charges",
        defaults={"title": "Analysis Charges"},
    )
    if obj.document:
        return
    if not os.path.isfile(seed_path):
        return
    filename = f"cms/site_documents/{datetime.now().strftime('%Y/%m')}/External_charges_Sept_2026.pdf"
    with open(seed_path, "rb") as fh:
        obj.document.save(filename, File(fh), save=True)
    if not obj.title:
        obj.title = "Analysis Charges"
        obj.save(update_fields=["title", "updated_at"])


def unseed_analysis_charges(apps, schema_editor):
    SiteDocument = apps.get_model("cms", "SiteDocument")
    SiteDocument.objects.filter(key="analysis_charges").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("cms", "0009_sitedocument"),
    ]

    operations = [
        migrations.RunPython(seed_analysis_charges, unseed_analysis_charges),
    ]
