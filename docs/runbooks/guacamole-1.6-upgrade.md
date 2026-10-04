# Runbook: Guacamole + guacd 1.5.5 -> 1.6.0 (Remote Analysis)

Goal: reduce remote-desktop lag in Remote Analysis sessions. 1.6.0 adds the RDP Graphics
Pipeline (GFX), a rewritten display optimizer with scroll/move detection, parallel image
encoding, fewer syscalls when base64-encoding image data, and fixes for RDP stalls.

Branch `chore/guacamole-1.6` (based on the production commit `1969966`, v2.5.47.138).
Helper: `scripts/ops/guacamole-1.6/guac16.sh`. Smoke test: `scripts/ops/guacamole-1.6/smoke_rest_client.py`.

**Approval is required before any step marked [PROD-WRITE].**

---

## 1. Current production layout (inspected read-only, 2026-10-04)

| Item | Value |
| --- | --- |
| Host | EC2 m5a.2xlarge (8 vCPU, 30 GiB), repo `/home/ubuntu/iic-booking-backend` |
| Compose for Guacamole | `docker-compose.guacamole.yml` (NOT `docker-compose.production.yml`), compose project `iic-booking-backend` (shared with the main stack), Docker Compose v5.0.2 |
| Containers | `iic-booking-backend-guacamole-1` (guacamole/guacamole:1.5.5, Tomcat 8.5 / Java 8), `iic-booking-backend-guacd-1` (guacamole/guacd:1.5.5), `iic-booking-backend-guacamole-db-1` (postgres:16-alpine, PG 16.14) |
| Extensions | only `guacamole-auth-jdbc-postgresql` (no header/json/TOTP/LDAP/branding) |
| Config | env vars only (no mounted `GUACAMOLE_HOME`); DB password from `.env` via `${GUACAMOLE_DB_PASSWORD}` |
| Volumes | `guac_db` (Postgres data), `guac_drive`, `guac_record` (unused: drive and recording disabled) |
| Networks | all three on `iic-booking-backend_guac_net`; **guacamole and guacd were additionally attached by hand** to `iic-booking-backend_default` (guacamole with alias `guacamole`). Django calls `http://guacamole:8080/guacamole`; guacd must reach `reverse-tunnel-gateway` on that network. |
| Apache (`equip.iitr.ac.in.conf`) | `/guacamole/websocket-tunnel` -> `ws://127.0.0.1:8085/...`, `/guacamole/` -> `http://127.0.0.1:8085/guacamole/` (`flushpackets=on`), prefork MPM, `mod_proxy_wstunnel` |
| DB schema | standard 1.5.x PostgreSQL schema (permission enum without `AUDIT`); 54 MB; 13 connections, 14 users, ~187k `guacamole_user_history` rows |
| Backend auth | `RA_GUACAMOLE_ADMIN_USERNAME=guacadmin` posts to `api/tokens` (health probe every 30 s from the Django container); per session: ephemeral user `ra-<hex>` + RDP connection `ra-session-<hex>`, user token minted server-side, browser opens `https://equip.iitr.ac.in/guacamole/#/client/<base64 id>?token=...` in an iframe |
| Stored RDP params | `color-depth=24`, `width=1920`, `height=1080`, `security=nla`, `ignore-cert=true`, audio on, drive/printing off |

## 2. What changes

| Change | Why |
| --- | --- |
| Images `guacamole/guacamole` + `guacamole/guacd` 1.5.5 -> 1.6.0, pinned through `GUACAMOLE_VERSION` (default `1.6.0`) | the upgrade; one variable to roll back |
| Schema: `upgrade-pre-1.6.0.sql` adds enum value `AUDIT` to `guacamole_system_permission_type` | required by 1.6.0 JDBC auth; additive only |
| `docker-compose.guacamole.yml` declares the external network `${RA_BACKEND_NETWORK:-iic-booking-backend_default}` for guacamole and guacd | recreating the containers would otherwise drop today's hand-made attachments and break Django -> Guacamole and guacd -> reverse tunnel (total Remote Analysis outage) |
| `BAN_ENABLED: "false"` | the 1.6.0 image turns on the new auth-ban extension by default (5 failed logins per IP -> HTTP 429 for 5 min). Every browser reaches Tomcat from the Docker gateway IP and every backend login comes from the Django container, so one IP's failures (e.g. reopening an expired session) would block all users / all session launches. Keeps 1.5.5 behaviour. |
| guacd `LOG_LEVEL: info` | `GUACD_LOG_LEVEL` is deprecated in 1.6 |
| `POSTGRESQL_USER` kept | the 1.5.5 image requires it (rollback); 1.6.0 accepts it and logs one deprecation warning |
| `docker-compose.ra-production.yml` (fresh-server stack) bumped the same way | consistency |

