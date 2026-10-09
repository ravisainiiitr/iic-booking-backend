from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0242_equipment_print_estimate_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="lasercutanalysis",
            name="own_sheet_height_mm",
            field=models.DecimalField(
                blank=True,
                decimal_places=1,
                help_text="Own sheet height entered by the user; blank uses the size worked out from the drawing.",
                max_digits=7,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="lasercutanalysis",
            name="own_sheet_width_mm",
            field=models.DecimalField(
                blank=True,
                decimal_places=1,
                help_text="Own sheet width entered by the user; blank uses the size worked out from the drawing.",
                max_digits=7,
                null=True,
            ),
        ),
        # Python-side default only (no SQL): new equipment offers "I will bring my own material" at ₹0.
        migrations.AlterField(
            model_name="equipment",
            name="own_material_fixed_charge",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                default=Decimal("0.00"),
                help_text=(
                    "For 3D printing and 2D laser cutting equipment: fixed charge (INR, once per booking) that "
                    "replaces the material cost when the user brings their own material. 0 (the default) means no "
                    "material charge; machine time is still charged. Leave blank to hide the 'I will bring my own "
                    "material' option."
                ),
                max_digits=10,
                null=True,
                verbose_name="Own material fixed charge (INR)",
            ),
        ),
    ]
