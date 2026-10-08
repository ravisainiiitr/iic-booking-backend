from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.decorators import permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .http import ExportError
from .http import export_response
from .http import parse_format
from .registry import get_builder

logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def export_report(request, report_key: str):
    """GET /api/exports/<report_key>/?export_format=xlsx|csv|pdf&<the list page's own filters>.

    Optional ``table=<key>`` exports one section of a multi-table report.
    """
    builder = get_builder(report_key)
    if builder is None:
        return Response({"error": "Unknown export."}, status=status.HTTP_404_NOT_FOUND)
    try:
        fmt = parse_format(request)
        document = builder(request)
        table_key = (request.query_params.get("table") or "").strip()
        if table_key:
            chosen = [t for t in document.tables if t.key == table_key]
            if not chosen:
                raise ExportError("Unknown table for this export.")
            document.tables = chosen
            document.kpis = []
            document.subtitle = chosen[0].title
        return export_response(document, fmt)
    except ExportError as exc:
        return Response({"error": exc.message}, status=exc.status)
    except Exception:
        logger.exception("Export %s failed", report_key)
        return Response(
            {"error": "The export could not be generated. Please try again or narrow the filters."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
