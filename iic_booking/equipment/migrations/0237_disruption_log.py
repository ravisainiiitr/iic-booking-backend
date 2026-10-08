"""Disruption log tables, slot status change log, DailySlot.external_reference and the Scheduled Maintenance /
Reserved (External) slot status choices (additive; the status choice change needs no SQL)."""

import django.db.models.deletion
import django.utils.timezone
import iic_booking.equipment.disruption_models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0236_equipment_results_overdue_after_hours'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='dailyslot',
            name='external_reference',
            field=models.CharField(blank=True, help_text='Optional I-STEM FBR reference for slots in Reserved (External) status. Staff only.', max_length=100, null=True, verbose_name='I-STEM FBR reference'),
        ),
        migrations.AlterField(
            model_name='dailyslot',
            name='status',
            field=models.CharField(choices=[('AVAILABLE', 'Available'), ('NOT_AVAILABLE', 'Not Available'), ('BOOKED', 'Booked'), ('BLOCKED', 'Blocked'), ('UNDER_MAINTENANCE', 'Under Maintenance'), ('OPERATOR_ABSENT', 'Operator Absent'), ('BOOKING_NOT_UTILIZED', 'Booking Not Utilized'), ('SCHEDULED_MAINT', 'Scheduled Maintenance'), ('RESERVED_EXTERNAL', 'Reserved (External)')], default='AVAILABLE', help_text='Availability status of this slot', max_length=20),
        ),
        migrations.CreateModel(
            name='DisruptionEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('disruption_type', models.CharField(choices=[('UNDER_MAINTENANCE', 'Under Maintenance'), ('OPERATOR_ABSENT', 'Operator Absent'), ('SCHEDULED_MAINTENANCE', 'Scheduled Maintenance'), ('OTHER', 'Other Reasons')], db_index=True, max_length=32)),
                ('scope', models.CharField(choices=[('EQUIPMENT', 'Whole equipment'), ('SLOTS', 'Selected slots')], default='SLOTS', max_length=16)),
                ('source', models.CharField(choices=[('CHANGE_SLOT_STATUS', 'Change slot status'), ('DASHBOARD_CALENDAR', 'Dashboard calendar'), ('BOOKING_DETAILS', 'Booking details'), ('EQUIPMENT_STATUS', 'Equipment status'), ('ADMIN_SLOT_API', 'Admin slot edit'), ('DJANGO_ADMIN', 'Django admin'), ('BACKFILL', 'Recorded from earlier data'), ('OTHER', 'Other')], default='OTHER', max_length=32)),
                ('start_at', models.DateTimeField(db_index=True, help_text='Start of the disrupted period.')),
                ('end_at', models.DateTimeField(blank=True, help_text='End of the disrupted period: last affected slot end, or when the equipment became operational.', null=True)),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now, help_text='When the disruption was recorded.')),
                ('reason_category', models.CharField(blank=True, default='', max_length=32)),
                ('reason', models.TextField(blank=True, default='')),
                ('reason_updated_at', models.DateTimeField(blank=True, null=True)),
                ('slots_affected', models.PositiveIntegerField(default=0)),
                ('bookings_affected', models.PositiveIntegerField(default=0, help_text='Bookings cancelled, refunded or put on hold for a decision by this disruption.')),
                ('ended_at', models.DateTimeField(blank=True, db_index=True, help_text='When staff resumed it.', null=True)),
                ('end_source', models.CharField(blank=True, choices=[('CHANGE_SLOT_STATUS', 'Change slot status'), ('DASHBOARD_CALENDAR', 'Dashboard calendar'), ('BOOKING_DETAILS', 'Booking details'), ('EQUIPMENT_STATUS', 'Equipment status'), ('ADMIN_SLOT_API', 'Admin slot edit'), ('DJANGO_ADMIN', 'Django admin'), ('BACKFILL', 'Recorded from earlier data'), ('OTHER', 'Other')], default='', max_length=32)),
                ('action_taken', models.TextField(blank=True, default='')),
                ('action_updated_at', models.DateTimeField(blank=True, null=True)),
                ('backfilled', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('action_updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('ended_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='disruption_events_ended', to=settings.AUTH_USER_MODEL)),
                ('equipment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='disruption_events', to='equipment.equipment')),
                ('reason_updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('started_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='disruption_events_started', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Disruption event',
                'verbose_name_plural': 'Disruption events',
                'ordering': ['-start_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='DisruptionEventEdit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(max_length=32)),
                ('field', models.CharField(blank=True, default='', max_length=32)),
                ('old_value', models.TextField(blank=True, default='')),
                ('new_value', models.TextField(blank=True, default='')),
                ('note', models.CharField(blank=True, default='', max_length=255)),
                ('edited_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('edited_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='edits', to='equipment.disruptionevent')),
            ],
            options={
                'ordering': ['edited_at', 'id'],
            },
        ),
        migrations.CreateModel(
            name='DisruptionEventSlot',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('start_datetime', models.DateTimeField()),
                ('end_datetime', models.DateTimeField()),
                ('released_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('daily_slot', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='disruption_links', to='equipment.dailyslot')),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slot_links', to='equipment.disruptionevent')),
            ],
            options={
                'ordering': ['start_datetime', 'id'],
            },
        ),
        migrations.CreateModel(
            name='DisruptionServiceReport',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(max_length=255, upload_to=iic_booking.equipment.disruption_models.service_report_upload_to)),
                ('original_name', models.CharField(max_length=255)),
                ('content_type', models.CharField(max_length=100)),
                ('size_bytes', models.PositiveBigIntegerField(default=0)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='service_reports', to='equipment.disruptionevent')),
                ('uploaded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-uploaded_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='SlotStatusChangeLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('new_status', models.CharField(db_index=True, max_length=32)),
                ('previous_statuses', models.JSONField(blank=True, default=dict, help_text='Status -> number of slots before.')),
                ('slot_ids', models.JSONField(blank=True, default=list)),
                ('slot_count', models.PositiveIntegerField(default=0)),
                ('first_start', models.DateTimeField(blank=True, null=True)),
                ('last_end', models.DateTimeField(blank=True, null=True)),
                ('label', models.CharField(blank=True, default='', max_length=255)),
                ('external_reference', models.CharField(blank=True, default='', max_length=100)),
                ('source', models.CharField(choices=[('CHANGE_SLOT_STATUS', 'Change slot status'), ('DASHBOARD_CALENDAR', 'Dashboard calendar'), ('BOOKING_DETAILS', 'Booking details'), ('EQUIPMENT_STATUS', 'Equipment status'), ('ADMIN_SLOT_API', 'Admin slot edit'), ('DJANGO_ADMIN', 'Django admin'), ('BACKFILL', 'Recorded from earlier data'), ('OTHER', 'Other')], default='OTHER', max_length=32)),
                ('bookings_affected', models.PositiveIntegerField(default=0)),
                ('changed_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('changed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('equipment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slot_status_change_logs', to='equipment.equipment')),
            ],
            options={
                'ordering': ['-changed_at', '-id'],
            },
        ),
        migrations.AddIndex(
            model_name='disruptionevent',
            index=models.Index(fields=['equipment', 'disruption_type', 'ended_at'], name='equip_disr_eq_type_end'),
        ),
        migrations.AddIndex(
            model_name='disruptioneventslot',
            index=models.Index(fields=['daily_slot', 'released_at'], name='equip_disr_slot_rel'),
        ),
        migrations.AddConstraint(
            model_name='disruptioneventslot',
            constraint=models.UniqueConstraint(fields=('event', 'daily_slot'), name='equip_disr_slot_event_slot_uniq'),
        ),
    ]
