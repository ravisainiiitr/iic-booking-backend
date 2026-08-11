# R11: per-RAA allocation_enabled + catalog link on InstalledSoftware

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("remote_analysis", "0024_r61_catalog_spa_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="installedsoftware",
            name="allocation_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When False, this install is ignored by the Remote Analysis allocator.",
            ),
        ),
        migrations.AddField(
            model_name="installedsoftware",
            name="catalog",
            field=models.ForeignKey(
                blank=True,
                help_text="Canonical catalog entry discovered/linked from this install.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="installed_on",
                to="remote_analysis.analysissoftwarecatalog",
            ),
        ),
    ]
