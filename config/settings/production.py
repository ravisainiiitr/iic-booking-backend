# ruff: noqa: E501
import logging

import sentry_sdk
from django.core.exceptions import ImproperlyConfigured
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.redis import RedisIntegration

from .base import *  # noqa: F403
from .base import DATABASES
from .base import INSTALLED_APPS
from .base import REDIS_URL
from .base import SPECTACULAR_SETTINGS
from .base import env
from .base import AWS_SES_ACCESS_KEY_ID
from .base import AWS_SES_SECRET_ACCESS_KEY
from .base import ses_region

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env("DJANGO_SECRET_KEY")
# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
# Include API server IP so WebSocket (e.g. ws://15.206.88.2:8080/ws/notifications/) Origin is accepted
ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS",
    default=["*.iicbooking.iitr.ac.in", "15.206.88.2", "localhost", "127.0.0.1"],
)

# Quota limits must always apply in production (ignore any SKIP_BOOKING_QUOTA_CHECK in env).
SKIP_BOOKING_QUOTA_CHECK = False

# DATABASES
# ------------------------------------------------------------------------------
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)

# CACHES
# ------------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            # Mimicking memcache behavior.
            # https://github.com/jazzband/django-redis#memcached-exceptions-behavior
            "IGNORE_EXCEPTIONS": True,
        },
    },
}

# SECURITY
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-proxy-ssl-header
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Required when running behind a proxy (e.g. Traefik) for HTTPS origins
CSRF_TRUSTED_ORIGINS = env.list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    default=["https://localhost", "https://127.0.0.1", "https://iicbooking.iitr.ac.in", "https://www.iicbooking.iitr.ac.in"],
)
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-ssl-redirect
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-secure
# Only require secure cookies when SSL redirect is enabled (i.e., HTTPS-only)
SESSION_COOKIE_SECURE = SECURE_SSL_REDIRECT
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-name
SESSION_COOKIE_NAME = "__Secure-sessionid" if SECURE_SSL_REDIRECT else "sessionid"
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-cookie-secure
# Only require secure cookies when SSL redirect is enabled (i.e., HTTPS-only)
CSRF_COOKIE_SECURE = SECURE_SSL_REDIRECT
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-cookie-name
CSRF_COOKIE_NAME = "__Secure-csrftoken" if SECURE_SSL_REDIRECT else "csrftoken"
# https://docs.djangoproject.com/en/dev/topics/security/#ssl-https
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-seconds
# TODO: set this to 60 seconds first and then to 518400 once you prove the former works
SECURE_HSTS_SECONDS = 60
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-include-subdomains
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS",
    default=True,
)
# https://docs.djangoproject.com/en/dev/ref/settings/#secure-hsts-preload
SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=True)
# https://docs.djangoproject.com/en/dev/ref/middleware/#x-content-type-options-nosniff
SECURE_CONTENT_TYPE_NOSNIFF = env.bool(
    "DJANGO_SECURE_CONTENT_TYPE_NOSNIFF",
    default=True,
)

# EMAIL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#default-from-email
DEFAULT_FROM_EMAIL = env(
    "DJANGO_DEFAULT_FROM_EMAIL",
    default="IIC Booking <no-reply@iicbooking.iitr.ac.in>",
)
# https://docs.djangoproject.com/en/dev/ref/settings/#server-email
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)
# https://docs.djangoproject.com/en/dev/ref/settings/#email-subject-prefix
EMAIL_SUBJECT_PREFIX = env(
    "DJANGO_EMAIL_SUBJECT_PREFIX",
    default="[IIC Booking] ",
)
ACCOUNT_EMAIL_SUBJECT_PREFIX = EMAIL_SUBJECT_PREFIX

# ADMIN
# ------------------------------------------------------------------------------
# Django Admin URL regex.
ADMIN_URL = env("DJANGO_ADMIN_URL")

# EMAIL
# ------------------------------------------------------------------------------
# Production uses IITR SMTP (nsmtp.iitr.ac.in:587 STARTTLS) — verified working on EC2.
# Set USE_AWS_SES_API=True only if switching back to AWS SES later.
USE_AWS_SES_API = env.bool("USE_AWS_SES_API", default=False)

