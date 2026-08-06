#!/usr/bin/env bash
# Remote EC2 script: diagnose + migrate device_provisioning + smoke sessions.
set -euo pipefail

APPLY_MIGRATE="${APPLY_MIGRATE:-true}"
RESTART_RUNNER="${RESTART_RUNNER:-true}"

echo "=== host ==="
hostname; whoami; date -u

echo "=== containers ==="
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' || true
CID="$(docker ps --format '{{.Names}}' | grep -E 'django' | head -n 1 || true)"
echo "container=$CID"
test -n "$CID"

echo "=== showmigrations device_provisioning (before) ==="
docker exec -w /app "$CID" python manage.py showmigrations device_provisioning 2>&1 || true

echo "=== table existence probe ==="
docker exec -w /app "$CID" python -c "
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')
django.setup()
from django.db import connection
tables = connection.introspection.table_names()
wanted = sorted(t for t in tables if 'device_provision' in t or 'provisioning' in t)
print('matching_tables', wanted)
"

echo "=== pre-migrate curl sessions ==="
printf '%s\n' '{"device_type":"dsa","machine_guid":"GUID-SSH-PRE","hostname":"SSH-PRE","windows_version":"Windows 11","cpu":"Intel","ram_gb":16,"mac_addresses":["aa:bb:cc:dd:ee:11"],"local_ips":["10.0.0.21"],"application_version":"1.0.2","bootstrap_public_key":"ssh-test"}' > /tmp/prov.json
pre=$(curl -sS -o /tmp/prov_pre.txt -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -H 'Host: equip.iitr.ac.in' \
  --data-binary @/tmp/prov.json \
  http://127.0.0.1:8080/api/v1/provisioning/sessions/ || true)
echo "pre_http=$pre"
cat /tmp/prov_pre.txt; echo

echo "=== in-container traceback (before migrate) ==="
docker exec -w /app "$CID" python -c "
import os, django, traceback, json
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.production')
django.setup()
from iic_booking.device_provisioning import services
from django.test import RequestFactory
payload = {
  'device_type': 'dsa',
  'machine_guid': 'GUID-SSH-TRACE',
  'hostname': 'SSH-TRACE',
  'windows_version': 'Windows 11',
  'cpu': 'Intel',
  'ram_gb': 16,
  'mac_addresses': ['aa:bb:cc:dd:ee:12'],
  'local_ips': ['10.0.0.22'],
  'application_version': '1.0.2',
  'bootstrap_public_key': 'ssh-test',
}
rf = RequestFactory()
req = rf.post('/api/v1/provisioning/sessions/', data=json.dumps(payload), content_type='application/json')
req.META['HTTP_X_FORWARDED_FOR'] = 'unknown, 10.0.0.5'
try:
    session, proof = services.create_session(payload=payload, request=req, actor=None)
    print('create_session OK', session.id, session.status)
except Exception:
    print('create_session FAIL')
    traceback.print_exc()
" || true

if [ "$APPLY_MIGRATE" = "true" ]; then
  echo "=== migrate --noinput ==="
  docker exec -w /app "$CID" python manage.py migrate --noinput
  echo "=== migrate device_provisioning --noinput ==="
  docker exec -w /app "$CID" python manage.py migrate device_provisioning --noinput
else
  echo "SKIP migrate (apply_migrate=false)"
fi

echo "=== showmigrations device_provisioning (after) ==="
docker exec -w /app "$CID" python manage.py showmigrations device_provisioning 2>&1 || true

echo "=== post-migrate curl sessions ==="
printf '%s\n' '{"device_type":"dsa","machine_guid":"GUID-SSH-POST","hostname":"SSH-POST","windows_version":"Windows 11","cpu":"Intel","ram_gb":16,"mac_addresses":["aa:bb:cc:dd:ee:13"],"local_ips":["10.0.0.23"],"application_version":"1.0.2","bootstrap_public_key":"ssh-test"}' > /tmp/prov2.json
post=$(curl -sS -o /tmp/prov_post.txt -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -H 'Host: equip.iitr.ac.in' \
  --data-binary @/tmp/prov2.json \
  http://127.0.0.1:8080/api/v1/provisioning/sessions/ || true)
echo "post_http=$post"
cat /tmp/prov_post.txt; echo

case "$post" in
  201|409) echo "PASS sessions smoke http=$post" ;;
  *)
    echo "FAIL sessions smoke http=$post"
    echo "=== recent django logs ==="
    docker logs --tail 80 "$CID" 2>&1 || true
    exit 1
    ;;
esac

if [ "$RESTART_RUNNER" = "true" ]; then
  echo "=== restart GitHub Actions runner ==="
  if [ -x /home/ubuntu/actions-runner/svc.sh ]; then
    sudo /home/ubuntu/actions-runner/svc.sh stop || true
    sudo /home/ubuntu/actions-runner/svc.sh start || true
    sudo /home/ubuntu/actions-runner/svc.sh status || true
  elif systemctl list-units --type=service --all 2>/dev/null | grep -qi 'actions.runner'; then
    UNIT=$(systemctl list-units --type=service --all | awk '/actions\.runner/ {print $1; exit}')
    echo "unit=$UNIT"
    sudo systemctl restart "$UNIT" || true
    sudo systemctl status "$UNIT" --no-pager || true
  else
    echo "WARN: could not locate actions-runner service"
    ls -la /home/ubuntu/actions-runner 2>/dev/null || true
  fi
fi
