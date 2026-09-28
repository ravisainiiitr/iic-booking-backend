from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0117_wallet_recharge_deferred_credit"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="dashboard_menu_layout",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Custom dashboard menu groups created by an OIC or Main Administrator: "
                    '{"groups": [{"id", "name", "items": [menu item ids]}]}. Empty = default menu.'
                ),
                verbose_name="Dashboard menu layout",
            ),
        ),
    ]
