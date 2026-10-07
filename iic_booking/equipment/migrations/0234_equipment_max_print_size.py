"""Maximum print size of 3D printing equipment (additive, all nullable).

Blank sizes mean no limit, so existing equipment behaves as before. The rotation flag is nullable so an
older release can still insert equipment rows; null is treated as allowed.
"""

import django.core.validators
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0233_fabrication_material_support'),
    ]

    operations = [
        migrations.AddField(
            model_name='equipment',
            name='allow_print_rotation_to_fit',
            field=models.BooleanField(blank=True, default=True, help_text='For 3D printing equipment: accept a model that fits the maximum print size only after turning it (the lab can re-orient it on the plate).', null=True, verbose_name='Allow rotation to fit'),
        ),
        migrations.AddField(
            model_name='equipment',
            name='max_print_size_x_mm',
            field=models.DecimalField(blank=True, decimal_places=1, help_text='For 3D printing equipment: largest printable width (X) in mm. Leave blank for no limit.', max_digits=7, null=True, validators=[django.core.validators.MinValueValidator(Decimal('0.1'))], verbose_name='Maximum print size X (mm)'),
        ),
        migrations.AddField(
            model_name='equipment',
            name='max_print_size_y_mm',
            field=models.DecimalField(blank=True, decimal_places=1, help_text='For 3D printing equipment: largest printable depth (Y) in mm. Leave blank for no limit.', max_digits=7, null=True, validators=[django.core.validators.MinValueValidator(Decimal('0.1'))], verbose_name='Maximum print size Y (mm)'),
        ),
        migrations.AddField(
            model_name='equipment',
            name='max_print_size_z_mm',
            field=models.DecimalField(blank=True, decimal_places=1, help_text='For 3D printing equipment: largest printable height (Z) in mm. Leave blank for no limit.', max_digits=7, null=True, validators=[django.core.validators.MinValueValidator(Decimal('0.1'))], verbose_name='Maximum print size Z (mm)'),
        ),
    ]
