# ruff: noqa: ERA001, E501
"""Base settings to build other settings files upon."""

import logging
import os
import ssl
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve(strict=True).parent.parent.parent
# iic_booking/
APPS_DIR = BASE_DIR / "iic_booking"
env = environ.Env()

READ_DOT_ENV_FILE = env.bool("DJANGO_READ_DOT_ENV_FILE", default=True)
if READ_DOT_ENV_FILE:
    # OS environment variables take precedence over variables from .env
    env.read_env(str(BASE_DIR / ".env"))

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
DEBUG = env.bool("DJANGO_DEBUG", False)
# Local time zone. Choices are
# http://en.wikipedia.org/wiki/List_of_tz_zones_by_name
# though not all of them may be available with every OS.
# In Windows, this must be set to your system time zone.
TIME_ZONE = "Asia/Kolkata"
# https://docs.djangoproject.com/en/dev/ref/settings/#language-code
LANGUAGE_CODE = "en-us"
# https://docs.djangoproject.com/en/dev/ref/settings/#languages
# from django.utils.translation import gettext_lazy as _
# LANGUAGES = [
#     ('en', _('English')),
#     ('fr-fr', _('French')),
#     ('pt-br', _('Portuguese')),
# ]
# https://docs.djangoproject.com/en/dev/ref/settings/#site-id
SITE_ID = 1
# https://docs.djangoproject.com/en/dev/ref/settings/#use-i18n
USE_I18N = True
# https://docs.djangoproject.com/en/dev/ref/settings/#use-tz
USE_TZ = True
# https://docs.djangoproject.com/en/dev/ref/settings/#locale-paths
LOCALE_PATHS = [str(BASE_DIR / "locale")]

# DATABASES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#databases
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["ATOMIC_REQUESTS"] = True
# https://docs.djangoproject.com/en/stable/ref/settings/#std:setting-DEFAULT_AUTO_FIELD
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# BOOKING
# ------------------------------------------------------------------------------
# When True, weekly/monthly quota checks are skipped in book-equipment and related flows.
# Default False; enable only for local benchmarking via env or local.py override.
SKIP_BOOKING_QUOTA_CHECK = env.bool("SKIP_BOOKING_QUOTA_CHECK", default=False)
# Expose X-Booking-Perf on book-equipment responses; log slow bookings when True or DEBUG.
BOOKING_PERFORMANCE_TIMINGS = env.bool("BOOKING_PERFORMANCE_TIMINGS", default=DEBUG)

# URLS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#root-urlconf
ROOT_URLCONF = "config.urls"
# https://docs.djangoproject.com/en/dev/ref/settings/#wsgi-application
WSGI_APPLICATION = "config.wsgi.application"

# APPS
# ------------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.sites",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # "django.contrib.humanize", # Handy template tags
    "django.contrib.admin",
    "django.forms",
]
THIRD_PARTY_APPS = [
    "channels",
    "crispy_forms",
    "crispy_bootstrap5",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "allauth.socialaccount",
    "django_celery_beat",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "drf_spectacular",
]

LOCAL_APPS = [
    "iic_booking.users",
    "iic_booking.communication",
    "iic_booking.equipment",
    "iic_booking.support",
    "iic_booking.cms",
    # Your stuff: custom apps go here
]
# https://docs.djangoproject.com/en/dev/ref/settings/#installed-apps
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# MIGRATIONS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#migration-modules
MIGRATION_MODULES = {"sites": "iic_booking.contrib.sites.migrations"}

# AUTHENTICATION
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#authentication-backends
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
# https://docs.djangoproject.com/en/dev/ref/settings/#auth-user-model
AUTH_USER_MODEL = "users.User"
# https://docs.djangoproject.com/en/dev/ref/settings/#login-redirect-url
LOGIN_REDIRECT_URL = "users:redirect"
# https://docs.djangoproject.com/en/dev/ref/settings/#login-url
LOGIN_URL = "account_login"

