from .base import *  # noqa: F403
from .base import INSTALLED_APPS
from .base import MIDDLEWARE
from .base import env

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
DEBUG = True
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="2inj6sSD7LwIN5aTcUjTJkxhRqRYimCXlXs0TtATLpgoy47W56vF4YQzql7mRbCf",
)
# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
ALLOWED_HOSTS = ["localhost", "0.0.0.0", "127.0.0.1"]  # noqa: S104

# CACHES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#caches
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "",
    },
}

# EMAIL
# ------------------------------------------------------------------------------
# SES configuration is in base.py
# For local development, you can override to use console backend:
# Set DJANGO_EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend in .env
# If you want to use mailpit (Docker), set EMAIL_HOST in your .env file
# EMAIL_HOST = env("EMAIL_HOST", default="mailpit")
# EMAIL_PORT = env.int("EMAIL_PORT", default=1025)

# WhiteNoise
# ------------------------------------------------------------------------------
# http://whitenoise.evans.io/en/latest/django.html#using-whitenoise-in-development
INSTALLED_APPS = ["whitenoise.runserver_nostatic", *INSTALLED_APPS]


# django-debug-toolbar
# ------------------------------------------------------------------------------
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#prerequisites
INSTALLED_APPS += ["debug_toolbar"]
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#middleware
MIDDLEWARE += ["debug_toolbar.middleware.DebugToolbarMiddleware"]
# https://django-debug-toolbar.readthedocs.io/en/latest/configuration.html#debug-toolbar-config
DEBUG_TOOLBAR_CONFIG = {
    "DISABLE_PANELS": [
        "debug_toolbar.panels.redirects.RedirectsPanel",
        # Disable profiling panel due to an issue with Python 3.12+:
        # https://github.com/jazzband/django-debug-toolbar/issues/1875
        "debug_toolbar.panels.profiling.ProfilingPanel",
    ],
    "SHOW_TEMPLATE_CONTEXT": True,
}
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#internal-ips
INTERNAL_IPS = ["127.0.0.1", "10.0.2.2"]
if env("USE_DOCKER") == "yes":
    import socket

    try:
        hostname, _, ips = socket.gethostbyname_ex(socket.gethostname())
    except socket.gaierror:
        ips = ["127.0.0.1"]
    # hostname, _, ips = socket.gethostbyname_ex(socket.gethostname())
    INTERNAL_IPS += [".".join([*ip.split(".")[:-1], "1"]) for ip in ips]

# django-extensions
# ------------------------------------------------------------------------------
# https://django-extensions.readthedocs.io/en/latest/installation_instructions.html#configuration
INSTALLED_APPS += ["django_extensions"]
# Celery
# ------------------------------------------------------------------------------

# https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-eager-propagates
CELERY_TASK_EAGER_PROPAGATES = True
# Run Celery tasks synchronously in local dev (no Redis required).
CELERY_TASK_ALWAYS_EAGER = True
# Avoid trying to publish task results to Redis in eager mode.
CELERY_TASK_STORE_EAGER_RESULT = False
# Use in-memory broker/backend so any accidental async call doesn't hit Redis.
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
# Run STL analysis inline locally (no Celery worker required).
PRINT_3D_USE_CELERY = env.bool("PRINT_3D_USE_CELERY", default=False)
# Faster dev UX: heuristic estimates only (skip CuraEngine slicing).
PRINT_3D_USE_CURAENGINE = env.bool("PRINT_3D_USE_CURAENGINE", default=False)
# Faster dev UX: don't block request while analyzing; frontend polls status.
PRINT_3D_ASYNC_INLINE = env.bool("PRINT_3D_ASYNC_INLINE", default=True)
# Storage
# ------------------------------------------------------------------------------
# Local development defaults to filesystem storage (fast, predictable).
# Set USE_S3_MEDIA=1 in .env if you explicitly want S3 locally.
if not env.bool("USE_S3_MEDIA", default=False):
    MEDIA_URL = "/media/"
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    # Local filesystem is the source of truth when not using S3.
    ALLOW_LOCAL_EQUIPMENT_IMAGE_FALLBACK = True
# Your stuff...
# ------------------------------------------------------------------------------
# Skip quota checks by default in local dev (faster booking UX / benchmarking).
# Set SKIP_BOOKING_QUOTA_CHECK=0 in .env to enforce quotas locally.
SKIP_BOOKING_QUOTA_CHECK = env.bool("SKIP_BOOKING_QUOTA_CHECK", default=True)
BOOKING_PERFORMANCE_TIMINGS = env.bool("BOOKING_PERFORMANCE_TIMINGS", default=True)
