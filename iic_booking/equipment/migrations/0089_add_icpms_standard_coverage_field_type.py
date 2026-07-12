# Generated manually: add ICPMS Standard Coverage field type and source_element_field_key

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0088_seed_icpms_standards"),
    ]

    operations = [
        migrations.AddField(
            model_name="dynamicinputfield",
            name="source_element_field_key",
            field=models.CharField(
                blank=True,
                help_text="For ICPMS Standard Coverage: field key (e.g. B) that provides the element list (Periodic Table type).",
                max_length=1,
                null=True,
            ),
        ),
    ]
