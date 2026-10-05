"""Bootstrap (drives navigation), per-department configuration, role assignments and audit log."""

from __future__ import annotations

from django.db.models import Q
from rest_framework.response import Response

from iic_booking.users.models import Department, User
from iic_booking.users.models.department import DepartmentType

from . import access, config_service
from . import constants as c
from . import serializers as s
from .api import data_of, paginate, parse_day, parse_int, pm_api, req_str
from .errors import forbidden, not_found
from .models import ProcurementAuditLog, ProcurementManagementConfiguration, ProcurementRoleAssignment

R = c.ModuleRole
P = c.OfficePermission


def _menus(scope, dept_id, cfg) -> dict:
    roles = scope.roles(dept_id)
    perms = scope.permissions(dept_id)
    lab_staff = bool(roles & {R.OIC, R.LAB_OPERATOR})
    wide = scope.dept_wide(dept_id)
    actor = bool(roles - {R.AUDITOR})
    return {
        "dashboard": True,
        "my_requests": actor,
        "requirements": (cfg.plan_enabled or cfg.non_plan_enabled) and (lab_staff or wide),
        "procurement": wide,
        "small_purchases": wide,
        "consumables": cfg.consumables_enabled and (wide or lab_staff),
        "assets": cfg.asset_register_enabled and (wide or lab_staff),
        "amc": cfg.amc_enabled and (wide or lab_staff),
        "approvals": bool(roles & {R.OIC, R.OC_STORES, R.HOD, R.MAIN_ADMIN}) or P.OFFLINE_APPROVAL in perms,
        "consolidation": P.CONSOLIDATE in perms,
        "reports": P.REPORTS in perms,
        "configuration": R.MAIN_ADMIN in roles,
    }


@pm_api(["GET"], require_module=False, pilot_bootstrap=True)
def bootstrap(request):
    scope = request.pm_scope
    dept_ids = scope.department_ids()
    configs = {
        cfg.department_id: cfg
        for cfg in ProcurementManagementConfiguration.objects.filter(department_id__in=dept_ids).select_related("department")
    }
    departments = []
    merged: dict[str, bool] = {}
    for dept_id in sorted(dept_ids, key=lambda d: configs[d].department.name if d in configs else ""):
        cfg = configs.get(dept_id)
        if cfg is None:
            continue
        menus = _menus(scope, dept_id, cfg)
        for k, v in menus.items():
            merged[k] = merged.get(k, False) or v
        departments.append(
            {
                "department": s.department_brief(cfg.department),
                "roles": sorted(scope.roles(dept_id)),
                "permissions": sorted(scope.permissions(dept_id)),
                "features": {
                    f: getattr(cfg, f)
                    for f in config_service.BOOL_FIELDS
                    if f.endswith("_enabled") and f != "module_enabled"
                },
                "small_purchase_threshold": s.m(cfg.small_purchase_threshold),
                "hod_approval_mode": cfg.hod_approval_mode,
                "menus": menus,
            }
        )
    enabled = bool(departments)
    equipment = []
    if enabled:
        from iic_booking.equipment.models import Equipment

        lab_ids = set(scope.oic_equipment) | set(scope.operator_equipment)
        wide_depts = [d for d in dept_ids if scope.dept_wide(d)]
        rows = Equipment.objects.filter(
            Q(equipment_id__in=lab_ids, internal_department_id__in=dept_ids) | Q(internal_department_id__in=wide_depts)
        ).order_by("name")
        equipment = [
            {**s.equipment_brief(eq), "department_id": eq.internal_department_id, "is_oic": eq.pk in scope.oic_equipment}
            for eq in rows
        ]
    return Response(
        {
            "enabled": enabled,
            "can_configure": scope.admin,
            "departments": departments,
            "menus": merged if enabled else {},
            "oic_equipment_ids": sorted(scope.oic_equipment) if enabled else [],
            "operator_equipment_ids": sorted(scope.operator_equipment) if enabled else [],
            "equipment": equipment,
        }
    )


def _require_admin(request):
    if not request.pm_scope.admin:
        raise forbidden("Only the Main Administrator can manage Procurement & Assets configuration.")


def _internal_department(dept_id):
    dept = Department.objects.filter(pk=dept_id, department_type=DepartmentType.INTERNAL).first()
    if dept is None:
        raise not_found("Department not found.")
    return dept


