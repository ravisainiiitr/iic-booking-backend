# Generated manually for home/non-home slot reservation semantics change.

from django.db import migrations, models


def clear_legacy_home_only_marks(apps, schema_editor):
    """
    Old True meant "home department only". New True means "reserved for non-home".
    Clear legacy True so existing marks are not reinterpreted as non-home reserved;
    unmarked slots remain open until OIC marks new non-home reservations (policy inactive).
    """
    DailySlot = apps.get_model("equipment", "DailySlot")
    DailySlot.objects.filter(home_department_only=True).update(home_department_only=False)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0157_equipmentadditionrequest_extended_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dailyslot",
            name="home_department_only",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=(
                    "When True, this slot is reserved for users outside the equipment’s home "
                    "(internal) department. Unmarked slots are home-department only while any "
                    "upcoming reserved mark exists on the equipment. Unbooked reserved slots open "
                    "to all departments once within Reschedule Hours Threshold before start. "
                    "Admin and OIC can mark/unmark. Has no effect if the equipment has no "
                    "internal department."
                ),
                verbose_name="Reserved for non-home department",
            ),
        ),
        migrations.RunPython(clear_legacy_home_only_marks, noop_reverse),
    ]
