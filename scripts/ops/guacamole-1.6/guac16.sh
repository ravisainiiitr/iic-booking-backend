#!/usr/bin/env bash
# Guacamole 1.5.x -> 1.6.0 upgrade helper (PostgreSQL backend).
# Runbook: docs/runbooks/guacamole-1.6-upgrade.md
#
#   guac16.sh precheck         read-only snapshot (images, schema, counts, networks, open sessions)
#   guac16.sh active           number of Guacamole connections currently open
#   guac16.sh backup           pg_dump -Fc of guacamole_db into $BACKUP_DIR (verified with pg_restore --list)
#   guac16.sh schema-upgrade   apply upgrade-pre-1.6.0 (no-op when AUDIT already exists)
#   guac16.sh verify           post-upgrade checks (schema, counts, HTTP, network wiring)
#   guac16.sh restore FILE     drop + recreate guacamole_db from a dump (web container must be stopped)
#
# Never prints credentials. Container names can be overridden through the environment.
set -euo pipefail

DB_CONTAINER="${DB_CONTAINER:-iic-booking-backend-guacamole-db-1}"
WEB_CONTAINER="${WEB_CONTAINER:-iic-booking-backend-guacamole-1}"
GUACD_CONTAINER="${GUACD_CONTAINER:-iic-booking-backend-guacd-1}"
DJANGO_CONTAINER="${DJANGO_CONTAINER:-iic-booking-backend-django-1}"
BACKEND_NETWORK="${BACKEND_NETWORK:-iic-booking-backend_default}"
TUNNEL_HOST="${TUNNEL_HOST:-reverse-tunnel-gateway}"
TUNNEL_PORT="${TUNNEL_PORT:-7090}"
GUAC_HOST_URL="${GUAC_HOST_URL:-http://127.0.0.1:8085/guacamole/}"
DB_USER="${DB_USER:-guacamole_user}"
DB_NAME="${DB_NAME:-guacamole_db}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/guacamole}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPGRADE_SQL="$SCRIPT_DIR/upgrade-pre-1.6.0.postgresql.sql"

psql_q() {
    docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" -Atc "$1"
}

has_audit() {
    [ "$(psql_q "select count(*) from pg_enum e join pg_type t on t.oid=e.enumtypid where t.typname='guacamole_system_permission_type' and e.enumlabel='AUDIT'")" = "1" ]
}

counts() {
    psql_q "select 'connections='||(select count(*) from guacamole_connection)
              ||' users='||(select count(*) from guacamole_user)
              ||' parameters='||(select count(*) from guacamole_connection_parameter)
              ||' system_permissions='||(select count(*) from guacamole_system_permission)
              ||' connection_permissions='||(select count(*) from guacamole_connection_permission)"
}

active_sessions() {
    psql_q "select count(*) from guacamole_connection_history where end_date is null"
}

image_of() {
    docker inspect "$1" --format '{{.Config.Image}} id={{slice .Image 7 19}}' 2>/dev/null || echo "missing"
}

networks_of() {
    docker inspect "$1" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{if $v.Aliases}}{{$v.Aliases}}{{end}} {{end}}' 2>/dev/null || echo "missing"
}

cmd_precheck() {
    echo "time:            $(date -Is)"
    echo "web image:       $(image_of "$WEB_CONTAINER")"
    echo "guacd image:     $(image_of "$GUACD_CONTAINER")"
    echo "db image:        $(image_of "$DB_CONTAINER")"
    echo "web networks:    $(networks_of "$WEB_CONTAINER")"
    echo "guacd networks:  $(networks_of "$GUACD_CONTAINER")"
    echo "postgres:        $(psql_q 'show server_version')"
    echo "permission enum: $(psql_q "select string_agg(e.enumlabel, ',' order by e.enumsortorder) from pg_enum e join pg_type t on t.oid=e.enumtypid where t.typname='guacamole_system_permission_type'")"
    if has_audit; then echo "schema:          1.6.0 (AUDIT present)"; else echo "schema:          pre-1.6.0 (upgrade required)"; fi
    echo "counts:          $(counts)"
    echo "db size:         $(psql_q "select pg_size_pretty(pg_database_size('$DB_NAME'))")"
    echo "open sessions:   $(active_sessions)"
    echo "backup dir free: $(df -h "$(dirname "$BACKUP_DIR")" | awk 'NR==2{print $4}')"
}

cmd_backup() {
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    local out="$BACKUP_DIR/guacamole_db-$(date -u +%Y%m%dT%H%M%SZ).dump"
    docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$out"
    chmod 600 "$out"
    local objects
    objects="$(docker exec -i "$DB_CONTAINER" pg_restore --list < "$out" | grep -c ' TABLE DATA ' || true)"
    if [ "${objects:-0}" -lt 20 ]; then
        echo "ERROR: backup $out looks incomplete ($objects table-data entries)" >&2
        exit 1
    fi
    echo "backup:  $out"
    echo "size:    $(du -h "$out" | cut -f1)"
    echo "sha256:  $(sha256sum "$out" | cut -d' ' -f1)"
    echo "tables:  $objects table-data entries"
    echo "counts:  $(counts)"
}

