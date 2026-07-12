# Add balance to CouponAssignment: remaining discount amount (capped per booking to equipment cost)

from decimal import Decimal
from django.db import migrations, models


def set_initial_balance(apps, schema_editor):
    CouponAssignment = apps.get_model("equipment", "CouponAssignment")
    for a in CouponAssignment.objects.select_related("coupon").all():
        a.balance = a.coupon.amount
        a.save(update_fields=["balance"])


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0075_add_coupons'),
    ]

    operations = [
        migrations.AddField(
            model_name='couponassignment',
            name='balance',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('0'),
                help_text='Remaining discount amount; capped per booking to equipment cost',
                max_digits=10,
                verbose_name='Balance (₹)',
            ),
        ),
        migrations.RunPython(set_initial_balance, migrations.RunPython.noop),
    ]
