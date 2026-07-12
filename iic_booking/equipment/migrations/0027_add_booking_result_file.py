# Generated migration for BookingResultFile (result files on complete booking)

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0026_remove_equipment_slot_start_time_slot_end_time'),
    ]

    operations = [
        migrations.CreateModel(
            name='BookingResultFile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(help_text='Uploaded result file', upload_to='booking_results/%Y/%m/%d/')),
                ('original_name', models.CharField(blank=True, help_text='Original filename when uploaded', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('booking', models.ForeignKey(help_text='Booking this result file belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='result_files', to='equipment.booking')),
            ],
            options={
                'ordering': ['created_at'],
                'verbose_name': 'Booking result file',
                'verbose_name_plural': 'Booking result files',
            },
        ),
    ]
