# Generated manually for Razorpay PaymentGateway choice

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0094_test_account_email_settings"),
    ]

    operations = [
        migrations.AlterField(
            model_name="paymentgatewaytransaction",
            name="gateway",
            field=models.CharField(
                choices=[("SBIEPAY", "SBIePay"), ("RAZORPAY", "Razorpay")],
                default="SBIEPAY",
                max_length=20,
            ),
        ),
    ]