@pm_api(["GET"], require_module=False)
def config_list(request):
    """Main Admin: every internal department with its switch. Others: configs of their departments."""
    scope = request.pm_scope
    if scope.admin:
        depts = Department.objects.filter(department_type=DepartmentType.INTERNAL).order_by("name")
        configs = {cfg.department_id: cfg for cfg in ProcurementManagementConfiguration.objects.all()}
        rows = []
        for dept in depts:
            cfg = configs.get(dept.pk) or ProcurementManagementConfiguration(department=dept)
            rows.append(s.config(cfg))
        return Response({"results": rows})
    scope.require_any_department()
    rows = [
        s.config(cfg)
        for cfg in ProcurementManagementConfiguration.objects.filter(department_id__in=scope.department_ids()).select_related(
            "department", "updated_by"
        )
    ]
    return Response({"results": rows})


@pm_api(["GET", "PATCH"], require_module=False)
def config_detail(request, department_id: int):
    scope = request.pm_scope
    dept = _internal_department(department_id)
    if request.method == "GET":
        if not scope.admin and department_id not in scope.department_ids():
            raise not_found()
        cfg = access.get_config(dept) or ProcurementManagementConfiguration(department=dept)
        return Response(s.config(cfg))
    _require_admin(request)
    cfg = config_service.update_config(request.user, dept, data_of(request), request=request)
    return Response(s.config(cfg))


@pm_api(["GET", "POST"], require_module=False)
def role_assignments(request, department_id: int):
    _require_admin(request)
    dept = _internal_department(department_id)
    if request.method == "GET":
        rows = ProcurementRoleAssignment.objects.filter(department=dept).select_related("user").order_by("role", "user__name")
        return Response({"results": [s.role_assignment(r) for r in rows], "roles": [r.value for r in c.ASSIGNABLE_ROLES],
                         "permissions": [{"value": p.value, "label": str(p.label)} for p in c.OfficePermission]})
    data = data_of(request)
    user_id = parse_int(data.get("user_id"), "user_id", required=True)
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        raise not_found("User not found.")
    role = req_str(data, "role", max_len=20)
    row = config_service.assign_role(request.user, dept, user, role, data.get("permissions"), request=request)
    return Response(s.role_assignment(row), status=201)


@pm_api(["DELETE"], require_module=False)
def role_assignment_detail(request, department_id: int, assignment_id: int):
    _require_admin(request)
    row = ProcurementRoleAssignment.objects.filter(pk=assignment_id, department_id=department_id).first()
    if row is None:
        raise not_found()
    config_service.revoke_role(request.user, row, reason=str(data_of(request).get("reason") or ""), request=request)
    return Response(status=204)


@pm_api(["GET"], require_module=False)
def user_search(request):
    _require_admin(request)
    term = (request.query_params.get("q") or "").strip()
    if len(term) < 2:
        return Response({"results": []})
    qs = (
        User.objects.filter(is_active=True)
        .filter(Q(user_type__in=access.MODULE_USER_TYPES) | Q(is_superuser=True))
        .filter(Q(name__icontains=term) | Q(email__icontains=term))
        .order_by("name")[:20]
    )
    return Response({"results": [{**s.user_brief(u), "user_type": u.user_type} for u in qs]})


@pm_api(["GET"])
def audit_logs(request):
    """Auditor drill-down: department → object → date range. Department-wide roles with reports permission."""
    scope = request.pm_scope
    allowed = [d for d in scope.department_ids() if scope.has_perm(d, P.REPORTS)]
    if not allowed:
        raise forbidden()
    qs = ProcurementAuditLog.objects.filter(department_id__in=allowed).select_related("actor")
    p = request.query_params
    dept = parse_int(p.get("department_id"), "department_id")
    if dept:
        qs = qs.filter(department_id=dept)
    if p.get("object_type"):
        qs = qs.filter(object_type=p["object_type"])
    if p.get("object_id"):
        qs = qs.filter(object_id=str(p["object_id"]))
    if p.get("action"):
        qs = qs.filter(action__startswith=p["action"])
    d_from = parse_day(p.get("date_from"), "date_from")
    d_to = parse_day(p.get("date_to"), "date_to")
    if d_from:
        qs = qs.filter(created_at__date__gte=d_from)
    if d_to:
        qs = qs.filter(created_at__date__lte=d_to)
    return paginate(request, qs, s.audit_log)
