from django.db import migrations


def seed_fee_settings(apps, schema_editor):
    BookingChargeSetting = apps.get_model("equipment", "BookingChargeSetting")
    BookingChargeSetting.objects.get_or_create(
        key="RAZORPAY_CONVENIENCE_FEE_PERCENT",
        defaults={"value": "0"},
    )
    BookingChargeSetting.objects.get_or_create(
        key="RAZORPAY_CONVENIENCE_FEE_GST_PERCENT",
        defaults={"value": "18"},
    )


def unseed_fee_settings(apps, schema_editor):
    BookingChargeSetting = apps.get_model("equipment", "BookingChargeSetting")
    BookingChargeSetting.objects.filter(
        key__in=[
            "RAZORPAY_CONVENIENCE_FEE_PERCENT",
            "RAZORPAY_CONVENIENCE_FEE_GST_PERCENT",
        ]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0001_initial_payment_models"),
        ("equipment", "0172_equipment_slot_tolerance_minutes"),
    ]

    operations = [
        migrations.RunPython(seed_fee_settings, unseed_fee_settings),
    ]
