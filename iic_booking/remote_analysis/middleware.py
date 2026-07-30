"""Request correlation IDs for Remote Analysis API paths."""

from __future__ import annotations

from iic_booking.remote_analysis.production_hardening import correlation_scope, new_correlation_id


class RemoteAnalysisCorrelationMiddleware:
    """
    Assigns a correlation id for /api/v1/analysis/* requests.
    Honors X-Request-ID / X-Correlation-ID when present; echoes X-Correlation-ID on response.
    """

    PREFIX = "/api/v1/analysis/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or ""
        if not path.startswith(self.PREFIX):
            return self.get_response(request)

        incoming = (
            request.META.get("HTTP_X_CORRELATION_ID")
            or request.META.get("HTTP_X_REQUEST_ID")
            or ""
        ).strip()
        cid = incoming or new_correlation_id()
        with correlation_scope(cid, request_id=cid):
            response = self.get_response(request)
        response["X-Correlation-ID"] = cid
        return response