# PASSWORDS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#password-hashers
PASSWORD_HASHERS = [
    # https://docs.djangoproject.com/en/dev/topics/auth/passwords/#using-argon2-with-django
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]
# https://docs.djangoproject.com/en/dev/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# MIDDLEWARE
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#middleware
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "config.middleware.AdminUnloadPermissionsPolicyMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "iic_booking.communication.middleware.CommunicationUserMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

# STATIC
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#static-root
STATIC_ROOT = str(BASE_DIR / "staticfiles")
# https://docs.djangoproject.com/en/dev/ref/settings/#static-url
STATIC_URL = "/static/"
# https://docs.djangoproject.com/en/dev/ref/contrib/staticfiles/#std:setting-STATICFILES_DIRS
STATICFILES_DIRS = [str(APPS_DIR / "static")]
# https://docs.djangoproject.com/en/dev/ref/contrib/staticfiles/#staticfiles-finders
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

# MEDIA
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#media-root
MEDIA_ROOT = str(APPS_DIR / "media")
# https://docs.djangoproject.com/en/dev/ref/settings/#media-url
MEDIA_URL = "/media/"

# AWS S3 Storage Configuration
# ------------------------------------------------------------------------------
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="iic-booking-s3")
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="ap-south-1")
# Enable signed URLs for secure access (instead of public access)
# Signed URLs expire after a specified time, providing time-limited access
# When True, django-storages will automatically generate signed URLs with query parameters
AWS_QUERYSTRING_AUTH = True
# Expiration time for signed URLs (in seconds). Max 604800 (7 days) for AWS SigV4.
# Use 7 days so equipment and profile images stay valid when API responses are cached or tab is left open.
AWS_S3_QUERYSTRING_EXPIRE = env.int("AWS_S3_SIGNED_URL_EXPIRATION", default=604800)  # 7 days

AWS_S3_OBJECT_PARAMETERS = {
    # Force clients/intermediaries to always fetch fresh from S3 for media files.
    "CacheControl": "no-store, max-age=0",
}
# Disable ACLs - modern S3 buckets have ACLs disabled by default
# Use signed URLs for access control instead
AWS_DEFAULT_ACL = None
AWS_S3_OBJECT_ACL = None
AWS_S3_FILE_OVERWRITE = False

# Storage Configuration
# Media URL for S3 - will be used as base, but actual URLs will be signed
# The storage backend will automatically generate signed URLs when url() is called
MEDIA_URL = f"https://{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.amazonaws.com/media/"

STORAGES = {
    "default": {
        # django-storages S3 backend
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {
            "location": "media",
            "file_overwrite": False,
        },
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# TEMPLATES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#templates
TEMPLATES = [
    {
        # https://docs.djangoproject.com/en/dev/ref/settings/#std:setting-TEMPLATES-BACKEND
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # https://docs.djangoproject.com/en/dev/ref/settings/#dirs
        "DIRS": [str(APPS_DIR / "templates")],
        # https://docs.djangoproject.com/en/dev/ref/settings/#app-dirs
        "APP_DIRS": True,
        "OPTIONS": {
            # https://docs.djangoproject.com/en/dev/ref/settings/#template-context-processors
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.i18n",
                "django.template.context_processors.media",
                "django.template.context_processors.static",
                "django.template.context_processors.tz",
                "django.contrib.messages.context_processors.messages",
                "iic_booking.users.context_processors.allauth_settings",
            ],
        },
    },
]

# https://docs.djangoproject.com/en/dev/ref/settings/#form-renderer
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

# http://django-crispy-forms.readthedocs.io/en/latest/install.html#template-packs
CRISPY_TEMPLATE_PACK = "bootstrap5"
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"

# FIXTURES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#fixture-dirs
FIXTURE_DIRS = (str(APPS_DIR / "fixtures"),)

# SECURITY
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-httponly
SESSION_COOKIE_HTTPONLY = True
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-cookie-httponly
CSRF_COOKIE_HTTPONLY = True
# https://docs.djangoproject.com/en/dev/ref/settings/#x-frame-options
X_FRAME_OPTIONS = "DENY"

# EMAIL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#email-backend
# Configure email backend - can use SMTP or anymail API
# Default to SMTP for SES (can be overridden via DJANGO_EMAIL_BACKEND env var)
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend",
)
# https://docs.djangoproject.com/en/dev/ref/settings/#email-timeout
EMAIL_TIMEOUT = env.int("DJANGO_EMAIL_TIMEOUT", default=15)

