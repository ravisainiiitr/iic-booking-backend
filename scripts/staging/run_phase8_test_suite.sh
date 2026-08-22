#!/bin/sh
set -eu
BASE="${DATABASE_URL%/*}"
export DATABASE_URL="${BASE}/test_iic_booking_test_8b"
export DJANGO_SETTINGS_MODULE=config.settings.test
cd /app
python manage.py migrate --noinput
python manage.py test \
  users.tests.test_migration_refund_settlement \
  users.tests.test_phase8b_legacy_booking_bridge \
  users.tests.test_phase8c_staging_simulation \
  users.tests.test_real_integration_preflight \
  users.tests.test_real_integration_activation \
  users.tests.test_production_deploy_no_auto_migrate \
  --keepdb -v 1