No backend Python changes are needed: every REST call the portal uses (`POST api/tokens`, users,
connections, `PUT` connection, `GET .../parameters`, `PATCH .../permissions`, `DELETE`, the
`Guacamole-Token` header, `#/client/<id>?token=`) behaves identically on 1.6.0 (verified, section 6).

## 3. 1.6.0 changes that matter here

- **Schema upgrade required** (`AUDIT` permission). 1.5.5 still runs on the upgraded schema (tested), so an image-only rollback is possible.
- **GFX on by default**. guacd forces **32 bpp** whenever GFX is active and logs
  `Ignoring requested color depth of 24 bpp, as the RDP Graphics Pipeline requires 32 bpp.` on every connection.
- **New RDP `timeout` parameter**, default 10 s, for the RDP connect phase.
- **Parallel encoding**: guacd uses one encoder thread per CPU per connection (8 on m5a.2xlarge). Expect higher guacd CPU bursts, lower bandwidth.
- **Docker images**: web image moves to Tomcat 9.0.106 / Java 21; every `guacamole.properties` key can be set from an env var; `*_FILE` env vars supported; `--link` env, `GUACD_LOG_LEVEL`, `LOGBACK_LEVEL`, `POSTGRESQL_USER` deprecated (warnings only).
- **auth-ban enabled by default in the image** (see section 2).
- **UI**: clipboard contents in the Guacamole menu are hidden until clicked; new import, audit and recording-player features (admin only).
- Duo v4, `guac_client`/`libguac` ABI changes: not used here.
- The official guacd 1.6.0 image is still built against **FreeRDP 2.11.7**; GFX works on that path.

### RDP parameters for the backend workstream (`connection.py`, not changed here)

| Parameter | 1.6.0 behaviour | Suggestion |
| --- | --- | --- |
| `disable-gfx` | new; empty/absent = GFX on | expose as a toggle (`"true"` to fall back to the legacy bitmap path if a PC shows artefacts/black screens) |
| `color-depth` | ignored (forced 32) while GFX is on; honoured when `disable-gfx=true` | send `""` or `32` with GFX on to avoid the warning; keep 24/16 only for the `disable-gfx` path |
| `timeout` | new, default 10 s | set `30` if launches through the reverse tunnel start failing with "server unreachable" |
| `width` / `height` / `dpi` | unchanged | viewport contract (even numbers, clamped); `dpi` = 96 x dpr if dpr is passed |
| `resize-method` | unchanged | keep empty (contract) |
| `force-lossless`, `enable-wallpaper`, `enable-theming`, `enable-font-smoothing`, `enable-full-window-drag`, `enable-desktop-composition`, `enable-menu-animations` | unchanged; defaults are the fast settings | leave unset |

## 4. Pre-checks (day before, read-only)

```bash
cd /home/ubuntu/iic-booking-backend
git log -1 --oneline                       # expect 1969966 or a descendant
bash scripts/ops/guacamole-1.6/guac16.sh precheck   # after the files are on the host (section 5.1)
df -h / ; docker system df                 # need ~1.5 GB for images + 2x DB size for backups
docker compose -f docker-compose.guacamole.yml config --quiet && echo compose-ok
```

Confirm in `precheck`: images 1.5.5, schema `pre-1.6.0`, guacamole on `iic-booking-backend_default[guacamole]`,
guacd on `iic-booking-backend_default`, `open sessions: 0` at window start.

Confirm there are no Remote Analysis sessions booked in the window (portal admin -> Remote Analysis -> sessions),
and that the test-only equipment **DSATEST** has an analysis PC online for verification.

## 5. Maintenance window

Pick a low-use slot (e.g. 13:00-13:30 IST Sunday or after 20:00). Expected remote-desktop downtime:
2-5 minutes. The portal stays up; only opening/using remote desktops is affected. Notify users of
Remote Analysis beforehand.

