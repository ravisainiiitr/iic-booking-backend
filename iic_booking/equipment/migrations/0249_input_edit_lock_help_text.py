from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0248_equipment_status_follows_parent'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dynamicinputfield',
            name='editing_required',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'If checked, this field can be edited by the user after booking until status is Complete. If '
                    'unchecked, only the Officer in Charge, Department Administrator or Main Administrator can change it.'
                ),
            ),
        ),
        migrations.AlterField(
            model_name='equipment',
            name='enable_charge_recalculation',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'When checked, charges are recalculated whenever the inputs of a confirmed (BOOKED) booking are edited '
                    'before completion. Users can still edit only the fields marked "Editing required"; other fields can be '
                    'changed only by the Officer in Charge, Department Administrator or Main Administrator. If the new '
                    'charge is higher than the amount already deducted, the difference is debited from the wallet; if '
                    'lower, the difference is credited. An email notification is sent to the user.'
                ),
                verbose_name='Enable charge recalculation',
            ),
        ),
    ]
