# Generated manually for waitlist opt-out + sample-while-waitlisted

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0179_equipment_google_maps_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="waitlistentry",
            name="opted_out_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the user voluntarily opted out of the waitlist.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="waitlistentry",
            name="sample_submitted",
            field=models.BooleanField(
                default=False,
                help_text="True when the waitlisted user has submitted a sample while awaiting confirmation.",
            ),
        ),
        migrations.AddField(
            model_name="waitlistentry",
            name="sample_identifiers",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Sample identifiers provided while waitlisted.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="waitlistentry",
            name="sample_tracking_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Courier/tracking id for sample submitted while waitlisted.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="waitlistentry",
            name="sample_submitted_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the waitlisted user submitted their sample.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="waitlistentry",
            name="status",
            field=models.CharField(
                default="ACTIVE",
                help_text=(
                    "ACTIVE: eligible for auto-booking. "
                    "CANNOT_FULFILL: removed from queue but kept for audit. "
                    "OPT_OUT: user withdrew; kept for audit; never auto-confirmed."
                ),
                max_length=32,
            ),
        ),
    ]
