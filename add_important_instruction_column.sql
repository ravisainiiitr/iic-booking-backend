-- Add important_instruction column to equipment_equipment (run this if migration 0031 cannot be applied)
--
-- Option A - Using psql:
--   psql -U your_user -d your_database -f add_important_instruction_column.sql
--
-- Option B - Using Django dbshell:
--   python manage.py dbshell
--   then paste the ALTER TABLE and COMMENT lines below.
--
-- After running this, mark the migration as applied so Django doesn't try to add the column again:
--   python manage.py migrate equipment 0031_equipment_important_instruction --fake

ALTER TABLE equipment_equipment
ADD COLUMN IF NOT EXISTS important_instruction TEXT NULL;

COMMENT ON COLUMN equipment_equipment.important_instruction IS 'Important instructions shown prominently on the equipment page (above specifications).';
