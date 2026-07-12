from django.db import migrations, models


def enable_istem_for_existing_external_charge_profiles(apps, schema_editor):
    """Preserve prior behaviour: external user charge profiles required I-STEM FBR."""
    ChargeProfile = apps.get_model("equipment", "ChargeProfile")
    external_codes = {"external", "RND", "Industry", "external_startup_msme", "other"}
    ChargeProfile.objects.filter(
        user_type__in=external_codes,
        pricing_profile="standard",
    ).update(require_istem_fbr=True)


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0150_print_analysis_batch"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="istem_portal_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="Optional hyperlink to this equipment's page on the I-STEM portal. Shown to users when I-STEM FBR is required for their charge profile.",
                max_length=500,
                verbose_name="I-STEM portal URL",
            ),
        ),
        migrations.AddField(
            model_name="chargeprofile",
            name="require_istem_fbr",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, bookings using this charge profile must enter an I-STEM FBR number and have it verified by the Officer in Charge before results are released.",
                verbose_name="Require I-STEM FBR",
            ),
        ),
        migrations.RunPython(enable_istem_for_existing_external_charge_profiles, migrations.RunPython.noop),
    ]
