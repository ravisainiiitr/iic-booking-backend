"""Per-equipment material support (additive).

Adds the link tables between master-list materials and 3D print / laser cutting equipment, then links every
existing material to the equipment it was added for (enabled and disabled alike). Until now an equipment
offered exactly the enabled materials it owned, so this keeps every booking dropdown unchanged, and a
material that is disabled today comes back on the same equipment when it is re-enabled.
"""

from django.db import migrations, models


def link_materials_to_own_equipment(apps, schema_editor):
    for model_name, profile in (("PrintMaterial", "PRINT_3D"), ("LaserSheetMaterial", "LASER_CUT_2D")):
        Material = apps.get_model("equipment", model_name)
        Through = Material.supported_equipment.through
        material_fk = Material._meta.model_name + "_id"
        rows = [
            Through(**{material_fk: pk, "equipment_id": eq_id})
            for pk, eq_id in Material.objects.filter(equipment__profile_type=profile).values_list("pk", "equipment_id")
        ]
        Through.objects.bulk_create(rows, ignore_conflicts=True, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0232_mode_schedule_optional_dates'),
    ]

    operations = [
        migrations.AddField(
            model_name='lasersheetmaterial',
            name='supported_equipment',
            field=models.ManyToManyField(blank=True, help_text='Laser cutters that offer this master-list sheet. Users see it when it is supported by the equipment and enabled.', related_name='supported_laser_sheet_materials', to='equipment.equipment'),
        ),
        migrations.AddField(
            model_name='printmaterial',
            name='supported_equipment',
            field=models.ManyToManyField(blank=True, help_text='3D printers that offer this master-list material. Users see it when it is supported by the equipment and enabled.', related_name='supported_print_materials', to='equipment.equipment'),
        ),
        migrations.RunPython(link_materials_to_own_equipment, migrations.RunPython.noop),
    ]
