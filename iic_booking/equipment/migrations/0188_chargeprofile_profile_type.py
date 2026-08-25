# Generated manually for per-user-type ChargeProfile.profile_type

from django.db import migrations, models


def copy_equipment_profile_type_to_charge_profiles(apps, schema_editor):
    ChargeProfile = apps.get_model("equipment", "ChargeProfile")
    Equipment = apps.get_model("equipment", "Equipment")
    eq_types = {
        e.equipment_id: e.profile_type
        for e in Equipment.objects.all().only("equipment_id", "profile_type")
    }
    for cp in ChargeProfile.objects.all().only("id", "equipment_id", "profile_type"):
        if cp.profile_type:
            continue
        inherited = eq_types.get(cp.equipment_id)
        if inherited:
            ChargeProfile.objects.filter(pk=cp.pk).update(profile_type=inherited)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0187_equipment_pi_and_pi_charge_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="chargeprofile",
            name="profile_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("SAMPLE", "Sample-based"),
                    ("HOUR", "Hour-based"),
                    ("SAMPLE_ELEMENT", "Sample + Element"),
                    ("MULTI_PARAM", "Multi-parameter"),
                    ("PRINT_3D", "3D Print"),
                ],
                help_text="Calculation profile type for this user type (SAMPLE, HOUR, …)",
                max_length=20,
                null=True,
            ),
        ),
        migrations.RunPython(copy_equipment_profile_type_to_charge_profiles, noop_reverse),
    ]
