"""WebSocket URL routing for Django Channels."""

from django.urls import re_path

from iic_booking.communication.consumers import NotificationConsumer

websocket_urlpatterns = [
    re_path(r"ws/notifications/$", NotificationConsumer.as_asgi()),
]
