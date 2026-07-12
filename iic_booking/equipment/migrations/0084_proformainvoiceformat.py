# Proforma Invoice Format: admin-editable terms and disclaimer for PDF

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0083_bookingchargesetting"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProformaInvoiceFormat",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "terms_and_conditions",
                    models.TextField(
                        blank=True,
                        default="Standard Terms and Conditions available for Standard Proforma Invoices.",
                        help_text='Shown just after the table. E.g. "Standard Terms and Conditions available for Standard Proforma Invoices."',
                        verbose_name="Terms and conditions text",
                    ),
                ),
                (
                    "disclaimer",
                    models.TextField(
                        blank=True,
                        default="This is a computer generated invoice and does not require a signature.",
                        help_text='Shown at bottom. E.g. "This is a computer generated invoice and does not require a signature."',
                        verbose_name="Disclaimer text",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Proforma invoice format",
                "verbose_name_plural": "Proforma invoice format",
            },
        ),
    ]
