"""Masters: categories, request types, GST rates, vendors and items."""

from __future__ import annotations

from rest_framework.response import Response

from . import access, masters
from . import serializers as s
from .api import data_of, paginate, parse_bool, pm_api, req_reason
from .defaults import ensure_department_defaults
from .models import GSTRate, Item, ItemCategory, RequestTypeConfig, Vendor


def _dept(request, data=None):
    raw = (data or {}).get("department_id") if data else None
    return access.pick_department(request.pm_scope, raw if raw not in (None, "") else request.query_params.get("department_id"))


@pm_api(["GET", "POST"])
def categories(request):
    scope = request.pm_scope
    if request.method == "GET":
        dept = _dept(request)
        ensure_department_defaults(dept)
        qs = ItemCategory.objects.filter(department=dept)
        if not parse_bool(request.query_params.get("include_inactive", "")):
            qs = qs.filter(active=True)
        return Response({"results": [s.category(x) for x in qs]})
    data = data_of(request)
    dept = _dept(request, data)
    return Response(s.category(masters.save_category(scope, dept, data, request=request)), status=201)


@pm_api(["PATCH"])
def category_detail(request, pk: int):
    scope = request.pm_scope
    cat = masters.get_in_department(ItemCategory, pk, scope.department_ids())
    return Response(s.category(masters.save_category(scope, cat.department, data_of(request), instance=cat, request=request)))


@pm_api(["GET"])
def request_types(request):
    dept = _dept(request)
    ensure_department_defaults(dept)
    qs = RequestTypeConfig.objects.filter(department=dept)
    if not parse_bool(request.query_params.get("include_inactive", "")):
        qs = qs.filter(active=True)
    return Response({"results": [s.request_type(x) for x in qs]})


@pm_api(["PATCH"])
def request_type_detail(request, pk: int):
    scope = request.pm_scope
    rt = masters.get_in_department(RequestTypeConfig, pk, scope.department_ids())
    return Response(s.request_type(masters.update_request_type(scope, rt, data_of(request), request=request)))


@pm_api(["GET", "POST"])
def gst_rates(request):
    scope = request.pm_scope
    if request.method == "GET":
        dept = _dept(request)
        ensure_department_defaults(dept)
        qs = GSTRate.objects.filter(department=dept)
        if not parse_bool(request.query_params.get("include_inactive", "")):
            qs = qs.filter(active=True)
        return Response({"results": [s.gst_rate(x) for x in qs]})
    data = data_of(request)
    dept = _dept(request, data)
    return Response(s.gst_rate(masters.save_gst_rate(scope, dept, data, request=request)), status=201)


@pm_api(["PATCH"])
def gst_rate_detail(request, pk: int):
    scope = request.pm_scope
    row = masters.get_in_department(GSTRate, pk, scope.department_ids())
    return Response(s.gst_rate(masters.save_gst_rate(scope, row.department, data_of(request), instance=row, request=request)))


def _master_list(request, model, serialize, search_fields):
    dept = _dept(request)
    p = request.query_params
    qs = model.objects.filter(department=dept)
    if not parse_bool(p.get("include_archived", "")):
        qs = qs.filter(is_archived=False)
    if not parse_bool(p.get("include_inactive", "")):
        qs = qs.filter(active=True)
    term = (p.get("q") or "").strip()
    if term:
        qs = qs.filter(masters.search_q(term, *search_fields))
    return qs, serialize


@pm_api(["GET", "POST"])
def vendors(request):
    scope = request.pm_scope
    if request.method == "GET":
        qs, ser = _master_list(request, Vendor, s.vendor, ("name", "code", "gstin", "contact_person"))
        return paginate(request, qs, ser)
    data = data_of(request)
    dept = _dept(request, data)
    return Response(s.vendor(masters.save_vendor(scope, dept, data, request=request)), status=201)


@pm_api(["GET", "PATCH"])
def vendor_detail(request, pk: int):
    scope = request.pm_scope
    v = masters.get_in_department(Vendor, pk, scope.department_ids())
    if request.method == "GET":
        return Response(s.vendor(v))
    return Response(s.vendor(masters.save_vendor(scope, v.department, data_of(request), instance=v, request=request)))


@pm_api(["POST"])
def vendor_archive(request, pk: int):
    scope = request.pm_scope
    v = masters.get_in_department(Vendor, pk, scope.department_ids())
    return Response(s.vendor(masters.archive(scope, v, req_reason(data_of(request)), request=request)))


@pm_api(["GET", "POST"])
def items(request):
    scope = request.pm_scope
    if request.method == "GET":
        qs, ser = _master_list(request, Item, s.item, ("name", "code", "specification", "hsn_sac"))
        qs = qs.select_related("category", "default_gst_rate")
        cat = request.query_params.get("category_id")
        if cat:
            qs = qs.filter(category_id=cat)
        return paginate(request, qs, ser)
    data = data_of(request)
    dept = _dept(request, data)
    return Response(s.item(masters.save_item(scope, dept, data, request=request)), status=201)


@pm_api(["GET", "PATCH"])
def item_detail(request, pk: int):
    scope = request.pm_scope
    it = masters.get_in_department(Item, pk, scope.department_ids())
    if request.method == "GET":
        return Response(s.item(it))
    return Response(s.item(masters.save_item(scope, it.department, data_of(request), instance=it, request=request)))


@pm_api(["POST"])
def item_archive(request, pk: int):
    scope = request.pm_scope
    it = masters.get_in_department(Item, pk, scope.department_ids())
    return Response(s.item(masters.archive(scope, it, req_reason(data_of(request)), request=request)))
