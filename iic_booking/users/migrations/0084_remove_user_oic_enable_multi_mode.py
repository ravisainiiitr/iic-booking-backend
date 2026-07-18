from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0083_merge_20260718_1240"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="user",
            name="oic_enable_multi_mode",
        ),
    ]
