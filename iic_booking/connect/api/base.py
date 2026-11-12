"""Base API view: Connect permissions, throttles and private / no-index response headers on every response."""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from ..permissions import ConnectEnabled

PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Vary": "Authorization, Cookie",
}


def apply_private_headers(response):
    for key, value in PRIVATE_HEADERS.items():
        response[key] = value
    return response


class ConnectAPIView(APIView):
    permission_classes = [IsAuthenticated, ConnectEnabled]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "connect_read"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        return apply_private_headers(response)
