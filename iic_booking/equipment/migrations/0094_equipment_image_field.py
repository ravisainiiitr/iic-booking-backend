from django.db import migrations, models

from iic_booking.equipment.models import equipment_image_upload_to


def migrate_legacy_s3_path_to_image(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    for eq in Equipment.objects.all():
        raw = (getattr(eq, "s3_path", None) or "").strip()
        if not raw:
            continue
        normalized = raw
        if normalized.startswith("media/"):
            normalized = normalized[6:]
        elif normalized.startswith("media") and (len(normalized) == 5 or normalized[5:6] in ("/", "")):
            normalized = normalized[5:].lstrip("/") or normalized
        if len(normalized) > 512:
            normalized = normalized[:512]
        Equipment.objects.filter(pk=eq.pk).update(image=normalized)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0093_user_discounted_charge_equipment"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="image",
            field=models.ImageField(
                blank=True,
                help_text="Photo shown when booking. Stored in the configured media backend until replaced.",
                max_length=512,
                null=True,
                upload_to=equipment_image_upload_to,
                verbose_name="Equipment image",
            ),
        ),
        migrations.RunPython(migrate_legacy_s3_path_to_image, noop_reverse),
        migrations.RemoveField(
            model_name="equipment",
            name="s3_path",
        ),
    ]
