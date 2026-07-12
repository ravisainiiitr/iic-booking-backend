from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0097_ta_rewards"),
    ]

    operations = [
        migrations.DeleteModel(
            name="CouponUsage",
        ),
        migrations.DeleteModel(
            name="CouponAssignment",
        ),
        migrations.DeleteModel(
            name="Coupon",
        ),
    ]

