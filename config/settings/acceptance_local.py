"""Local acceptance testing of the external-booking enhancement. Disposable local database only.

Not for staging or production. Importing this module raises unless:

* the process opted in: ``IIC_ACCEPTANCE_ENV=local-acceptance`` (set by ``scripts/extbook_local/extbook-local.ps1``),
  and it is not a container or CI job;
* ``DATABASE_URL`` is PostgreSQL on this machine (127.0.0.1 / localhost / ::1) and the database is exactly
  ``iic_extbook_accept_dev``.

Every new database connection is checked as well: the server must report a loopback address and the database must
carry the disposable marker set by ``extbook-local.ps1 create-db`` (see ``acceptance_guards``).

Integrations:

* E-mail: console backend (printed, never sent). ``ACCEPTANCE_EMAIL_FILES=1`` writes files to
  ``.acceptance_local/mail`` instead (used by the scripted runs to inspect messages). No SMTP / SES settings are used.
* Storage: local disk under ``.acceptance_local/media``; AWS credential files, profiles and instance metadata are
  switched off so boto3 cannot find real credentials.
* Razorpay: no key id / secret, so order creation and the legacy wallet client refuse before any network call. The
  webhook secret is a local constant; ``scripts/extbook_local/mock_razorpay.py`` signs simulated events with it and
  injects a fake gateway client in its own process only. SBIePay: no merchant, gateway URL points at a dead local port.
* SRIC, OpenAI, Ollama, channels/Redis: off or in-memory. Celery runs tasks inline (local.py); worker and beat
  processes are refused and the database beat schedule is never used, so scheduled jobs only run when invoked with
  ``extbook-local.ps1 task``.
* Network: DNS lookups and connections to anything but loopback raise ``OutboundNetworkBlocked`` in-process, so a
  missed integration (SMTP, IMAP, S3, web push, Guacamole, I-STEM ...) fails loudly instead of reaching a real service.
* Optional simulated clock: ``ACCEPTANCE_CLOCK=2026-10-28T21:05:00+05:30`` starts ``timezone.now()`` at that instant
  (then it ticks). The Windows clock is not touched.
"""

import os
import re
import sys

from django.core.exceptions import ImproperlyConfigured

from .acceptance_guards import ACCEPTANCE_DATABASE_NAME
from .acceptance_guards import AcceptanceGuardError
from .acceptance_guards import LOCAL_HOSTS
from .acceptance_guards import check_connected_database
from .acceptance_guards import check_database_url
from .acceptance_guards import check_environment
from .acceptance_guards import install_network_guard

try:
    check_environment()
    _DB_HOST, _DB_PORT, _DB_NAME = check_database_url(os.environ.get("DATABASE_URL", ""))
except AcceptanceGuardError as exc:
    raise ImproperlyConfigured(f"acceptance_local: {exc}") from None
if any("celery" in os.path.basename(str(a)).lower() for a in sys.argv[:2]):
    raise ImproperlyConfigured("acceptance_local: no Celery worker or beat; run tasks with extbook-local.ps1 task.")

# Never read a .env file; neutralise every credential source before the base settings read the environment.
os.environ["DJANGO_READ_DOT_ENV_FILE"] = "False"
os.environ["USE_DOCKER"] = "no"
os.environ["USE_S3_MEDIA"] = "0"
os.environ["USE_AWS_SES_API"] = "False"
os.environ["DJANGO_EMAIL_BACKEND"] = "django.core.mail.backends.console.EmailBackend"
for _name in (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SES_ACCESS_KEY_ID",
    "AWS_SES_SECRET_ACCESS_KEY", "AWS_SES_SMTP_USERNAME", "AWS_SES_SMTP_PASSWORD", "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET", "SRIC_API_KEY", "OPENAI_API_KEY", "SBIEPAY_MERCHANT_ID",
    "SBIEPAY_ENCRYPTION_KEY",
):
    os.environ[_name] = ""
for _name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
    os.environ.pop(_name, None)
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"

from .local import *  # noqa: E402, F403
from .local import BASE_DIR  # noqa: E402
from .local import DATABASES  # noqa: E402

# Re-check what Django will actually use (after env parsing).
_db = DATABASES["default"]
if (_db.get("HOST") or "").lower() not in LOCAL_HOSTS or _db.get("NAME") != ACCEPTANCE_DATABASE_NAME:
    raise ImproperlyConfigured("acceptance_local: the parsed database settings are not the local acceptance database.")
if len(DATABASES) != 1:
    raise ImproperlyConfigured("acceptance_local: only the default (local acceptance) database may be configured.")