# SES SMTP Configuration
# ------------------------------------------------------------------------------
# Get SES region for SMTP endpoint
ses_region = env("AWS_S3_REGION_NAME", default="ap-south-1").strip()

# SES SMTP endpoint format: email-smtp.{region}.amazonaws.com
# https://docs.aws.amazon.com/ses/latest/dg/send-email-smtp.html
EMAIL_HOST = env(
    "AWS_SES_SMTP_HOST",
    default=f"email-smtp.{ses_region}.amazonaws.com",
)
EMAIL_PORT = env.int("AWS_SES_SMTP_PORT", default=587)
EMAIL_USE_TLS = env.bool("AWS_SES_SMTP_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("AWS_SES_SMTP_USE_SSL", default=False)

# SES SMTP credentials (username and password from IAM user)
# Get these from AWS IAM Console -> Users -> ses-smtp-user -> Security credentials -> SMTP credentials
# If AWS_SES_SMTP_PASSWORD contains + or /, put it in double quotes in .env: AWS_SES_SMTP_PASSWORD="your+pass/word"
EMAIL_HOST_USER = env("AWS_SES_SMTP_USERNAME", default="")
EMAIL_HOST_PASSWORD = env("AWS_SES_SMTP_PASSWORD", default="")

# Default from email address
# IMPORTANT: This email address MUST be verified in AWS SES before sending emails
# Go to AWS SES Console -> Verified identities -> Create identity (verify email or domain)
# https://docs.djangoproject.com/en/dev/ref/settings/#default-from-email
DEFAULT_FROM_EMAIL = env(
    "DJANGO_DEFAULT_FROM_EMAIL",
    default="IIC Booking <no-reply@iicbooking.iitr.ac.in>",
)
# https://docs.djangoproject.com/en/dev/ref/settings/#server-email
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)

# DOCUMENTS (Invoice / Shipping label)
# ------------------------------------------------------------------------------
# Organization "From" details used in generated invoices and shipping labels.
ORG_LEGAL_NAME = env("ORG_LEGAL_NAME", default="IIC Booking, IIT Roorkee")
ORG_GSTIN = env("ORG_GSTIN", default="")
ORG_ADDRESS = env("ORG_ADDRESS", default="Indian Institute of Technology Roorkee, Roorkee, Uttarakhand, India")
ORG_CONTACT_EMAIL = env("ORG_CONTACT_EMAIL", default="")  # optional
ORG_CONTACT_PHONE = env("ORG_CONTACT_PHONE", default="")  # optional

# OIC self-leave intimation: extra addresses (comma/semicolon separated) in addition to co-OICs and department head.
OIC_LEAVE_INTIMATION_EXTRA_EMAILS = env("OIC_LEAVE_INTIMATION_EXTRA_EMAILS", default="")

# Shipping label: "From" address block (editable; one line per entry, separate with \n in .env).
# Used as the sender address on the envelope. Default: IIC Roorkee postal address.
SHIPPING_LABEL_FROM_ADDRESS = env(
    "SHIPPING_LABEL_FROM_ADDRESS",
    default="The Head,\nInstitute Instrumentation Centre,\nIndian Institute of Technology Roorkee\nRoorkee - 247667 (Uttarakhand) India",
)

# Warn if using SMTP backend but credentials are missing (emails will fail)
if "smtp" in EMAIL_BACKEND.lower() and (not EMAIL_HOST_USER or not EMAIL_HOST_PASSWORD):
    logging.getLogger(__name__).warning(
        "Outgoing email may fail: AWS_SES_SMTP_USERNAME and/or AWS_SES_SMTP_PASSWORD are not set. "
        "Set them in .env for SES SMTP, or use DJANGO_EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend for local testing."
    )

