"""DisruptionEvent: role of who started / resumed it, expected recovery, linked procurement requests (additive)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0240_disruptionevent_soft_delete'),
    ]

    operations = [
        migrations.AddField(
            model_name='disruptionevent',
            name='started_by_role',
            field=models.CharField(
                blank=True, default='', help_text='Role of the person who started it, at that time.', max_length=16
            ),
        ),
        migrations.AddField(
            model_name='disruptionevent',
            name='ended_by_role',
            field=models.CharField(
                blank=True, default='', help_text='Role of the person who resumed it, at that time.', max_length=16
            ),
        ),
        migrations.AddField(
            model_name='disruptionevent',
            name='expected_recovery_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='When the equipment or slots are expected back; empty = not announced.',
            ),
        ),
        migrations.AddField(
            model_name='disruptionevent',
            name='procurement_request_ids',
            field=models.JSONField(
                blank=True, default=list, help_text='Procurement & Assets requests raised from this disruption.'
            ),
        ),
    ]