### 5.1 Stage files [PROD-WRITE: repo working tree]

Push `chore/guacamole-1.6`, then on the host:

```bash
cd /home/ubuntu/iic-booking-backend
git fetch origin chore/guacamole-1.6
git checkout FETCH_HEAD -- docker-compose.guacamole.yml docker-compose.ra-production.yml \
    scripts/ops/guacamole-1.6 docs/runbooks/guacamole-1.6-upgrade.md
docker compose -f docker-compose.guacamole.yml config --quiet && echo compose-ok
```

(Merge the branch into the deploy line afterwards so the working tree is clean again.)

### 5.2 Pre-pull images [PROD-WRITE: disk only, running containers unaffected]

```bash
docker pull guacamole/guacamole:1.6.0
docker pull guacamole/guacd:1.6.0
# keep the cached 1.5.5 images for rollback; do NOT run docker image prune during this change
```

Digests tested locally: guacamole 1.6.0 `sha256:f344085e618b...`, guacd 1.6.0 `sha256:8974eaa9ba32...`;
production 1.5.5 digests: guacamole `sha256:0f62f6d17ab3...`, guacd `sha256:38232cae2713...`.

### 5.3 Upgrade [PROD-WRITE]

```bash
cd /home/ubuntu/iic-booking-backend
G=scripts/ops/guacamole-1.6/guac16.sh
bash $G precheck | tee ~/guac16-precheck-$(date +%F).txt
bash $G active                                  # must print 0; otherwise wait or warn the user

# 1. stop web + guacd (DB keeps running). Target services explicitly; NEVER use --remove-orphans
#    (this compose project also owns django/celery/redis/...).
docker compose -f docker-compose.guacamole.yml stop guacamole guacd

# 2. backup (custom-format dump, verified with pg_restore --list; prints path, size, sha256)
bash $G backup | tee ~/guac16-backup-$(date +%F).txt

# 3. schema upgrade (no-op if already applied)
bash $G schema-upgrade

# 4. start 1.6.0 (recreates only guacd + guacamole; guacamole-db must show "Running", not "Recreate")
docker compose -f docker-compose.guacamole.yml up -d guacd guacamole

# 5. wait for health, then verify
sleep 20; docker ps --filter name=guac --format '{{.Names}} {{.Image}} {{.Status}}'
bash $G verify
```

## 6. Verification

1. `guac16.sh verify` all `OK` (schema 1.6.0, both images 1.6.0, `http://127.0.0.1:8085/guacamole/` 200,
   guacamole on the backend network with alias `guacamole`, guacd on it and able to reach
   `reverse-tunnel-gateway:7090`, Django can reach `http://guacamole:8080/guacamole/`, PostgreSQL extension loaded).
2. Backend REST client smoke test (creates and deletes a throw-away user + connection):
   ```bash
   docker exec -i iic-booking-backend-django-1 python manage.py shell < scripts/ops/guacamole-1.6/smoke_rest_client.py
   ```
   Expect `PASS 10 checks`.
3. Public path: `curl -sI https://equip.iitr.ac.in/guacamole/ | head -1` -> 200.
4. Guacamole logs: `docker logs --since 5m iic-booking-backend-guacamole-1 2>&1 | grep -E 'ERROR|loaded'`
   (only the expected `POSTGRESQL_USER` deprecation warning; no `ERROR`).
5. **Test session on DSATEST only**: as a test user, open Analysis Environment for a DSATEST booking, confirm
   auto-login, scroll a long document, move a window, type, copy/paste, end the session; confirm files sync as before.
   During the session: `docker logs --since 5m iic-booking-backend-guacd-1` should show
   `Graphical updates will be encoded using 8 worker thread(s)` and the 32 bpp GFX line, and no
   `RDP server closed/refused connection` errors.
6. Health probes keep succeeding: `docker logs --since 2m iic-booking-backend-guacamole-1 | grep -c 'successfully authenticated'` > 0.

If any of 1-5 fails and cannot be fixed within 10 minutes: roll back.

## 7. Rollback

**A. Images only (fast, ~1 min; schema stays upgraded - 1.5.5 runs fine on it, tested):**

```bash
GUACAMOLE_VERSION=1.5.5 docker compose -f docker-compose.guacamole.yml up -d guacd guacamole
bash scripts/ops/guacamole-1.6/guac16.sh precheck     # images 1.5.5, networks intact
docker exec -i iic-booking-backend-django-1 python manage.py shell < scripts/ops/guacamole-1.6/smoke_rest_client.py
```

