from django.db import migrations


def seed_external_return_shipping_fee(apps, schema_editor):
    BookingChargeSetting = apps.get_model("equipment", "BookingChargeSetting")
    # Default return-shipping fee (INR) applied when external user requests sample return after analysis.
    # Can be changed later in Django Admin -> Booking charge settings.
    BookingChargeSetting.objects.get_or_create(
        key="EXTERNAL_RETURN_SHIPPING_FEE_AMOUNT",
        defaults={"value": "200"},
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0134_booking_return_sample_and_shipping_fee"),
    ]

    operations = [
        migrations.RunPython(seed_external_return_shipping_fee, noop_reverse),
    ]

