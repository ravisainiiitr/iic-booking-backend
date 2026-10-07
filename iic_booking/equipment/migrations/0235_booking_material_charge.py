"""IIC material charges on own-material fabrication bookings (additive: one new table)."""

import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0234_equipment_max_print_size'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='BookingMaterialCharge',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('profile_type', models.CharField(help_text='PRINT_3D or LASER_CUT_2D at the time of the charge', max_length=32)),
                ('material_code', models.CharField(max_length=64)),
                ('material_name', models.CharField(max_length=255)),
                ('quantity', models.DecimalField(decimal_places=3, max_digits=12)),
                ('unit', models.CharField(help_text='"sheet" or "g"', max_length=16)),
                ('unit_price', models.DecimalField(decimal_places=4, max_digits=12)),
                ('base_amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('gst_percent', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=5)),
                ('gst_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('computed_amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('amount', models.DecimalField(decimal_places=2, help_text='Amount charged (GST included)', max_digits=12)),
                ('amount_overridden', models.BooleanField(default=False)),
                ('reason', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('wallet_transaction_id', models.PositiveBigIntegerField(blank=True, help_text='Wallet debit made when the charge was posted, if any', null=True)),
                ('reversed_at', models.DateTimeField(blank=True, null=True)),
                ('reversal_reason', models.TextField(blank=True, default='')),
                ('booking', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='material_charges', to='equipment.booking')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('laser_material', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='equipment.lasersheetmaterial')),
                ('print_material', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='equipment.printmaterial')),
                ('reversed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Booking material charge',
                'verbose_name_plural': 'Booking material charges',
                'ordering': ['created_at', 'pk'],
            },
        ),
    ]
