from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0220_peak_window_setting"),
    ]

    operations = [
        migrations.AddField(
            model_name="dynamicinputfield",
            name="table_config",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Advanced table (typed columns) schema: columns (key, label, type, limits, options) "
                    "and row rules (user-managed or linked to a numeric field key)."
                ),
            ),
        ),
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
                    ("TYPED_TABLE", "Advanced table (typed columns)"),
                    ("ICPMS_STANDARD_COVERAGE", "ICPMS Standard Coverage"),
                ],
                help_text="Type of input field",
                max_length=32,
            ),
        ),
    ]
