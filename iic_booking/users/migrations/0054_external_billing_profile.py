from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0053_alter_authsettings_inactivity_timeout_seconds_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExternalBillingProfile",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("billing_name", models.CharField(blank=True, help_text="Legal entity / institute / company name to appear on invoice.", max_length=255, verbose_name="Billing name / company")),
                ("gstin", models.CharField(blank=True, help_text="GSTIN (if applicable).", max_length=30, verbose_name="GSTIN")),
                ("billing_address_line1", models.CharField(blank=True, max_length=255, verbose_name="Billing address line 1")),
                ("billing_address_line2", models.CharField(blank=True, max_length=255, verbose_name="Billing address line 2")),
                ("billing_city", models.CharField(blank=True, max_length=120, verbose_name="Billing city")),
                ("billing_state", models.CharField(blank=True, max_length=120, verbose_name="Billing state")),
                ("billing_pincode", models.CharField(blank=True, max_length=20, verbose_name="Billing pincode")),
                ("billing_country", models.CharField(blank=True, default="India", max_length=120, verbose_name="Billing country")),
                ("shipping_same_as_billing", models.BooleanField(default=True, verbose_name="Shipping same as billing")),
                ("shipping_name", models.CharField(blank=True, max_length=255, verbose_name="Shipping name / contact")),
                ("shipping_phone", models.CharField(blank=True, max_length=30, verbose_name="Shipping phone")),
                ("shipping_address_line1", models.CharField(blank=True, max_length=255, verbose_name="Shipping address line 1")),
                ("shipping_address_line2", models.CharField(blank=True, max_length=255, verbose_name="Shipping address line 2")),
                ("shipping_city", models.CharField(blank=True, max_length=120, verbose_name="Shipping city")),
                ("shipping_state", models.CharField(blank=True, max_length=120, verbose_name="Shipping state")),
                ("shipping_pincode", models.CharField(blank=True, max_length=20, verbose_name="Shipping pincode")),
                ("shipping_country", models.CharField(blank=True, default="India", max_length=120, verbose_name="Shipping country")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="external_billing_profile", to="users.user", verbose_name="User")),
            ],
            options={
                "verbose_name": "External billing profile",
                "verbose_name_plural": "External billing profiles",
            },
        ),
    ]

