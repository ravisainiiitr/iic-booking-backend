"""Equipment flash messages: short timed messages on the equipment and booking pages, with an audit trail (additive)."""

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0243_laser_own_sheet_size_own_material_default'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='EquipmentFlashMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('message', models.TextField(help_text='Short text; bold, italic and links only (sanitised).')),
                ('tone', models.CharField(choices=[('INFO', 'Info'), ('NOTICE', 'Notice'), ('IMPORTANT', 'Important'), ('SUCCESS', 'Success')], default='INFO', max_length=16)),
                ('start_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('end_at', models.DateTimeField(db_index=True)),
                ('is_active', models.BooleanField(default=True, help_text='Off hides the message without deleting it.')),
                ('audience', models.CharField(choices=[('ALL', 'Everyone (including signed-out visitors)'), ('INTERNAL', 'Internal (IITR) users'), ('EXTERNAL', 'External users'), ('USER_TYPES', 'Selected user types')], default='ALL', max_length=16)),
                ('audience_user_types', models.JSONField(blank=True, default=list)),
                ('show_on_modes', models.BooleanField(default=False, help_text='Multi-mode base instrument: also show on the pages of all its modes.')),
                ('link_url', models.URLField(blank=True, default='', max_length=500)),
                ('link_label', models.CharField(blank=True, default='', max_length=60)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('equipment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='flash_messages', to='equipment.equipment')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Equipment flash message',
                'verbose_name_plural': 'Equipment flash messages',
                'ordering': ['-start_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='EquipmentFlashMessageAudit',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('equipment_id_snapshot', models.PositiveIntegerField(blank=True, null=True)),
                ('action', models.CharField(max_length=24)),
                ('actor_role', models.CharField(blank=True, default='', max_length=32)),
                ('changes', models.JSONField(blank=True, default=dict)),
                ('at', models.DateTimeField(default=django.utils.timezone.now)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('flash_message', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='audit_entries', to='equipment.equipmentflashmessage')),
            ],
            options={
                'ordering': ['at', 'id'],
            },
        ),
        migrations.AddIndex(
            model_name='equipmentflashmessage',
            index=models.Index(fields=['equipment', 'is_active', 'end_at'], name='equip_flash_eq_active_end'),
        ),
    ]
