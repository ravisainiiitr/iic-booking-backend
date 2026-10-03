from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0222_equipment_results_deadline"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="visible_to_test_accounts_only",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text=(
                    "Testing equipment: only flagged test accounts (and the Main Administrator) can see or book it. "
                    "Hidden from everyone else, from public counts, and from Department Sync Agent installer "
                    "equipment lists."
                ),
                verbose_name="Visible to test accounts only",
            ),
        ),
    ]