# Optional: Anymail configuration (if you want to use API instead of SMTP)
# Uncomment and configure if you want to switch to anymail API backend
# INSTALLED_APPS += ["anymail"]
# ANYMAIL = {
#     "AMAZON_SES_AWS_ACCESS_KEY_ID": env("AWS_SES_ACCESS_KEY_ID", default=""),
#     "AMAZON_SES_AWS_SECRET_ACCESS_KEY": env("AWS_SES_SECRET_ACCESS_KEY", default=""),
#     "AMAZON_SES_REGION": ses_region,
# }

# IMAP (incoming email) configuration
# ------------------------------------------------------------------------------
# Used to read email from a mailbox (e.g. ravis.mic2014@iitr.ac.in via imap.iitr.ac.in)
IMAP_HOST = env("IMAP_HOST", default="mapi.iitr.ac.in")
IMAP_PORT = env.int("IMAP_PORT", default=993)
IMAP_USE_SSL = env.bool("IMAP_USE_SSL", default=True)
IMAP_USER = env("IMAP_USER", default="")
IMAP_PASSWORD = env("IMAP_PASSWORD", default="")
IMAP_MAILBOX = env("IMAP_MAILBOX", default="INBOX")

# Legacy IIC booking MySQL (read-only wallet lookup: user + user_wallet)
# ------------------------------------------------------------------------------
# On the legacy AWS host use 127.0.0.1; from dev use the server IP if the security group allows.
LEGACY_MYSQL_HOST = env("LEGACY_MYSQL_HOST", default="")
LEGACY_MYSQL_PORT = env.int("LEGACY_MYSQL_PORT", default=3306)
LEGACY_MYSQL_USER = env("LEGACY_MYSQL_USER", default="")
LEGACY_MYSQL_PASSWORD = env("LEGACY_MYSQL_PASSWORD", default="")
LEGACY_MYSQL_DATABASE = env("LEGACY_MYSQL_DATABASE", default="admin")

# ADMIN
# ------------------------------------------------------------------------------
# Django Admin URL.
ADMIN_URL = "admin/"
# https://docs.djangoproject.com/en/dev/ref/settings/#admins
ADMINS = [("""Harshit Mittal""", "harshitmittal260@gmail.com")]
# https://docs.djangoproject.com/en/dev/ref/settings/#managers
MANAGERS = ADMINS
# https://cookiecutter-django.readthedocs.io/en/latest/settings.html#other-environment-settings
# Force the `admin` sign in process to go through the `django-allauth` workflow
DJANGO_ADMIN_FORCE_ALLAUTH = env.bool("DJANGO_ADMIN_FORCE_ALLAUTH", default=False)

# LOGGING
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#logging
# See https://docs.djangoproject.com/en/dev/topics/logging for
# more details on how to customize your logging configuration.
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

REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")
REDIS_SSL = REDIS_URL.startswith("rediss://")

