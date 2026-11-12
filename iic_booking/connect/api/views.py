"""IITR Connect API views."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .. import access
from .base import ConnectAPIView


class BootstrapView(ConnectAPIView):
    """Whether IITR Connect is on for the caller, and what the caller may do.

    Unlike every other Connect endpoint this one answers 200 with ``enabled: false`` when the module is off, so
    the frontend can hide its navigation without probing. It reveals nothing beyond the caller's own access.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["IITR Connect"], summary="Connect availability and the caller's capabilities")
    def get(self, request):
        caps = access.capabilities(request.user)
        return Response(
            {
                **caps,
                "name": "IITR Connect",
                "tagline": "Connect. Discuss. Share Knowledge. Solve Together.",
            }
        )
