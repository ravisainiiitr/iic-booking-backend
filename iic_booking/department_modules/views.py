"""Department module switches: Main Administrator matrix / cell update / history, and per-user availability."""

from __future__ import annotations

from functools import wraps

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import access, services
from .constants import ALL_MODULES
from .errors import DepartmentModuleError


def _flag(data: dict, name: str) -> bool | None:
    if name not in data or data[name] is None:
        return None
    value = data[name]
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "on", "yes"}:
        return True
    if text in {"false", "0", "off", "no"}:
        return False
    raise DepartmentModuleError(f"{name} must be true or false.", code="invalid", field=name)


def dm_api(methods, *, admin_only: bool = True):
    def deco(fn):
        @wraps(fn)
        def inner(request, *args, **kwargs):
            try:
                if admin_only and not access.is_main_admin(request.user):
                    raise DepartmentModuleError(
                        "Only the Main Administrator can manage department modules.", status=403, code="forbidden"
                    )
                return fn(request, *args, **kwargs)
            except DepartmentModuleError as exc:
                body = {"detail": exc.message, "code": exc.code}
                if exc.field:
                    body["field"] = exc.field
                return Response(body, status=exc.status)

        return api_view(methods)(permission_classes([IsAuthenticated])(inner))

    return deco


@dm_api(["GET"])
def matrix(request):
    return Response(services.matrix())


@dm_api(["POST", "PATCH"])
def update_cell(request, department_id: int, module_key: str):
    if module_key not in [m.value for m in ALL_MODULES]:
        raise DepartmentModuleError("Unknown module.", status=404, code="not_found")
    department = services.get_department(department_id)
    data = request.data if isinstance(request.data, dict) else {}
    cell = services.set_module(
        request.user,
        department,
        module_key,
        enabled=_flag(data, "enabled"),
        test_users_only=_flag(data, "test_users_only"),
        reason=str(data.get("reason") or ""),
        request=request,
    )
    return Response({"department_id": department.pk, "module_key": module_key, "cell": cell})


@dm_api(["GET"])
def history(request):
    params = request.query_params
    module = params.get("module") or None
    if module and module not in [m.value for m in ALL_MODULES]:
        raise DepartmentModuleError("Unknown module.", code="invalid_module", field="module")
    dept = services.get_department(params["department"]).pk if params.get("department") else None
    try:
        limit = int(params.get("limit") or 200)
    except ValueError:
        limit = 200
    return Response({"results": services.history(department_id=dept, module_key=module, limit=limit)})


@dm_api(["GET"], admin_only=False)
def me(request):
    return Response(access.user_availability(request.user))
