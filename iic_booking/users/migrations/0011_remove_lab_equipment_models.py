# Generated manually to remove Lab, Equipment, EquipmentGroup, EquipmentSpecification, EquipmentAccessory models

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0010_migrate_user_type_and_transaction_type_to_lookup'),
    ]

    operations = [
        # First, remove all data and drop foreign key constraints using DO blocks
        # This handles errors gracefully and avoids transaction abort issues
        migrations.RunSQL(
            """
            DO $$
            DECLARE
                constraint_name text;
            BEGIN
                -- Delete all data (disable triggers first to avoid pending trigger events)
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users_equipmentaccessory') THEN
                    ALTER TABLE users_equipmentaccessory DISABLE TRIGGER USER;
                    DELETE FROM users_equipmentaccessory;
                    ALTER TABLE users_equipmentaccessory ENABLE TRIGGER USER;
                END IF;
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users_equipmentspecification') THEN
                    ALTER TABLE users_equipmentspecification DISABLE TRIGGER USER;
                    DELETE FROM users_equipmentspecification;
                    ALTER TABLE users_equipmentspecification ENABLE TRIGGER USER;
                END IF;
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users_equipment') THEN
                    ALTER TABLE users_equipment DISABLE TRIGGER USER;
                    DELETE FROM users_equipment;
                    ALTER TABLE users_equipment ENABLE TRIGGER USER;
                END IF;
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users_lab') THEN
                    -- Disable triggers temporarily to avoid pending trigger events
                    ALTER TABLE users_lab DISABLE TRIGGER USER;
                    UPDATE users_lab SET status_id = NULL;
                    DELETE FROM users_lab;
                    -- Re-enable triggers (though table will be dropped anyway)
                    ALTER TABLE users_lab ENABLE TRIGGER USER;
                END IF;
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users_equipmentgroup') THEN
                    ALTER TABLE users_equipmentgroup DISABLE TRIGGER USER;
                    DELETE FROM users_equipmentgroup;
                    ALTER TABLE users_equipmentgroup ENABLE TRIGGER USER;
                END IF;
                
                -- Drop foreign key constraints (qualify column names to avoid ambiguity)
                IF EXISTS (SELECT FROM information_schema.table_constraints tc
                           WHERE tc.constraint_name = 'users_equipmentaccessory_equipment_id_fkey'
                           AND tc.table_name = 'users_equipmentaccessory') THEN
                    ALTER TABLE users_equipmentaccessory 
                    DROP CONSTRAINT users_equipmentaccessory_equipment_id_fkey;
                END IF;
                
                IF EXISTS (SELECT FROM information_schema.table_constraints tc
                           WHERE tc.constraint_name = 'users_equipmentspecification_equipment_id_fkey'
                           AND tc.table_name = 'users_equipmentspecification') THEN
                    ALTER TABLE users_equipmentspecification 
                    DROP CONSTRAINT users_equipmentspecification_equipment_id_fkey;
                END IF;
                
                IF EXISTS (SELECT FROM information_schema.table_constraints tc
                           WHERE tc.constraint_name = 'users_equipment_lab_id_fkey'
                           AND tc.table_name = 'users_equipment') THEN
                    ALTER TABLE users_equipment 
                    DROP CONSTRAINT users_equipment_lab_id_fkey;
                END IF;
                
                IF EXISTS (SELECT FROM information_schema.table_constraints tc
                           WHERE tc.constraint_name = 'users_equipment_group_id_fkey'
                           AND tc.table_name = 'users_equipment') THEN
                    ALTER TABLE users_equipment 
                    DROP CONSTRAINT users_equipment_group_id_fkey;
                END IF;
                
                IF EXISTS (SELECT FROM information_schema.table_constraints tc
                           WHERE tc.constraint_name = 'users_equipment_equipment_status_id_fkey'
                           AND tc.table_name = 'users_equipment') THEN
                    ALTER TABLE users_equipment 
                    DROP CONSTRAINT users_equipment_equipment_status_id_fkey;
                END IF;
                
                -- Lab -> MasterLookup (status) - handle dynamic constraint names
                -- Disable triggers first to avoid pending trigger events
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users_lab') THEN
                    ALTER TABLE users_lab DISABLE TRIGGER USER;
                    FOR constraint_name IN 
                        SELECT tc.constraint_name FROM information_schema.table_constraints tc
                        WHERE tc.table_name = 'users_lab' 
                        AND tc.constraint_name LIKE 'users_lab_status_id%'
                        AND tc.constraint_name LIKE '%fk_users_masterlookup_id'
                    LOOP
                        EXECUTE format('ALTER TABLE users_lab DROP CONSTRAINT IF EXISTS %I CASCADE', constraint_name);
                    END LOOP;
                    
                    -- Lab -> Department - handle dynamic constraint names
                    FOR constraint_name IN 
                        SELECT tc.constraint_name FROM information_schema.table_constraints tc
                        WHERE tc.table_name = 'users_lab' 
                        AND tc.constraint_name LIKE 'users_lab_department_id%'
                    LOOP
                        EXECUTE format('ALTER TABLE users_lab DROP CONSTRAINT IF EXISTS %I CASCADE', constraint_name);
                    END LOOP;
                    -- Re-enable triggers (though table will be dropped anyway)
                    ALTER TABLE users_lab ENABLE TRIGGER USER;
                END IF;
            END $$;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        # Disable triggers before dropping tables to avoid pending trigger events
        migrations.RunSQL(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users_equipmentaccessory') THEN
                    ALTER TABLE users_equipmentaccessory DISABLE TRIGGER USER;
                END IF;
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users_equipmentspecification') THEN
                    ALTER TABLE users_equipmentspecification DISABLE TRIGGER USER;
                END IF;
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users_equipment') THEN
                    ALTER TABLE users_equipment DISABLE TRIGGER USER;
                END IF;
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users_equipmentgroup') THEN
                    ALTER TABLE users_equipmentgroup DISABLE TRIGGER USER;
                END IF;
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users_lab') THEN
                    ALTER TABLE users_lab DISABLE TRIGGER USER;
                END IF;
            END $$;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        # Remove EquipmentAccessory first (depends on Equipment)
        migrations.DeleteModel(
            name='EquipmentAccessory',
        ),
        # Remove EquipmentSpecification (depends on Equipment)
        migrations.DeleteModel(
            name='EquipmentSpecification',
        ),
        # Remove Equipment (depends on Lab and EquipmentGroup)
        migrations.DeleteModel(
            name='Equipment',
        ),
        # Remove EquipmentGroup
        migrations.DeleteModel(
            name='EquipmentGroup',
        ),
        # Remove Lab (depends on Department and MasterLookup)
        migrations.DeleteModel(
            name='Lab',
        ),
    ]