# Celery
# ------------------------------------------------------------------------------
if USE_TZ:
    # https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-timezone
    CELERY_TIMEZONE = TIME_ZONE
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-broker_url
CELERY_BROKER_URL = REDIS_URL
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#redis-backend-use-ssl
CELERY_BROKER_USE_SSL = {"ssl_cert_reqs": ssl.CERT_NONE} if REDIS_SSL else None
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-result_backend
CELERY_RESULT_BACKEND = REDIS_URL
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#redis-backend-use-ssl
CELERY_REDIS_BACKEND_USE_SSL = CELERY_BROKER_USE_SSL
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#result-extended
CELERY_RESULT_EXTENDED = True
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#result-backend-always-retry
# https://github.com/celery/celery/pull/6122
CELERY_RESULT_BACKEND_ALWAYS_RETRY = True
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#result-backend-max-retries
CELERY_RESULT_BACKEND_MAX_RETRIES = 10
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-accept_content
CELERY_ACCEPT_CONTENT = ["json"]
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-task_serializer
CELERY_TASK_SERIALIZER = "json"
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-result_serializer
CELERY_RESULT_SERIALIZER = "json"
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-time-limit
# TODO: set to whatever value is adequate in your circumstances
CELERY_TASK_TIME_LIMIT = 5 * 60
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-soft-time-limit
# TODO: set to whatever value is adequate in your circumstances
CELERY_TASK_SOFT_TIME_LIMIT = 60
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#beat-scheduler
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#worker-send-task-events
CELERY_WORKER_SEND_TASK_EVENTS = True
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std-setting-task_send_sent_event
CELERY_TASK_SEND_SENT_EVENT = True
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#worker-hijack-root-logger
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
# django-allauth
# ------------------------------------------------------------------------------
ACCOUNT_ALLOW_REGISTRATION = env.bool("DJANGO_ACCOUNT_ALLOW_REGISTRATION", True)
# https://docs.allauth.org/en/latest/account/configuration.html
ACCOUNT_LOGIN_METHODS = {"email"}
# https://docs.allauth.org/en/latest/account/configuration.html
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
# https://docs.allauth.org/en/latest/account/configuration.html
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
# https://docs.allauth.org/en/latest/account/configuration.html
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
# https://docs.allauth.org/en/latest/account/configuration.html
ACCOUNT_ADAPTER = "iic_booking.users.adapters.AccountAdapter"
# https://docs.allauth.org/en/latest/account/forms.html
ACCOUNT_FORMS = {"signup": "iic_booking.users.forms.UserSignupForm"}
# https://docs.allauth.org/en/latest/socialaccount/configuration.html
SOCIALACCOUNT_ADAPTER = "iic_booking.users.adapters.SocialAccountAdapter"
# https://docs.allauth.org/en/latest/socialaccount/configuration.html
SOCIALACCOUNT_FORMS = {"signup": "iic_booking.users.forms.UserSocialSignupForm"}

# django-rest-framework
# -------------------------------------------------------------------------------
# django-rest-framework - https://www.django-rest-framework.org/api-guide/settings/
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework.authentication.SessionAuthentication",
        "iic_booking.users.api.token_auth.TokenAuthenticationWithInactivity",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}
# Inactivity logout disabled: tokens do not expire due to inactivity.
# AUTH_INACTIVITY_TIMEOUT_SECONDS is kept for backwards compatibility but not used.
AUTH_INACTIVITY_TIMEOUT_SECONDS = env.int("AUTH_INACTIVITY_TIMEOUT_SECONDS", default=1800)

# django-cors-headers - https://github.com/adamchainz/django-cors-headers#setup
CORS_URLS_REGEX = r"^/api/.*$"
# Allow CORS for React frontend
# CORS_ALLOWED_ORIGINS expects comma-separated list: "http://localhost:3000,http://127.0.0.1:8080"
CORS_ALLOWED_ORIGINS = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]
CORS_ALLOW_CREDENTIALS = True
CORS_EXPOSE_HEADERS = ["X-Booking-Perf", "x-booking-perf"]

# By Default swagger ui is available only to admin user(s). You can change permission classes to change that
# See more configuration options at https://drf-spectacular.readthedocs.io/en/latest/settings.html#settings
SPECTACULAR_SETTINGS = {
    "TITLE": "IIC Booking API",
    "DESCRIPTION": "Documentation of API endpoints of IIC Booking",
    "VERSION": "1.0.0",
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAdminUser"],
    "SCHEMA_PATH_PREFIX": "/api/",
}
# Your stuff...
# ------------------------------------------------------------------------------


# Omniport 
# ------------------------------------------------------------------------------
OMNIPORT_CLIENT_ID = env("OMNIPORT_CLIENT_ID", default="")
OMNIPORT_CLIENT_SECRET = env("OMNIPORT_CLIENT_SECRET", default="")

