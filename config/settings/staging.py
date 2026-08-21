# ruff: noqa: E501
"""Staging settings — isolated non-production environment.

Hard rule: refuse known production database hosts / names.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import APPS_DIR
from .base import BASE_DIR
from .base import DATABASES
from .base import INSTALLED_APPS
from .base import REDIS_URL
from .base import SPECTACULAR_SETTINGS
from .base import env

logger = logging.getLogger(__name__)

# GENERAL
# ------------------------------------------------------------------------------
DEBUG = env.bool("DJANGO_DEBUG", default=False)
SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS",
    default=[
        "localhost",
        "127.0.0.1",
        "staging.equip.iitr.ac.in",
        "iic-booking-staging",
    ],
)

# Explicit environment marker for audits / UI banners
DEPLOYMENT_ENVIRONMENT = "STAGING"
ENVIRONMENT_LABEL = "IIC Booking — STAGING"

# DATABASES
# ------------------------------------------------------------------------------
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)

# Known production identifiers — refuse if staging env accidentally points here.
_PRODUCTION_DB_HOST_MARKERS = (
    "iic-booking-rds.cvs75htsmowj.ap-south-1.rds.amazonaws.com",
)
_PRODUCTION_DB_NAMES = set()  # empty: name alone is weak; host markers are decisive
_PRODUCTION_FRONTEND_MARKERS = (
    "https://equip.iitr.ac.in",
    "http://equip.iitr.ac.in",
)


def _assert_not_production_database() -> None:
    database_url = env("DATABASE_URL", default="")
    if not database_url:
        raise ImproperlyConfigured(
            "STAGING requires DATABASE_URL pointing at an isolated staging database."
        )
    parsed = urlparse(database_url)
    host = (parsed.hostname or "").lower()
    db_name = (parsed.path or "").lstrip("/").lower()

    for marker in _PRODUCTION_DB_HOST_MARKERS:
        if marker.lower() in host or marker.lower() in database_url.lower():
            raise ImproperlyConfigured(
                "STAGING SAFETY STOP: DATABASE_URL points at production RDS "
                f"({marker}). Provision an isolated staging database."
            )

    # Refuse the bare production DB name only when host also looks like RDS.
    if db_name == "postgres" and ("rds.amazonaws.com" in host or "iic-booking-rds" in host):
        raise ImproperlyConfigured(
            "STAGING SAFETY STOP: refusing production-style RDS database name "
            f"'postgres' on host {host}."
        )

    # Prefer explicit staging DB name.
    expected = env("STAGING_EXPECTED_DB_NAME", default="iic_booking_staging")
    if expected and db_name and db_name != expected.lower():
        logger.warning(
            "Staging DB name is %r; recommended name is %r",
            db_name,
            expected,
        )


def _assert_not_production_frontend() -> None:
    frontend = env("FRONTEND_URL", default="")
    for marker in _PRODUCTION_FRONTEND_MARKERS:
        if frontend.rstrip("/") == marker.rstrip("/"):
            raise ImproperlyConfigured(
                "STAGING SAFETY STOP: FRONTEND_URL must not be production "
                "https://equip.iitr.ac.in"
            )


_assert_not_production_database()
_assert_not_production_frontend()

# CACHES
# ------------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": True,
        },
    },
}

# SECURITY — staging often HTTP on localhost; SSL optional via env
# ------------------------------------------------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = env.list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    default=[
        "http://localhost:8100",
        "http://127.0.0.1:8100",
        "https://staging.equip.iitr.ac.in",
    ],
)
# Local staging frontend is :8100 (not base.py's :8080). Without this, browsers
# show "Failed to fetch" after a successful Omniport redirect to /auth/callback.
CORS_ALLOWED_ORIGINS = env.list(
    "DJANGO_CORS_ALLOWED_ORIGINS",
    default=[
        "http://localhost:8100",
        "http://127.0.0.1:8100",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ],
)
CORS_ALLOW_CREDENTIALS = True
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=False)
SESSION_COOKIE_SECURE = SECURE_SSL_REDIRECT
CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT
SESSION_COOKIE_NAME = "__Secure-sessionid" if SECURE_SSL_REDIRECT else "staging-sessionid"
CSRF_COOKIE_NAME = "__Secure-csrftoken" if SECURE_SSL_REDIRECT else "staging-csrftoken"
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=0)

# EMAIL — never silently use production SMTP; console/mailpit preferred
# ------------------------------------------------------------------------------
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    default="django.core.mail.backends.console.EmailBackend",
)
DEFAULT_FROM_EMAIL = env(
    "DJANGO_DEFAULT_FROM_EMAIL",
    default="IIC Booking STAGING <noreply-staging@localhost>",
)
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)
EMAIL_SUBJECT_PREFIX = env("DJANGO_EMAIL_SUBJECT_PREFIX", default="[STAGING] ")

# ADMIN / SPECTACULAR
# ------------------------------------------------------------------------------
SPECTACULAR_SETTINGS = {
    **SPECTACULAR_SETTINGS,
    "TITLE": "IIC Booking API (STAGING)",
}

# MEDIA — LOCAL_STAGING vs S3 (never use production bucket by accident)
# ------------------------------------------------------------------------------
STAGING_STORAGE_BACKEND = env("STAGING_STORAGE_BACKEND", default="LOCAL_STAGING")
USE_S3_MEDIA = env.bool("USE_S3_MEDIA", default=False)
# Formal operator acceptance that LOCAL_STAGING is an intentional staging limitation.
# Does NOT make S3 a PASS; removes S3 NOT_AVAILABLE as a GO blocker when true.
LOCAL_STAGING_ACCEPTED = env.bool("LOCAL_STAGING_ACCEPTED", default=False)
if STAGING_STORAGE_BACKEND.upper() == "S3" or USE_S3_MEDIA:
    USE_S3_MEDIA = True
else:
    USE_S3_MEDIA = False
    DEFAULT_FILE_STORAGE = "django.core.files.storage.FileSystemStorage"
    ALLOW_LOCAL_EQUIPMENT_IMAGE_FALLBACK = True
    MEDIA_ROOT = str(APPS_DIR / "media" / "staging_media")

# Copilot safe envelope (staging defaults)
# ------------------------------------------------------------------------------
RESEARCH_COPILOT_ENABLED = env.bool("RESEARCH_COPILOT_ENABLED", default=True)
# PUBLIC false via empty pilot list + require pilot emails when set
RESEARCH_COPILOT_PILOT_EMAILS = env("RESEARCH_COPILOT_PILOT_EMAILS", default="")
RESEARCH_COPILOT_MAX_CONCURRENT = env.int("RESEARCH_COPILOT_MAX_CONCURRENT", default=1)
RESEARCH_COPILOT_MAX_TOKENS = env.int("RESEARCH_COPILOT_MAX_TOKENS", default=160)
OLLAMA_MODEL = env("OLLAMA_MODEL", default="llama3.2:1b")

# RAA / DSA — do not inherit production agents; default off enrollment reuse
# ------------------------------------------------------------------------------
RA_APPLY_ENV_SETTINGS = env.bool("RA_APPLY_ENV_SETTINGS", default=False)
RA_SKIP_GATEWAY_READY = env.bool("RA_SKIP_GATEWAY_READY", default=True)

# Feature flags — migration-sensitive OFF initially
# ------------------------------------------------------------------------------
STUDENT_LIFECYCLE_ENABLED = env.bool("STUDENT_LIFECYCLE_ENABLED", default=False)
DEPARTMENT_MAPPING_ENABLED = env.bool("DEPARTMENT_MAPPING_ENABLED", default=False)

# Channel-I — never silently reuse production secrets
# ------------------------------------------------------------------------------
# Explicit live-integration intent. When true, fixture modes must be off and
# missing credentials must BLOCK (never silent fixture success).
REAL_INTEGRATION_ENABLED = env.bool("REAL_INTEGRATION_ENABLED", default=False)
CHANNEL_I_STAGING_FIXTURE_MODE = env.bool("CHANNEL_I_STAGING_FIXTURE_MODE", default=False)
# Operator aliases (already in base as OMNIPORT_*):
# CHANNEL_I_CLIENT_ID -> OMNIPORT_CLIENT_ID
# CHANNEL_I_CLIENT_SECRET -> OMNIPORT_CLIENT_SECRET
# CHANNEL_I_CALLBACK_URL -> OMNIPORT_REDIRECT_URI
# CHANNEL_I_AUTHORIZATION_URL -> OMNIPORT_AUTH_URL
# CHANNEL_I_TOKEN_URL -> OMNIPORT_TOKEN_URL
# CHANNEL_I_USERINFO_URL -> OMNIPORT_USERINFO_URL

# Legacy MySQL — fixture mode vs real RO credentials
# ------------------------------------------------------------------------------
LEGACY_MYSQL_STAGING_FIXTURE_MODE = env.bool("LEGACY_MYSQL_STAGING_FIXTURE_MODE", default=False)
LEGACY_MYSQL_STAGING_FIXTURE_PATH = env(
    "LEGACY_MYSQL_STAGING_FIXTURE_PATH",
    default=str(BASE_DIR / "iic_booking" / "users" / "fixtures" / "staging_legacy_snapshot.json"),
)

# Logging
# ------------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(module)s %(process)d %(thread)d %(message)s",
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
}

INSTALLED_APPS += ["whitenoise.runserver_nostatic"]  # noqa: F405
