from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0194_chargeprofile_time_formula_textfield"),
    ]

    operations = [
        migrations.AddField(
            model_name="urgentbookingrequest",
            name="waive_urgent_surcharge",
            field=models.BooleanField(
                default=False,
                help_text="Rush relief (NO_SLOT with >=2 peak-window failed attempts): 50% urgent surcharge is not applied",
            ),
        ),
    ]
