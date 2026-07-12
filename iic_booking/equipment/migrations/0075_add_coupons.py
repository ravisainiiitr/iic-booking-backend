# Generated manually for Coupons feature

import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0074_alter_bookingattemptlog_options_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Coupon',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(blank=True, db_index=True, help_text='Unique code; generated automatically if left blank on create', max_length=64, unique=True, verbose_name='Coupon code')),
                ('amount', models.DecimalField(decimal_places=2, help_text='Amount to subtract from booking charge', max_digits=10, validators=[django.core.validators.MinValueValidator(Decimal('0.01'))], verbose_name='Discount amount (₹)')),
                ('valid_from', models.DateTimeField(help_text='Coupon can be used from this time', verbose_name='Valid from')),
                ('valid_until', models.DateTimeField(help_text='Coupon can be used until this time', verbose_name='Valid until')),
                ('max_uses', models.PositiveIntegerField(blank=True, help_text='Maximum number of times this coupon can be used (leave blank for unlimited)', null=True, verbose_name='Max uses')),
                ('used_count', models.PositiveIntegerField(default=0, help_text='Number of times this coupon has been used', verbose_name='Used count')),
                ('is_active', models.BooleanField(default=True, help_text='Inactive coupons cannot be used', verbose_name='Active')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='coupons_created', to=settings.AUTH_USER_MODEL, verbose_name='Created by')),
            ],
            options={
                'verbose_name': 'Coupon',
                'verbose_name_plural': 'Coupons',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='CouponAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('assigned_at', models.DateTimeField(auto_now_add=True)),
                ('assigned_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='coupon_assignments_given', to=settings.AUTH_USER_MODEL, verbose_name='Assigned by')),
                ('coupon', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='assignments', to='equipment.coupon', verbose_name='Coupon')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='coupon_assignments', to=settings.AUTH_USER_MODEL, verbose_name='User')),
            ],
            options={
                'verbose_name': 'Coupon assignment',
                'verbose_name_plural': 'Coupon assignments',
                'ordering': ['-assigned_at'],
            },
        ),
        migrations.CreateModel(
            name='CouponUsage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('discount_amount', models.DecimalField(decimal_places=2, max_digits=10, verbose_name='Discount applied (₹)')),
                ('used_at', models.DateTimeField(auto_now_add=True)),
                ('booking', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='coupon_usages', to='equipment.booking', verbose_name='Booking')),
                ('coupon', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='usages', to='equipment.coupon', verbose_name='Coupon')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='coupon_usages', to=settings.AUTH_USER_MODEL, verbose_name='User who used')),
            ],
            options={
                'verbose_name': 'Coupon usage',
                'verbose_name_plural': 'Coupon usages',
                'ordering': ['-used_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='couponassignment',
            constraint=models.UniqueConstraint(fields=('coupon', 'user'), name='equipment_couponassignment_coupon_user_uniq'),
        ),
    ]