cmd_schema_upgrade() {
    if has_audit; then
        echo "schema already at 1.6.0 (AUDIT present) - nothing to do"
        return 0
    fi
    docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d "$DB_NAME" < "$UPGRADE_SQL"
    has_audit && echo "schema upgraded to 1.6.0 (AUDIT added)"
}

cmd_verify() {
    local fail=0
    if has_audit; then echo "OK   schema 1.6.0"; else echo "FAIL schema not upgraded"; fail=1; fi
    echo "INFO counts $(counts)"
    local web guacd
    web="$(image_of "$WEB_CONTAINER")"; guacd="$(image_of "$GUACD_CONTAINER")"
    case "$web" in *:1.6.0*) echo "OK   web $web";; *) echo "FAIL web $web"; fail=1;; esac
    case "$guacd" in *:1.6.0*) echo "OK   guacd $guacd";; *) echo "FAIL guacd $guacd"; fail=1;; esac
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' "$GUAC_HOST_URL" || true)"
    if [ "$code" = "200" ]; then echo "OK   $GUAC_HOST_URL -> 200"; else echo "FAIL $GUAC_HOST_URL -> $code"; fail=1; fi
    case "$(networks_of "$WEB_CONTAINER")" in
        *"$BACKEND_NETWORK"*guacamole*) echo "OK   web on $BACKEND_NETWORK with alias guacamole";;
        *) echo "FAIL web not on $BACKEND_NETWORK with alias guacamole: $(networks_of "$WEB_CONTAINER")"; fail=1;;
    esac
    case "$(networks_of "$GUACD_CONTAINER")" in
        *"$BACKEND_NETWORK"*) echo "OK   guacd on $BACKEND_NETWORK";;
        *) echo "FAIL guacd not on $BACKEND_NETWORK: $(networks_of "$GUACD_CONTAINER")"; fail=1;;
    esac
    if docker exec "$GUACD_CONTAINER" nc -z -w 3 "$TUNNEL_HOST" "$TUNNEL_PORT" 2>/dev/null; then
        echo "OK   guacd -> $TUNNEL_HOST:$TUNNEL_PORT"
    else
        echo "FAIL guacd cannot reach $TUNNEL_HOST:$TUNNEL_PORT"; fail=1
    fi
    if docker inspect "$DJANGO_CONTAINER" >/dev/null 2>&1; then
        if docker exec "$DJANGO_CONTAINER" python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://guacamole:8080/guacamole/', timeout=5).status==200 else 1)" 2>/dev/null; then
            echo "OK   django -> http://guacamole:8080/guacamole/"
        else
            echo "FAIL django cannot reach http://guacamole:8080/guacamole/"; fail=1
        fi
    else
        echo "SKIP django container $DJANGO_CONTAINER not found"
    fi
    if docker logs --since 10m "$WEB_CONTAINER" 2>&1 | grep -q 'Extension "PostgreSQL Authentication"'; then
        echo "OK   PostgreSQL auth extension loaded"
    else
        echo "WARN PostgreSQL auth extension load line not seen in last 10 min of logs"
    fi
    return "$fail"
}

cmd_restore() {
    local dump="${1:-}"
    [ -f "$dump" ] || { echo "usage: $0 restore /path/to/guacamole_db-*.dump" >&2; exit 2; }
    local objects
    objects="$(docker exec -i "$DB_CONTAINER" pg_restore --list < "$dump" 2>/dev/null | grep -c ' TABLE DATA ' || true)"
    if [ "${objects:-0}" -lt 20 ]; then
        echo "ERROR: $dump is not a usable guacamole_db dump ($objects table-data entries); database left untouched" >&2
        exit 1
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$WEB_CONTAINER" 2>/dev/null || echo false)" = "true" ]; then
        echo "ERROR: stop $WEB_CONTAINER first (docker compose -f docker-compose.guacamole.yml stop guacamole)" >&2
        exit 1
    fi
    docker exec -i "$DB_CONTAINER" psql -X -v ON_ERROR_STOP=1 -U "$DB_USER" -d postgres \
        -c "DROP DATABASE IF EXISTS \"$DB_NAME\" WITH (FORCE)" \
        -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_USER\""
    docker exec -i "$DB_CONTAINER" pg_restore -U "$DB_USER" -d "$DB_NAME" --no-owner --role="$DB_USER" --exit-on-error < "$dump"
    echo "restored $dump"
    echo "counts:  $(counts)"
    if has_audit; then echo "schema:  1.6.0"; else echo "schema:  pre-1.6.0"; fi
}

case "${1:-}" in
    precheck) cmd_precheck ;;
    active) active_sessions ;;
    backup) cmd_backup ;;
    schema-upgrade) cmd_schema_upgrade ;;
    verify) cmd_verify ;;
    restore) shift; cmd_restore "$@" ;;
    *) sed -n '2,12p' "$0"; exit 2 ;;
esac
