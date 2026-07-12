from decimal import Decimal

from django.db import migrations, models


def seed_discounted_charge_profiles(apps, schema_editor):
    ChargeProfile = apps.get_model("equipment", "ChargeProfile")

    # Create/update the discounted variant for every existing standard charge profile.
    # Discounted variant has zero charges but keeps time_formula/breakpoint so that
    # time-slot calculations remain consistent.
    for cp in ChargeProfile.objects.filter(pricing_profile="standard").iterator():
        discounted, created = ChargeProfile.objects.get_or_create(
            equipment=cp.equipment,
            user_type=cp.user_type,
            pricing_profile="discounted",
            defaults={
                "is_active": True,
                "primary_unit_charge": Decimal("0.00"),
                "secondary_unit_charge": Decimal("0.00"),
                "breakpoint": cp.breakpoint,
                "time_formula": cp.time_formula,
            },
        )
        if not created:
            discounted.is_active = True
            discounted.primary_unit_charge = Decimal("0.00")
            discounted.secondary_unit_charge = Decimal("0.00")
            discounted.breakpoint = cp.breakpoint
            discounted.time_formula = cp.time_formula
            discounted.save()


def remove_discounted_charge_profiles(apps, schema_editor):
    ChargeProfile = apps.get_model("equipment", "ChargeProfile")
    # Best-effort cleanup: remove discounted rows where charges are zero.
    ChargeProfile.objects.filter(
        pricing_profile="discounted",
        primary_unit_charge=Decimal("0.00"),
        secondary_unit_charge=Decimal("0.00"),
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0091_waitlist_pre_reference_clear_schedule"),
    ]

    # Postgres can error with "pending trigger events" when index creation happens
    # in the same transaction as other row-changing operations in this migration.
    # Running non-atomically avoids that.
    atomic = False

    operations = [
        migrations.AddField(
            model_name="chargeprofile",
            name="pricing_profile",
            field=models.CharField(
                choices=[
                    ("standard", "Standard Charge Profile"),
                    ("discounted", "Discounted Charge Profile"),
                ],
                default="standard",
                db_index=True,
                help_text="Pricing variant (discounted profiles return zero charges).",
                max_length=20,
            ),
        ),
        migrations.AlterUniqueTogether(
            name="chargeprofile",
            unique_together={("equipment", "user_type", "pricing_profile")},
        ),
        migrations.AlterModelOptions(
            name="chargeprofile",
            options={"ordering": ["equipment", "user_type", "pricing_profile"]},
        ),
        migrations.RunPython(
            seed_discounted_charge_profiles,
            remove_discounted_charge_profiles,
        ),
    ]