Make it permanent with `GUACAMOLE_VERSION=1.5.5` in `.env` (or revert the compose file) so a later
`up` does not re-upgrade.

**B. Full (images + DB restore to the pre-upgrade dump; loses Guacamole changes made after the backup -
only ephemeral session users/connections and history):**

```bash
GUACAMOLE_VERSION=1.5.5 docker compose -f docker-compose.guacamole.yml stop guacamole guacd
bash scripts/ops/guacamole-1.6/guac16.sh restore ~/backups/guacamole/guacamole_db-<timestamp>.dump
GUACAMOLE_VERSION=1.5.5 docker compose -f docker-compose.guacamole.yml up -d guacd guacamole
```

`restore` refuses to run while the web container is up and validates the dump before dropping anything.

**C. Last resort (old compose file):** `git checkout 1969966 -- docker-compose.guacamole.yml`,
`docker compose -f docker-compose.guacamole.yml up -d guacd guacamole`, then restore the hand-made wiring:

```bash
docker network connect --alias guacamole iic-booking-backend_default iic-booking-backend-guacamole-1
docker network connect iic-booking-backend_default iic-booking-backend-guacd-1
```

## 8. Expected effects

- Noticeably smoother scrolling and window moves (scroll/copy detection reuses pixels instead of re-encoding),
  lower bandwidth per session, faster recovery after bursts (frames are combined when the browser lags).
- 32-bit colour sessions (GFX); slightly more bandwidth on photo-like content, less on UI/text.
- Higher but shorter guacd CPU bursts (up to 8 encoder threads per session). Host has 8 vCPU and a few
  concurrent sessions, so headroom is ample; watch `docker stats` during the first busy day.
- Guacamole menu: clipboard text hidden until clicked (users may ask).
- Log noise: one `POSTGRESQL_USER` deprecation warning at web start; one 32 bpp warning per RDP connection
  until `connection.py` stops sending `color-depth=24`.

## 9. Measuring lag before / after

Run the same script on DSATEST before (1.5.5) and after (1.6.0), same browser, same network, same
1920x1080 session, same time of day, 3 runs each.

Workload (3 minutes): scroll a 50-page PDF top-to-bottom for 60 s; drag a window in circles for 30 s;
pan/zoom a plot in the analysis software for 60 s; type a paragraph in Notepad for 30 s.

Server side (read-only sampling while the workload runs):

```bash
while sleep 1; do docker stats --no-stream --format \
  "$(date +%T),{{.Name}},{{.CPUPerc}},{{.NetIO}}" iic-booking-backend-guacd-1 iic-booking-backend-guacamole-1; \
done | tee ~/guac-lag-$(date +%F-%H%M).csv
```

Metrics:

| Metric | How |
| --- | --- |
| Bytes sent to browser per workload | delta of guacamole container NetIO "out" (and Chrome DevTools -> Network -> WS frames size) |
| guacd CPU (avg / p95) | from the CSV |
| Perceived frame rate while scrolling | screen-record the browser at 60 fps (OBS), count distinct frames per second in the scroll segment |
| Input latency | in the recording, frames between a keypress (on-screen keyboard overlay) and the character appearing in Notepad |
| Stalls | count freezes > 500 ms in the recording; count `User is not responding` / forced terminations in guacd logs |
| Subjective | operator rates 1-5 for scrolling, typing, plotting |

Success: lower bytes per workload, higher scroll frame rate, equal or lower input latency, no new stalls.

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| Containers lose the backend network on recreate | network now declared in compose; `verify` checks it; rollback C restores it by hand |
| GFX incompatibility on a particular analysis PC (black screen / artefacts) | `disable-gfx=true` per connection once the backend toggle exists; otherwise rollback A |
| Higher guacd CPU with many concurrent sessions | monitor; host has 8 vCPU |
| RDP connect over the reverse tunnel slower than the new 10 s default `timeout` | set `timeout=30` in `connection.py` (backend workstream) |
| auth-ban accidentally enabled later | keep `BAN_ENABLED=false` until RemoteIpValve + backend-side handling exists |
| Schema upgrade fails | it is a single additive `ALTER TYPE`; DB backup taken immediately before; rollback B |
