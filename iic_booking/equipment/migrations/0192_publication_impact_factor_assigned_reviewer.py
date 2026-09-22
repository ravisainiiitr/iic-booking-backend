# Generated manually for impact_factor + assigned_reviewer on publication claims

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0191_equipment_publication_claims"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipmentpublication",
            name="impact_factor",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text="Optional journal impact factor as reported by the submitter.",
                max_digits=8,
                null=True,
                verbose_name="Impact factor",
            ),
        ),
        migrations.AddField(
            model_name="equipmentpublicationclaim",
            name="impact_factor",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text="Optional journal impact factor.",
                max_digits=8,
                null=True,
                verbose_name="Impact factor",
            ),
        ),
        migrations.AddField(
            model_name="equipmentpublicationclaim",
            name="assigned_reviewer",
            field=models.ForeignKey(
                blank=True,
                help_text="Faculty supervisor for student claims; blank for OIC/Admin external review.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="equipment_publication_claims_assigned",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Assigned reviewer",
            ),
        ),
    ]
