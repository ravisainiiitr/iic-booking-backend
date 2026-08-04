from django.db import migrations, models


class Migration(migrations.Migration):
    """Forward-port session duration fields already present on some environments."""

    dependencies = [
        ("equipment", "0181_waitlistentry_opt_out_and_sample"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="equipment",
                    name="analysis_default_session_minutes",
                    field=models.PositiveIntegerField(
                        default=30,
                        help_text="Interactive desktop session length for Analyze Data (default 30).",
                        verbose_name="Default analysis session duration (minutes)",
                    ),
                ),
                migrations.AddField(
                    model_name="equipment",
                    name="analysis_extension_minutes",
                    field=models.PositiveIntegerField(
                        default=15,
                        help_text="Extra minutes when the user extends and no one is waiting (default 15).",
                        verbose_name="Analysis session extension (minutes)",
                    ),
                ),
            ],
            database_operations=[
                # Columns may already exist (prod schema drift). Ensure defaults.
                migrations.RunSQL(
                    sql="""
                    DO $$
                    BEGIN
                      IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='equipment_equipment'
                          AND column_name='analysis_default_session_minutes'
                      ) THEN
                        ALTER TABLE equipment_equipment
                          ADD COLUMN analysis_default_session_minutes integer NOT NULL DEFAULT 30;
                      ELSE
                        UPDATE equipment_equipment
                           SET analysis_default_session_minutes = 30
                         WHERE analysis_default_session_minutes IS NULL;
                        ALTER TABLE equipment_equipment
                          ALTER COLUMN analysis_default_session_minutes SET DEFAULT 30;
                        ALTER TABLE equipment_equipment
                          ALTER COLUMN analysis_default_session_minutes SET NOT NULL;
                      END IF;

                      IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='equipment_equipment'
                          AND column_name='analysis_extension_minutes'
                      ) THEN
                        ALTER TABLE equipment_equipment
                          ADD COLUMN analysis_extension_minutes integer NOT NULL DEFAULT 15;
                      ELSE
                        UPDATE equipment_equipment
                           SET analysis_extension_minutes = 15
                         WHERE analysis_extension_minutes IS NULL;
                        ALTER TABLE equipment_equipment
                          ALTER COLUMN analysis_extension_minutes SET DEFAULT 15;
                        ALTER TABLE equipment_equipment
                          ALTER COLUMN analysis_extension_minutes SET NOT NULL;
                      END IF;
                    END $$;
                    """,
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
        ),
    ]