OMNIPORT_AUTH_URL = env(
    "OMNIPORT_AUTH_URL",
    default="https://channeli.in/oauth/authorise/",
)
OMNIPORT_TOKEN_URL = env(
    "OMNIPORT_TOKEN_URL",
    default="https://channeli.in/open_auth/token/",
)
OMNIPORT_USERINFO_URL = env(
    "OMNIPORT_USERINFO_URL",
    default="https://channeli.in/open_auth/get_user_data/",
)
OMNIPORT_LOGOUT_URL = env(
    "OMNIPORT_LOGOUT_URL",
    default="https://channeli.in/accounts/logout/",
)
# Base URL of the frontend app. Used for redirects after auth and for links in emails
# (booking confirmation, wallet balance, results available, etc.). Must match where users access the app.
FRONTEND_URL = env(
    "FRONTEND_URL",
    default="http://localhost:8080",
)
# Optional: for AI-powered chat assistant. If set, chat uses OpenAI; otherwise falls back to FAQ.
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
OPENAI_CHAT_MODEL = env("OPENAI_CHAT_MODEL", default="gpt-4o-mini")
OMNIPORT_REDIRECT_URI = env(
    "OMNIPORT_REDIRECT_URI",
    default="http://localhost:8000/api/auth/omniport/callback/",
)

# Django Channels Configuration
# ------------------------------------------------------------------------------
ASGI_APPLICATION = "config.asgi.application"

# Channel layers configuration for WebSocket support
# Use a separate Redis URL for channels to avoid conflicts with Celery
CHANNELS_REDIS_URL = env("CHANNELS_REDIS_URL", default="redis://127.0.0.1:6379/1")
# Only in local dev: use localhost so Django on host can reach Redis (Docker hostname 'redis' not reachable)
# In production (DEBUG=False) keep redis://redis:6379/1 so Django in container reaches Redis container
if DEBUG and "redis://redis:" in CHANNELS_REDIS_URL:
    CHANNELS_REDIS_URL = CHANNELS_REDIS_URL.replace("redis://redis:", "redis://127.0.0.1:")

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [CHANNELS_REDIS_URL],
        },
    },
}


RAZORPAY_KEY_ID = env("RAZORPAY_KEY_ID", default="")
RAZORPAY_KEY_SECRET = env("RAZORPAY_KEY_SECRET", default="")

# SBIePay (replaces Razorpay for online payments)
SBIEPAY_MERCHANT_ID = env("SBIEPAY_MERCHANT_ID", default="")
SBIEPAY_ENCRYPTION_KEY = env("SBIEPAY_ENCRYPTION_KEY", default="")
SBIEPAY_GATEWAY_URL = env(
    "SBIEPAY_GATEWAY_URL",
    default="https://test.sbiepay.sbi/secure/AggregatorHostedListener",
)
SBIEPAY_SUCCESS_URL = env("SBIEPAY_SUCCESS_URL", default="")
SBIEPAY_FAILURE_URL = env("SBIEPAY_FAILURE_URL", default="")
SBIEPAY_PUSH_URL = env("SBIEPAY_PUSH_URL", default="")

# SRIC office integration API (replaces email when key is set)
SRIC_API_KEY = env("SRIC_API_KEY", default="")
SRIC_EMAIL_FALLBACK = env.bool("SRIC_EMAIL_FALLBACK", default=False)

# Accounts Email Configuration
# ------------------------------------------------------------------------------
# Email address for accounts team to receive wallet recharge requests
ACCOUNTS_EMAIL = env("ACCOUNTS_EMAIL", default="iicbooking@iitr.ac.in")

# 3D printing / STL analysis
# ------------------------------------------------------------------------------
CURAENGINE_PATH = env("CURAENGINE_PATH", default="")
PRINT_3D_USE_CURAENGINE = env.bool("PRINT_3D_USE_CURAENGINE", default=True)
PRINT_3D_MAX_STL_BYTES = env.int("PRINT_3D_MAX_STL_BYTES", default=100 * 1024 * 1024)
PRINT_3D_USE_CELERY = env.bool("PRINT_3D_USE_CELERY", default=True)