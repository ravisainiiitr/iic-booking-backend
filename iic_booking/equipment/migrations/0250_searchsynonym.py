import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def seed(apps, schema_editor):
    from iic_booking.equipment.search_synonyms_seed import seed_search_synonyms

    seed_search_synonyms(
        apps.get_model("equipment", "Equipment"),
        apps.get_model("equipment", "EquipmentCategory"),
        apps.get_model("equipment", "SearchSynonym"),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0249_input_edit_lock_help_text'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SearchSynonym',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('technique', models.CharField(help_text='Shown to users, e.g. "X-ray diffraction"', max_length=120, verbose_name='Technique')),
                ('terms', models.TextField(blank=True, default='', help_text='Comma-separated words people search for, e.g. "xrd, crystal structure, phase identification"', verbose_name='Search terms')),
                ('is_active', models.BooleanField(default=True, verbose_name='Active')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Updated at')),
                ('categories', models.ManyToManyField(blank=True, related_name='search_synonyms', to='equipment.equipmentcategory', verbose_name='Categories')),
                ('equipment', models.ManyToManyField(blank=True, related_name='search_synonyms', to='equipment.equipment', verbose_name='Equipment')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Updated by')),
            ],
            options={
                'verbose_name': 'Search synonym',
                'verbose_name_plural': 'Search synonyms',
                'ordering': ['technique', 'pk'],
            },
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
