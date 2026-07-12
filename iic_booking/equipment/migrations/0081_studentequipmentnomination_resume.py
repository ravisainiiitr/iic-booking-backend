# Student nomination resume: student can upload resume for OIC/Admin review

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0080_add_ta_nomination_call_and_link'),
    ]

    operations = [
        migrations.AddField(
            model_name='studentequipmentnomination',
            name='resume',
            field=models.FileField(
                blank=True,
                help_text='Resume uploaded by the student for OIC/Admin review',
                null=True,
                upload_to='nomination_resumes/%Y/%m/%d/',
                verbose_name='Resume',
            ),
        ),
        migrations.AddField(
            model_name='studentequipmentnomination',
            name='resume_submitted_at',
            field=models.DateTimeField(
                blank=True,
                help_text='When the student submitted their resume for this nomination',
                null=True,
                verbose_name='Resume submitted at',
            ),
        ),
    ]
