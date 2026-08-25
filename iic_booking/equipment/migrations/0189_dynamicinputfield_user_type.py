# Per-user-type dynamic input fields + HOUR time_formula help text

from django.db import migrations, models


def expand_input_fields_per_user_type(apps, schema_editor):
    """
    Legacy rows have empty user_type (shared). Duplicate each field for every
    STANDARD charge-profile user_type on that equipment, then delete shared rows.
    If equipment has no charge profiles, leave empty user_type as shared fallback.
    """
    from django.db.models import Q

    DynamicInputField = apps.get_model("equipment", "DynamicInputField")
    ChargeProfile = apps.get_model("equipment", "ChargeProfile")

    shared = list(DynamicInputField.objects.filter(Q(user_type__isnull=True) | Q(user_type="")))
    by_eq: dict[int, list] = {}
    for f in shared:
        if f.equipment_id is None:
            continue
        by_eq.setdefault(f.equipment_id, []).append(f)

    for eq_id, fields in by_eq.items():
        user_types = list(
            ChargeProfile.objects.filter(
                equipment_id=eq_id,
                pricing_profile="standard",
            )
            .values_list("user_type", flat=True)
            .distinct()
        )
        if not user_types:
            continue
        for f in fields:
            for ut in user_types:
                exists = DynamicInputField.objects.filter(
                    equipment_id=eq_id, user_type=ut, field_key=f.field_key
                ).exists()
                if exists:
                    continue
                DynamicInputField.objects.create(
                    equipment_id=eq_id,
                    user_type=ut,
                    field_key=f.field_key,
                    field_label=f.field_label,
                    field_type=f.field_type,
                    is_required=f.is_required,
                    default_value=f.default_value,
                    options=f.options,
                    help_text=f.help_text,
                    source_element_field_key=f.source_element_field_key,
                    editing_required=f.editing_required,
                )
            f.delete()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    # PostgreSQL: AddField(default=...) leaves deferred trigger events; AlterUniqueTogether
    # then fails with "cannot CREATE INDEX ... pending trigger events" inside one transaction.
    atomic = False

    dependencies = [
        ("equipment", "0188_chargeprofile_profile_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="dynamicinputfield",
            name="user_type",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text="User type this input field applies to (charge-profile scoped)",
                max_length=50,
            ),
        ),
        migrations.AlterField(
            model_name="chargeprofile",
            name="time_formula",
            field=models.CharField(
                blank=True,
                help_text=(
                    'Formula for time calculation using fields A–G '
                    '(e.g. "(A * C) + B" or "((((C-B)/D)*E)*A)/60"). '
                    'HOUR: leave blank or set to "B" for legacy B×slot-duration behavior.'
                ),
                max_length=500,
                null=True,
            ),
        ),
        # Expand shared rows before tightening uniqueness to (equipment, user_type, field_key).
        migrations.RunPython(expand_input_fields_per_user_type, noop_reverse),
        migrations.AlterUniqueTogether(
            name="dynamicinputfield",
            unique_together={("equipment", "user_type", "field_key")},
        ),
        migrations.AlterModelOptions(
            name="dynamicinputfield",
            options={
                "ordering": ["equipment", "user_type", "field_key"],
                "verbose_name": "Dynamic Input Field",
                "verbose_name_plural": "Dynamic Input Fields",
            },
        ),
    ]
