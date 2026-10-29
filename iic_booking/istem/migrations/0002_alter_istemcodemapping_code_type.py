# Adds the Create User user_type mapping kind and the request-confirmation permission. No schema change.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('istem', '0001_initial'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='istembookinglink',
            options={'ordering': ['-updated_at'], 'permissions': [('view_sensitive_payload', 'Can view unmasked I-STEM payloads'), ('reconcile', 'Can run I-STEM reconciliation'), ('link_manually', 'Can link I-STEM identifiers manually'), ('resolve_uncertain', 'Can request resolution of an uncertain I-STEM operation'), ('approve_resubmission', 'Can approve resubmitting an uncertain I-STEM operation'), ('request_confirm', 'Can request I-STEM booking confirmation'), ('request_execute', 'Can request I-STEM FBR execute')], 'verbose_name': 'I-STEM booking link', 'verbose_name_plural': 'I-STEM booking links'},
        ),
        migrations.AlterField(
            model_name='istemcodemapping',
            name='code_type',
            field=models.CharField(choices=[('SERVICE_TYPE', 'Service type'), ('USAGE_TYPE', 'Usage type'), ('USER_TYPE', 'User type'), ('ANALYSIS_TYPE', 'Analysis type'), ('EQUIPMENT_UOM', 'Equipment unit of measure'), ('PUBLIC_USER_TYPE', 'Create User user type')], max_length=20),
        ),
    ]
