"""Print what the acceptance environment points at (no secrets) and fail unless it is the local disposable target."""

import socket
import sys
from pathlib import Path

from _bootstrap import setup

settings = setup()

from django.db import connection  # noqa: E402

from config.settings.acceptance_guards import OutboundNetworkBlocked  # noqa: E402
from django.utils import timezone  # noqa: E402

db = settings.DATABASES["default"]
with connection.cursor() as cur:
    cur.execute("select coalesce(host(inet_server_addr()), 'socket'), current_setting('port'), current_database()")
    server_host, server_port, server_db = cur.fetchone()

checks = {
    "settings module": settings.SETTINGS_MODULE,
    "database host/port/name": f"{db['HOST']}:{db.get('PORT') or 5432}/{db['NAME']}",
    "server reports": f"{server_host}:{server_port}/{server_db}",
    "email backend": settings.EMAIL_BACKEND,
    "email files": getattr(settings, "EMAIL_FILE_PATH", "(console: not written)")
    if "filebased" in settings.EMAIL_BACKEND else "(console: not written)",
    "media root": settings.MEDIA_ROOT,
    "razorpay order creation configured": bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET),
    "AWS keys present": bool(settings.AWS_ACCESS_KEY_ID or settings.AWS_SECRET_ACCESS_KEY),
    "simulated clock": settings.ACCEPTANCE_CLOCK or "(off: real clock)",
    "now (IST)": timezone.localtime().strftime("%Y-%m-%d %H:%M:%S %Z"),
}
for k, v in checks.items():
    print(f"  {k:38} {v}")

problems = []
if server_host not in ("127.0.0.1", "::1") or server_db != "iic_extbook_accept_dev":
    problems.append("database server/name is not the local acceptance database")
mail_dir = (settings.ACCEPTANCE_DATA_DIR / "mail").resolve()
if settings.EMAIL_BACKEND == "django.core.mail.backends.filebased.EmailBackend":
    if Path(settings.EMAIL_FILE_PATH).resolve() != mail_dir:
        problems.append("file e-mail is not in the dedicated .acceptance_local/mail folder")
elif settings.EMAIL_BACKEND != "django.core.mail.backends.console.EmailBackend":
    problems.append("e-mail backend is neither console nor the local file backend")
if settings.SBIEPAY_MERCHANT_ID or not settings.SBIEPAY_GATEWAY_URL.startswith("http://127.0.0.1:9/"):
    problems.append("SBIePay is not disabled")
if settings.CELERY_BEAT_SCHEDULER != "celery.beat:PersistentScheduler" or settings.CELERY_BEAT_SCHEDULE:
    problems.append("a beat schedule is configured")
try:
    socket.getaddrinfo("api.razorpay.com", 443)
    problems.append("outbound DNS is not blocked")
except OutboundNetworkBlocked:
    pass
if checks["razorpay order creation configured"] or checks["AWS keys present"]:
    problems.append("gateway or cloud credentials are present")
if problems:
    print("REFUSED: " + "; ".join(problems))
    sys.exit(2)
print("OK: local disposable acceptance target.")