if USE_AWS_SES_API:
    EMAIL_BACKEND = "anymail.backends.amazon_ses.EmailBackend"
    if "anymail" not in INSTALLED_APPS:
        INSTALLED_APPS += ["anymail"]
    if not (AWS_SES_ACCESS_KEY_ID and AWS_SES_SECRET_ACCESS_KEY):
        raise ImproperlyConfigured(
            "USE_AWS_SES_API=True requires AWS_SES_ACCESS_KEY_ID and AWS_SES_SECRET_ACCESS_KEY."
        )
    ANYMAIL = {
        "AMAZON_SES_CLIENT_PARAMS": {
            "aws_access_key_id": AWS_SES_ACCESS_KEY_ID,
            "aws_secret_access_key": AWS_SES_SECRET_ACCESS_KEY,
            "region_name": ses_region,
        },
    }
else:
    EMAIL_BACKEND = env(
        "DJANGO_EMAIL_BACKEND",
        default="django.core.mail.backends.smtp.EmailBackend",
    )
    EMAIL_HOST = env("AWS_SES_SMTP_HOST", default="nsmtp.iitr.ac.in")
    EMAIL_PORT = env.int("AWS_SES_SMTP_PORT", default=587)
    EMAIL_USE_TLS = env.bool("AWS_SES_SMTP_USE_TLS", default=True)
    EMAIL_USE_SSL = env.bool("AWS_SES_SMTP_USE_SSL", default=False)
    EMAIL_HOST_USER = env("AWS_SES_SMTP_USERNAME", default="")
    EMAIL_HOST_PASSWORD = env("AWS_SES_SMTP_PASSWORD", default="")
    if not EMAIL_HOST_USER or not EMAIL_HOST_PASSWORD:
        logging.getLogger(__name__).warning(
            "Production SMTP: AWS_SES_SMTP_USERNAME / AWS_SES_SMTP_PASSWORD missing in "
            ".envs/.production/.django — outbound email will fail."
        )


# LOGGING
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#logging
# See https://docs.djangoproject.com/en/dev/topics/logging for
# more details on how to customize your logging configuration.

LOGGING = {
    "version": 1,
    "disable_existing_loggers": True,
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
    "loggers": {
        "django.db.backends": {
            "level": "ERROR",
            "handlers": ["console"],
            "propagate": False,
        },
        # Errors logged by the SDK itself
        "sentry_sdk": {"level": "ERROR", "handlers": ["console"], "propagate": False},
        "django.security.DisallowedHost": {
            "level": "ERROR",
            "handlers": ["console"],
            "propagate": False,
        },
    },
}

# Sentry
# ------------------------------------------------------------------------------
if env("SENTRY_DSN", default="") != "":
    SENTRY_DSN = env("SENTRY_DSN")
    SENTRY_LOG_LEVEL = env.int("DJANGO_SENTRY_LOG_LEVEL", logging.INFO)

    sentry_logging = LoggingIntegration(
        level=SENTRY_LOG_LEVEL,  # Capture info and above as breadcrumbs
        event_level=logging.ERROR,  # Send errors as events
    )
    integrations = [
        sentry_logging,
        DjangoIntegration(),
        CeleryIntegration(),
        RedisIntegration(),
    ]
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=integrations,
        environment=env("SENTRY_ENVIRONMENT", default="production"),
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
    )

# django-cors-headers - Production
# -------------------------------------------------------------------------------
# Allow CORS for React frontend in production
# Set DJANGO_CORS_ALLOWED_ORIGINS in your environment variables
# Example: DJANGO_CORS_ALLOWED_ORIGINS=http://15.206.88.2,https://yourdomain.com
CORS_ALLOWED_ORIGINS = env.list(
    "DJANGO_CORS_ALLOWED_ORIGINS",
    default=[],
)
# Allow credentials (cookies, authorization headers) in CORS requests
CORS_ALLOW_CREDENTIALS = True

# django-rest-framework
# -------------------------------------------------------------------------------
# Tools that generate code samples can use SERVERS to point to the correct domain
SPECTACULAR_SETTINGS["SERVERS"] = [
    {"url": "https://iicbooking.iitr.ac.in", "description": "Production server"},
]
# Your stuff...
# ------------------------------------------------------------------------------
# Equipment images must live in S3 (or another durable store). Never treat
# container-local MEDIA_ROOT backups as sufficient in production.
ALLOW_LOCAL_EQUIPMENT_IMAGE_FALLBACK = False

if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY):
    logging.getLogger(__name__).warning(
        "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are empty. "
        "Equipment images will disappear after redeploy unless instance IAM "
        "can write to S3 bucket %s.",
        AWS_STORAGE_BUCKET_NAME,
    )
