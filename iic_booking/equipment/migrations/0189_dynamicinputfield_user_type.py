# Per-user-type dynamic input fields + HOUR time_formula help text
#
# Production note: an earlier atomic attempt failed mid-way on PostgreSQL
# ("pending trigger events"). A follow-up non-atomic attempt added user_type
# then failed on expand while the OLD unique (equipment, field_key) still
# existed. This migration is therefore idempotent on the database side.

from django.db import migrations, models


def ensure_user_type_column(apps, schema_editor):
    """Add user_type if missing (safe when a prior partial apply already added it)."""
    table = "equipment_dynamicinputfield"
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = 'user_type'
            """,
            [table],
        )
        if cursor.fetchone():
            return
        cursor.execute(
            f'ALTER TABLE "{table}" '
            f"ADD COLUMN \"user_type\" varchar(50) DEFAULT '' NOT NULL"
        )
        cursor.execute(
            f'CREATE INDEX IF NOT EXISTS "equipment_dynamicinputfield_user_type_idx" '
            f'ON "{table}" ("user_type")'
        )


def swap_unique_to_include_user_type(apps, schema_editor):
    """
    Drop legacy UNIQUE(equipment_id, field_key) and ensure
    UNIQUE(equipment_id, user_type, field_key) so expand can insert per user type.
    Never touch the primary key.
    """
    table = "equipment_dynamicinputfield"
    with schema_editor.connection.cursor() as cursor:
        # Only UNIQUE constraints (contype='u'), never PRIMARY KEY ('p').
        cursor.execute(
            """
            SELECT c.conname
            FROM pg_constraint c
            JOIN pg_class t ON c.conrelid = t.oid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = current_schema()
              AND t.relname = %s
              AND c.contype = 'u'
            """,
            [table],
        )
        for (name,) in cursor.fetchall():
            cursor.execute(f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"')

        cursor.execute(
            f'CREATE UNIQUE INDEX IF NOT EXISTS '
            f'"equipment_dynamicinputfield_equip_ut_fkey_uniq" '
            f'ON "{table}" ("equipment_id", "user_type", "field_key")'
        )


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
    # Non-atomic: schema + data steps must commit independently on PostgreSQL.
    atomic = False

    dependencies = [
        ("equipment", "0188_chargeprofile_profile_type"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
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
            ],
            database_operations=[
                migrations.RunPython(ensure_user_type_column, noop_reverse),
                migrations.RunPython(swap_unique_to_include_user_type, noop_reverse),
                migrations.RunPython(expand_input_fields_per_user_type, noop_reverse),
            ],
        ),
    ]
