# Generated manually

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0022_add_blocked_label_to_dailyslot'),
    ]

    operations = [
        migrations.CreateModel(
            name='EquipmentGroup',
            fields=[
                ('equipment_group_id', models.AutoField(primary_key=True, serialize=False)),
                ('name', models.CharField(help_text='Name of the equipment group', max_length=255, verbose_name='Group Name')),
                ('code', models.CharField(help_text='Unique code for the equipment group', max_length=255, unique=True, verbose_name='Group Code')),
                ('description', models.TextField(blank=True, help_text='Optional description of the equipment group', null=True, verbose_name='Description')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Equipment Group',
                'verbose_name_plural': 'Equipment Groups',
                'ordering': ['name'],
            },
        ),
        migrations.AddField(
            model_name='equipment',
            name='equipment_group',
            field=models.ForeignKey(
                blank=True,
                help_text='Equipment group this equipment belongs to. Quota configuration is applied at group level.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='equipment',
                to='equipment.equipmentgroup',
                verbose_name='Equipment Group'
            ),
        ),
        migrations.CreateModel(
            name='EquipmentGroupQuota',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quota_type', models.CharField(choices=[('WEEKLY', 'Weekly'), ('MONTHLY', 'Monthly')], help_text='Type of quota (weekly/monthly)', max_length=20, verbose_name='Quota Type')),
                ('internal_individual_quota_minutes', models.IntegerField(default=0, help_text='Weekly/monthly quota in minutes for internal individual users (INDIVIDUAL_STUDENT)', verbose_name='Internal Individual Quota (minutes)')),
                ('internal_faculty_quota_minutes', models.IntegerField(default=0, help_text='Weekly/monthly quota in minutes for internal faculty users. Shared across all users linked to the same wallet.', verbose_name='Internal Faculty Quota (minutes)')),
                ('external_individual_quota_minutes', models.IntegerField(default=0, help_text='Weekly/monthly quota in minutes for external individual users', verbose_name='External Individual Quota (minutes)')),
                ('external_faculty_quota_minutes', models.IntegerField(default=0, help_text='Weekly/monthly quota in minutes for external faculty users. Shared across all users linked to the same wallet.', verbose_name='External Faculty Quota (minutes)')),
                ('is_enforced', models.BooleanField(default=True, help_text='Whether this quota is enforced', verbose_name='Is Enforced')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('equipment_group', models.ForeignKey(help_text='Equipment group this quota applies to', on_delete=django.db.models.deletion.CASCADE, related_name='quotas', to='equipment.equipmentgroup', verbose_name='Equipment Group')),
            ],
            options={
                'verbose_name': 'Equipment Group Quota',
                'verbose_name_plural': 'Equipment Group Quotas',
                'ordering': ['equipment_group', 'quota_type'],
                'unique_together': {('equipment_group', 'quota_type')},
            },
        ),
    ]
