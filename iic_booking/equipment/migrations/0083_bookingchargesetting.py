# Booking charge settings: external GST percent (default 18%)

from django.db import migrations, models


def seed_external_gst(apps, schema_editor):
    BookingChargeSetting = apps.get_model("equipment", "BookingChargeSetting")
    BookingChargeSetting.objects.get_or_create(key="EXTERNAL_GST_PERCENT", defaults={"value": "18"})


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0082_equipmentoperatingtacall_expected_duty_time"),
    ]

    operations = [
        migrations.CreateModel(
            name="BookingChargeSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=64, unique=True, verbose_name="Setting key")),
                ("value", models.CharField(default="0", max_length=32, verbose_name="Value")),
            ],
            options={
                "verbose_name": "Booking charge setting",
                "verbose_name_plural": "Booking charge settings",
                "ordering": ["key"],
            },
        ),
        migrations.RunPython(seed_external_gst, noop_reverse),
    ]