ACCEPTANCE_LOCAL = True
ACCEPTANCE_DATA_DIR = BASE_DIR / ".acceptance_local"

# E-mail: printed to the console by default; files only in the dedicated local folder when asked for.
ANYMAIL = {}
EMAIL_HOST = "127.0.0.1"
EMAIL_PORT = 9  # discard port: even a misconfigured SMTP backend could not reach a real server
EMAIL_HOST_USER = ""
EMAIL_HOST_PASSWORD = ""
if os.environ.get("ACCEPTANCE_EMAIL_FILES") == "1":
    EMAIL_BACKEND = "django.core.mail.backends.filebased.EmailBackend"
    EMAIL_FILE_PATH = str(ACCEPTANCE_DATA_DIR / "mail")
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Uploads on local disk; no S3.
MEDIA_ROOT = str(ACCEPTANCE_DATA_DIR / "media")
MEDIA_URL = "/media/"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
AWS_ACCESS_KEY_ID = ""
AWS_SECRET_ACCESS_KEY = ""
AWS_SES_ACCESS_KEY_ID = ""
AWS_SES_SECRET_ACCESS_KEY = ""

# Payments: no gateway credentials, so every real client refuses; a local-only webhook secret for the mock.
RAZORPAY_KEY_ID = ""
RAZORPAY_KEY_SECRET = ""
RAZORPAY_WEBHOOK_SECRET = "acceptance-local-webhook-secret-not-a-real-secret"
SBIEPAY_MERCHANT_ID = ""
SBIEPAY_ENCRYPTION_KEY = ""
SBIEPAY_GATEWAY_URL = "http://127.0.0.1:9/sbiepay-disabled-in-acceptance"
SBIEPAY_SUCCESS_URL = ""
SBIEPAY_FAILURE_URL = ""
SBIEPAY_PUSH_URL = ""

# Other integrations off.
SRIC_API_KEY = ""
SRIC_EMAIL_FALLBACK = False
OPENAI_API_KEY = ""
OLLAMA_BASE_URL = "http://127.0.0.1:9"  # discard port: nothing listens
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Scheduled jobs: never from the database schedule (only explicit extbook-local.ps1 task runs).
CELERY_BEAT_SCHEDULER = "celery.beat:PersistentScheduler"
CELERY_BEAT_SCHEDULE = {}

# Enforce quotas like production (local.py skips them by default).
SKIP_BOOKING_QUOTA_CHECK = False

install_network_guard()


def _verify_connection(sender, connection, **kwargs):
    with connection.cursor() as cur:
        cur.execute(
            "select host(inet_server_addr()), current_database(), "
            "shobj_description(d.oid, 'pg_database') from pg_database d where d.datname = current_database()",
        )
        address, database, comment = cur.fetchone()
    try:
        check_connected_database(address, database, comment)
    except AcceptanceGuardError as exc:
        connection.close()
        raise ImproperlyConfigured(f"acceptance_local: {exc}") from None


def _connect_guard():
    from django.db.backends.signals import connection_created

    connection_created.connect(_verify_connection, dispatch_uid="acceptance_local_db_guard")


_connect_guard()


def _install_simulated_clock() -> None:
    """``timezone.now()`` = real time + an offset (zero unless ACCEPTANCE_CLOCK is set). In-process scripts can move
    it with ``set_simulated_clock``."""
    from datetime import timedelta

    from django.utils import timezone as dj_timezone

    real_now = dj_timezone.now
    state = {"offset": timedelta(0)}

    def now():
        return real_now() + state["offset"]

    now.acceptance_state = state
    now.acceptance_real_now = real_now
    dj_timezone.now = now


def set_simulated_clock(value) -> None:
    """Make ``timezone.now()`` equal ``value`` (ISO string with offset, or aware datetime) from this moment on."""
    from datetime import datetime

    from django.utils import timezone as dj_timezone

    if isinstance(value, str):
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?[+-]\d{2}:\d{2}$", value):
            raise ImproperlyConfigured("acceptance_local: the clock must look like 2026-10-28T21:05:00+05:30")
        value = datetime.fromisoformat(value)
    now = dj_timezone.now
    now.acceptance_state["offset"] = value - now.acceptance_real_now()


_install_simulated_clock()
ACCEPTANCE_CLOCK = os.environ.get("ACCEPTANCE_CLOCK", "").strip()
if ACCEPTANCE_CLOCK:
    set_simulated_clock(ACCEPTANCE_CLOCK)
