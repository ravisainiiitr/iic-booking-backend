from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0116_equipment_operator_unavailable_after_booking_end_hours"),
    ]

    operations = [
        # SampleTraceStatus.OP_UNAVAILABLE — choice-only (max_length 20 unchanged).
    ]
