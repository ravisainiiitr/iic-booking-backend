from django.db import migrations, models


def apply_policy_and_depts(apps, schema_editor):
    WalletCreditPolicy = apps.get_model("users", "WalletCreditPolicy")
    Department = apps.get_model("users", "Department")
    policy, _ = WalletCreditPolicy.objects.get_or_create(singleton_key="default")
    updates = []
    if not policy.enabled:
        policy.enabled = True
        updates.append("enabled")
    if int(getattr(policy, "max_credit_duration_days", 30) or 30) < 180:
        policy.max_credit_duration_days = 180
        updates.append("max_credit_duration_days")
    if updates:
        policy.save(update_fields=updates)
    # Enable credit for all internal departments so IIC faculty can select them.
    Department.objects.filter(department_type="internal").update(enable_wallet_credit=True)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0109_enable_wallet_credit_policy"),
    ]

    operations = [
        migrations.AddField(
            model_name="department",
            name="enable_wallet_credit",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Main-administrator switch: when enabled, eligible faculty/staff may request "
                    "Wallet Credit Facility for this department. External users remain ineligible."
                ),
                verbose_name="Wallet credit facility enabled",
            ),
        ),
        migrations.AlterField(
            model_name="walletcreditpolicy",
            name="max_credit_duration_days",
            field=models.PositiveIntegerField(default=180),
        ),
        migrations.RunPython(apply_policy_and_depts, noop_reverse),
    ]