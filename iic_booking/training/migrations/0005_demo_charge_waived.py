from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('training', '0004_module_settings_course_demos_free'),
    ]

    operations = [
        migrations.AlterField(
            model_name='demorequest',
            name='charge_mode',
            field=models.CharField(choices=[('FREE', 'No charge'), ('WALLET', 'Charge faculty wallet'), ('WAIVED', 'Charge waived by the OIC')], default='FREE', max_length=10),
        ),
    ]
