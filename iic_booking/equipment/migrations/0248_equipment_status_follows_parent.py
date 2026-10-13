from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0247_testdataflag'),
    ]

    operations = [
        migrations.AddField(
            model_name='equipment',
            name='status_follows_parent',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Set automatically when this mode was put Under Maintenance because its base instrument was; '
                    'the mode returns to Operational with the base. Cleared when its status is changed directly.'
                ),
                verbose_name='Status set by base instrument',
            ),
        ),
    ]
