# Read-only post-deploy regression probe. Everything runs inside one READ ONLY transaction that is
# rolled back; each call is isolated in a savepoint so a blocked write is reported, not persisted.
import traceback
from datetime import date, timedelta

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.cookie import CookieStorage
from django.db import connection, transaction
from django.test import RequestFactory
from django.urls import resolve
from rest_framework.test import APIRequestFactory, force_authenticate

from iic_booking.equipment.models import Booking, Equipment, EquipmentGroup

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (" :: " + str(detail) if detail else ""))


def api_get(name, path, user=None, params=None, expect=(200,), check=None):
    try:
        with transaction.atomic():
            req = APIRequestFactory().get(path, params or {})
            if user is not None:
                force_authenticate(req, user=user)
            match = resolve(path)
            resp = match.func(req, *match.args, **match.kwargs)
            if hasattr(resp, "render"):
                resp.render()
            data = getattr(resp, "data", None)
            extra = ""
            ok = resp.status_code in expect
            if ok and check is not None:
                ok, extra = check(data)
            record(name, ok, f"status={resp.status_code} {extra}".strip())
            return data
    except Exception as exc:
        record(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")
        traceback.print_exc(limit=3)
    return None


def admin_view(name, model, user, obj_pk=None):
    try:
        with transaction.atomic():
            ma = admin.site._registry[model]
            req = RequestFactory().get("/admin/")
            req.user = user
            req.session = {}
            req._messages = CookieStorage(req)
            resp = ma.change_view(req, str(obj_pk)) if obj_pk is not None else ma.changelist_view(req)
            if hasattr(resp, "render"):
                resp.render()
            record(name, resp.status_code == 200, f"status={resp.status_code}")
    except Exception as exc:
        record(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")
        traceback.print_exc(limit=3)


def count_key(key):
    def _check(data):
        if isinstance(data, dict):
            for k in (key, "results", "count"):
                if k in data:
                    v = data[k]
                    return True, f"{k}={len(v) if isinstance(v, list) else v}"
        if isinstance(data, list):
            return True, f"items={len(data)}"
        return True, f"type={type(data).__name__}"
    return _check


with transaction.atomic():
    cur = connection.cursor()
    cur.execute("SET TRANSACTION READ ONLY")
    cur.execute("SHOW transaction_read_only")
    print("transaction_read_only=", cur.fetchone()[0])

    for flag in (
        "EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED",
        "EQUIPMENT_GROUP_CROSS_RESCHEDULING_ENABLED",
        "EQUIPMENT_GROUP_AUTO_ALLOCATION_ENABLED",
    ):
        val = getattr(settings, flag, "UNDEFINED")
        record(f"flag {flag}={val}", val is False)

    sw = EquipmentGroup.objects.filter(
        alternative_booking_enabled=False,
        alternative_search_other_slots=False,
        cross_rescheduling_enabled=False,
        auto_allocation_enabled=False,
    ).count()
    total = EquipmentGroup.objects.count()
    record(f"group switches all OFF ({sw}/{total})", sw == total)

    User = get_user_model()
    superuser = User.objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
    grouped_ids = list(
        Equipment.objects.filter(equipment_group_id__in=[16, 21]).values_list("equipment_id", flat=True)
    )
    print("multi_member_group_equipment=", sorted(grouped_ids))
    from django.db.models import Count
    print("booking_status_counts=", sorted(Booking.objects.values_list("status").annotate(n=Count("pk")).values_list("status", "n")))
    sample_booking = (
        Booking.objects.filter(equipment_id__in=grouped_ids, status="BOOKED").order_by("-booking_id").first()
        or Booking.objects.filter(status="BOOKED").order_by("-booking_id").first()
        or Booking.objects.filter(equipment_id__in=grouped_ids).order_by("-booking_id").first()
        or Booking.objects.order_by("-booking_id").first()
    )
    owner = sample_booking.user if sample_booking else (
        User.objects.filter(is_active=True, is_superuser=False).exclude(last_login=None).order_by("-last_login").first()
    )
    print("sample_booking=", getattr(sample_booking, "booking_id", None),
          "equipment=", getattr(sample_booking, "equipment_id", None),
          "owner_id=", getattr(owner, "pk", None), "superuser_id=", getattr(superuser, "pk", None))

    api_get("catalogue anon GET /api/equipments/", "/api/equipments/", check=count_key("equipment"))
    if owner:
        api_get("catalogue user GET /api/equipments/", "/api/equipments/", user=owner, check=count_key("equipment"))
    api_get("catalogue departments GET", "/api/equipments/catalog-departments/", check=count_key("departments"))

    monday = date.today() - timedelta(days=date.today().weekday())
    for eid in sorted(grouped_ids)[:5]:
        api_get(f"detail GET /api/equipments/{eid}/", f"/api/equipments/{eid}/", user=owner,
                check=lambda d: (isinstance(d, dict), f"keys={len(d) if isinstance(d, dict) else 0}"))
        api_get(f"slots GET /api/equipments/{eid}/slots/ (current week)", f"/api/equipments/{eid}/slots/",
                params={"start_date": monday.isoformat(), "end_date": (monday + timedelta(days=6)).isoformat()},
                check=count_key("slots"))

    if owner:
        api_get("my bookings GET /api/bookings/", "/api/bookings/", user=owner, params={"limit": 5},
                check=count_key("results"))
        api_get("my waitlist GET /api/waitlist/my/", "/api/waitlist/my/", user=owner, check=count_key("entries"))
        api_get("wallet balance GET /api/wallet/balance/", "/api/wallet/balance/", user=owner, expect=(200, 403),
                check=lambda d: (True, "keys=" + ",".join(sorted(d.keys())[:6]) if isinstance(d, dict) else ""))

        def _resched(d):
            if not isinstance(d, dict):
                return False, "no body"
            opts = d.get("options") or []
            only_original = all(o.get("is_original") for o in opts)
            return (d.get("cross_rescheduling_enabled") is False and only_original,
                    f"cross_rescheduling_enabled={d.get('cross_rescheduling_enabled')} options={len(opts)} only_original={only_original}")

        if sample_booking:
            api_get(f"reschedule-options GET booking {sample_booking.booking_id} status={sample_booking.status} (expect disabled)",
                    f"/api/bookings/{sample_booking.booking_id}/reschedule-options/", user=owner, check=_resched)

    if superuser:
        api_get("admin bookings GET /api/bookings/", "/api/bookings/", user=superuser, params={"limit": 5},
                check=count_key("results"))
        from config.admin_api import admin_api_router
        for prefix, _vs, basename in admin_api_router().registry:
            if "equipment" in prefix and "{" not in prefix and prefix in ("equipment", "equipments", "equipment-groups"):
                api_get(f"admin api GET /api/admin/{prefix}/", f"/api/admin/{prefix}/", user=superuser,
                        check=count_key("results"))
        g = EquipmentGroup.objects.filter(equipment_group_id=16).first()
        if g:
            api_get("admin api GET /api/admin/equipment-groups/16/", "/api/admin/equipment-groups/16/", user=superuser,
                    check=lambda d: (isinstance(d, dict) and d.get("alternative_booking_enabled") is False,
                                     f"alternative_booking_enabled={d.get('alternative_booking_enabled') if isinstance(d, dict) else None}"))
        admin_view("django admin Equipment changelist", Equipment, superuser)
        admin_view("django admin EquipmentGroup changelist", EquipmentGroup, superuser)
        if grouped_ids:
            admin_view(f"django admin Equipment change {sorted(grouped_ids)[0]}", Equipment, superuser, sorted(grouped_ids)[0])
        admin_view("django admin EquipmentGroup change 16", EquipmentGroup, superuser, 16)
        admin_view("django admin Booking changelist", Booking, superuser)

    transaction.set_rollback(True)

failed = [n for n, ok in RESULTS if not ok]
print(f"REGRESSION_SUMMARY total={len(RESULTS)} passed={len(RESULTS) - len(failed)} failed={len(failed)}")
for n in failed:
    print("FAILED_CHECK", n)
