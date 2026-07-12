# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0012_add_department_to_equipment'),
    ]

    operations = [
        migrations.AddField(
            model_name='equipment',
            name='video_file',
            field=models.FileField(
                blank=True,
                help_text='Video file for the equipment',
                null=True,
                upload_to='equipment_videos/%Y/%m/%d/',
                verbose_name='Equipment Video',
            ),
        ),
    ]
