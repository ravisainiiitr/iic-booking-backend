"""Run a page's own list endpoint for the signed-in user and collect every row it returns.

Exports call the exact view that feeds the page (same permission checks, role scoping, filters, search,
ordering and serialized fields), paging through it, so a file never contains anything the user could not
see on screen.
"""

from __future__ import annotations

import copy

from django.http import QueryDict
from django.urls import resolve
from django.urls import reverse

from .http import EXPORT_ROW_LIMIT
from .http import ExportError
from .http import check_row_limit

EXPORT_ONLY_PARAMS = ("export_format", "table")


def _error_message(data, status_code: int) -> str:
    if isinstance(data, dict):
        for key in ("error", "detail", "message"):
            if data.get(key):
                return str(data[key])
    if status_code == 403:
        return "You do not have permission to export this list."
    return "The list could not be loaded for export."


def call_view(request, url_name: str, params: dict | QueryDict | None = None, *, kwargs: dict | None = None):
    """GET ``api:<url_name>`` as the current user with ``params`` (defaults to the request's own query)."""
    path = reverse(f"api:{url_name}", kwargs=kwargs or {})
    match = resolve(path)
    if params is None:
        query = request.query_params.copy()
    elif isinstance(params, QueryDict):
        query = params.copy()
    else:
        query = QueryDict(mutable=True)
        for key, value in params.items():
            if isinstance(value, (list, tuple)):
                query.setlist(key, [str(v) for v in value])
            elif value is not None:
                query[key] = str(value)
    for key in EXPORT_ONLY_PARAMS:
        query.pop(key, None)
    inner = copy.copy(request._request)
    inner.GET = query
    inner.path = path
    inner.path_info = path
    inner.method = "GET"
    inner.META = {**inner.META, "QUERY_STRING": query.urlencode(), "PATH_INFO": path}
    response = match.func(inner, *match.args, **match.kwargs)
    data = getattr(response, "data", None)
    if response.status_code >= 400:
        raise ExportError(_error_message(data, response.status_code), status=response.status_code)
    return data


def _items(data, results_key: str | None):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if results_key and isinstance(data.get(results_key), list):
            return data[results_key]
        for key in ("results", "items", "entries"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _total(data):
    if isinstance(data, dict):
        for key in ("total_count", "count", "total"):
            value = data.get(key)
            if isinstance(value, int):
                return value
    return None


def collect_rows(
    request,
    url_name: str,
    *,
    results_key: str | None = "results",
    page_size: int | None = 200,
    limit_param: str = "limit",
    offset_param: str = "offset",
    page_param: str | None = None,
    params: dict | QueryDict | None = None,
    kwargs: dict | None = None,
    cap: int = EXPORT_ROW_LIMIT,
) -> tuple[list, object]:
    """Every row of a list endpoint (paging with limit/offset, or ``page_param`` page numbers).

    Returns ``(rows, first_page_data)``; raises ``ExportTooLarge`` when more than ``cap`` rows match.
    """
    base = request.query_params.copy() if params is None else params
    if not isinstance(base, QueryDict):
        query = QueryDict(mutable=True)
        for key, value in base.items():
            if isinstance(value, (list, tuple)):
                query.setlist(key, [str(v) for v in value])
            elif value is not None:
                query[key] = str(value)
        base = query
    rows: list = []
    first = None
    page = 1
    while True:
        query = base.copy()
        if page_size:
            if page_param:
                query[page_param] = str(page)
                query[limit_param] = str(page_size)
            else:
                query[limit_param] = str(page_size)
                query[offset_param] = str(len(rows))
        data = call_view(request, url_name, query, kwargs=kwargs)
        if first is None:
            first = data
        batch = _items(data, results_key)
        rows.extend(batch)
        check_row_limit(len(rows))
        total = _total(data)
        if total is not None:
            check_row_limit(total)
        if not page_size or not batch:
            break
        if total is not None:
            if len(rows) >= total:
                break
        elif len(batch) < page_size:
            break
        if len(rows) > cap:
            break
        page += 1
    return rows, first
