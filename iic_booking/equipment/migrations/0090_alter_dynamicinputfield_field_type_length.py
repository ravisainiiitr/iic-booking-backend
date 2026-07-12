from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0089_add_icpms_standard_coverage_field_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dynamicinputfield",
            name="field_type",
            field=models.CharField(
                choices=[
                    ("NUMERIC", "Numeric"),
                    ("TEXT", "Text"),
                    ("RADIO", "Radio"),
                    ("COMBO", "Combo/Dropdown"),
                    ("MULTI_SELECT", "Multi-select"),
                    ("TOGGLE", "Toggle"),
                    ("PERIODIC_TABLE", "Periodic table / Element selector"),
                    ("TABLE", "Table"),
                    ("ICPMS_STANDARD_COVERAGE", "ICPMS Standard Coverage"),
                ],
                help_text="Type of input field",
                max_length=32,
            ),
        ),
    ]
