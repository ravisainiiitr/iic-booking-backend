"""
ASGI config for IIC Booking project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/dev/howto/deployment/asgi/

"""
import warnings

# Suppress pkg_resources deprecation warning from razorpay (third-party)
warnings.filterwarnings("ignore", message=".*pkg_resources is deprecated.*", category=UserWarning)
# Suppress ASGI StreamingHttpResponse sync-iterator warning (third-party or DRF streaming)
warnings.filterwarnings("ignore", message=".*StreamingHttpResponse must consume synchronous iterators.*", category=Warning)

import os
import sys
from pathlib import Path

from django.core.asgi import get_asgi_application

# This allows easy placement of apps within the interior
# iic_booking directory.
BASE_DIR = Path(__file__).resolve(strict=True).parent.parent
sys.path.append(str(BASE_DIR / "iic_booking"))

# If DJANGO_SETTINGS_MODULE is unset, default to the local settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = get_asgi_application()

# Import routing after Django setup
from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator

from config.routing import websocket_urlpatterns

# In development, allow all origins for WebSocket connections
# In production, use AllowedHostsOriginValidator
import os
from django.conf import settings

if settings.DEBUG:
    # Development: Allow all origins
    websocket_application = AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
else:
    # Production: Use origin validator
    websocket_application = AllowedHostsOriginValidator(
        AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
    )

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": websocket_application,
    }
)
