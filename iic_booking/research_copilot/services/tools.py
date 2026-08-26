"""Tool registry — read-only tools executable; mutating tools return confirmation action cards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    mutating: bool
    roles: tuple[str, ...]


TOOL_REGISTRY: list[ToolSpec] = [
    ToolSpec("search_equipment", "Search instruments by name/capability/location", False, ("*",)),
    ToolSpec("search_slots", "Search available slots for equipment/date", False, ("*",)),
    ToolSpec("search_bookings", "List caller's bookings", False, ("*",)),
    ToolSpec("get_next_booking", "Caller's next upcoming booking", False, ("*",)),
    ToolSpec("get_wallet", "Wallet summary for caller", False, ("student", "faculty", "external", "admin")),
    ToolSpec("search_documentation", "RAG over docs", False, ("*",)),
    ToolSpec("recommend_software", "Recommend analysis software via R6 catalog", False, ("*",)),
    ToolSpec("get_sample_status", "Sample/trace status for caller's booking", False, ("*",)),
    ToolSpec("get_booking_results", "Result availability for caller's booking", False, ("*",)),
    ToolSpec("get_sample_deadline", "Sample submission deadline for caller's booking", False, ("*",)),
    ToolSpec("estimate_booking_cost", "Cost estimate via ChargeCalculationEngine (portal calculate still authoritative)", False, ("*",)),
    ToolSpec("create_booking", "Prepare booking options (requires confirmation)", True, ("student", "faculty", "external", "admin")),
    ToolSpec("cancel_booking", "Prepare cancel action (requires confirmation)", True, ("student", "faculty", "external", "admin")),
    ToolSpec("create_support_ticket", "Create support ticket", True, ("*",)),
    ToolSpec("launch_remote_analysis", "Open Analysis Workspace for a booking", True, ("student", "faculty", "operator", "admin")),
]


def list_tools_for_role(role_bucket: str) -> list[dict]:
    out = []
    for t in TOOL_REGISTRY:
        if "*" in t.roles or role_bucket in t.roles or role_bucket == "admin":
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "mutating": t.mutating,
                    # Read-only tools are executable; mutating tools return action cards only.
                    "available": not t.mutating or t.name in {"create_booking", "cancel_booking", "launch_remote_analysis"},
                }
            )
    return out


def _ok(data: dict | list | None = None, **extra) -> dict:
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return payload


def _err(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


def _search_equipment(*, arguments: dict, user) -> dict:
    from iic_booking.research_copilot.services.structured_search import search_equipment

    query = str(arguments.get("query") or arguments.get("q") or "").strip()
    limit = int(arguments.get("limit") or 8)
    hits = search_equipment(query=query, limit=max(1, min(limit, 20)))
    return _ok(
        [
            {
                "equipment_id": int(h.source_id.split(":")[1]) if ":" in h.source_id else None,
                "title": h.title,
                "snippet": h.snippet,
                "url": h.url,
                "score": h.score,
            }
            for h in hits
        ],
        actions=[
            {
                "id": "open_equipment",
                "label": f"Open {h.title}",
                "href": h.url,
                "enabled": True,
            }
            for h in hits[:3]
        ],
    )


def _search_slots(*, arguments: dict, user) -> dict:
    from iic_booking.equipment.models import DailySlot, Equipment, SlotStatus

    equipment_id = arguments.get("equipment_id")
    day_raw = arguments.get("date") or arguments.get("day")
    if not equipment_id:
        return _err("missing_equipment_id", "equipment_id is required for slot search")
    try:
        eq = Equipment.objects.get(pk=int(equipment_id))
    except Exception:
        return _err("equipment_not_found", f"Equipment {equipment_id} not found")

    try:
        day = date.fromisoformat(str(day_raw)) if day_raw else date.today() + timedelta(days=1)
    except ValueError:
        return _err("invalid_date", "date must be YYYY-MM-DD")

    qs = (
        DailySlot.objects.filter(
            slot_master__equipment=eq,
            date=day,
            status=SlotStatus.AVAILABLE,
            booking__isnull=True,
        )
        .select_related("slot_master")
        .order_by("start_datetime")[:40]
    )
    slots = [
        {
            "slot_id": s.pk,
            "start": s.start_datetime.isoformat() if s.start_datetime else None,
            "end": s.end_datetime.isoformat() if s.end_datetime else None,
            "status": s.status,
        }
        for s in qs
    ]

    if not slots:
        return _ok(
            {
                "equipment_id": eq.pk,
                "equipment_name": eq.name,
                "date": day.isoformat(),
                "slots": [],
                "note": "No AVAILABLE unbooked slots found for this date in portal data. Open the equipment page for the authoritative calendar (generation/window rules may apply).",
            },
            actions=[
                {
                    "id": "open_equipment_slots",
                    "label": f"View availability — {eq.name}",
                    "href": f"/equipments/{eq.pk}",
                    "enabled": True,
                }
            ],
        )

    public_mode = bool(arguments.get("public") or arguments.get("anonymous"))
    actions: list[dict] = [
        {
            "id": "open_equipment_slots",
            "label": f"View availability — {eq.name}",
            "href": f"/equipments/{eq.pk}",
            "enabled": True,
        }
    ]
    if not public_mode:
        actions = [
            {
                "id": f"book_slot_{i}",
                "label": f"Review & book {s.get('start') or 'slot'}",
                "href": f"/book-equipment?equipment={eq.pk}&date={day.isoformat()}",
                "enabled": True,
                "requires_confirmation": True,
                "hint": "Opens portal booking; confirmation uses normal booking APIs.",
            }
            for i, s in enumerate(slots[:5])
        ] + actions
    else:
        actions.append(
            {
                "id": "sign_in_to_book",
                "label": "Sign in to book",
                "href": "/auth",
                "enabled": True,
                "hint": "Booking requires a signed-in portal account.",
            }
        )

    return _ok(
        {
            "equipment_id": eq.pk,
            "equipment_name": eq.name,
            "date": day.isoformat(),
            "slots": slots,
            "source": "PORTAL_DATA",
            "public": public_mode,
        },
        actions=actions,
    )


def _search_bookings(*, arguments: dict, user) -> dict:
    from iic_booking.equipment.models import Booking

    # Hard scope to authenticated caller — ignore any attempted foreign user selectors.
    for banned in ("user_id", "user", "email", "owner", "owner_id", "target_user"):
        if banned in (arguments or {}):
            return _err(
                "forbidden",
                "Bookings are scoped to the authenticated user only.",
            )

    status_filter = (arguments.get("status") or "").strip().upper()
    # Booking PK is booking_id; schedule lives on related daily_slots (no start_datetime column).
    qs = list(
        Booking.objects.filter(user=user)
        .select_related("equipment")
        .prefetch_related("daily_slots")
        .order_by("-created_at")[:30]
    )
    if status_filter:
        qs = [b for b in qs if str(getattr(b, "status", "")).upper() == status_filter]
    rows = []
    for b in qs[:20]:
        eq = getattr(b, "equipment", None)
        slots = list(getattr(b, "daily_slots", []).all()) if hasattr(getattr(b, "daily_slots", None), "all") else []
        start = slots[0].start_datetime if slots else None
        end = slots[-1].end_datetime if slots else None
        rows.append(
            {
                "booking_id": b.pk,
                "equipment": getattr(eq, "name", None),
                "status": getattr(b, "status", None),
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
                "url": f"/my-bookings?booking={b.pk}",
            }
        )
    return _ok(
        rows,
        actions=[
            {
                "id": f"open_booking_{r['booking_id']}",
                "label": f"View booking #{r['booking_id']}",
                "href": r["url"],
                "enabled": True,
            }
            for r in rows[:5]
        ],
    )


def _get_wallet(*, arguments: dict, user) -> dict:
    for banned in ("user_id", "user", "email", "owner", "owner_id", "target_user"):
        if banned in (arguments or {}):
            return _err("forbidden", "Wallet is scoped to the authenticated user only.")
    try:
        wallet = None
        if hasattr(user, "get_accessible_wallet"):
            wallet = user.get_accessible_wallet()
        if wallet is None:
            from iic_booking.users.models import Wallet

            wallet = Wallet.objects.filter(user=user).first()
        if not wallet:
            return _ok({"balance": None, "note": "No accessible wallet found for this user.", "source": "PORTAL_DATA"})
        # Authoritative consolidated balance (Wallet has no .balance attribute).
        try:
            balance = wallet.total_balance
        except Exception:  # noqa: BLE001
            balance = None
        sub_wallets = []
        try:
            for sw in wallet.sub_wallets.select_related("department").all()[:20]:
                sub_wallets.append(
                    {
                        "department_id": getattr(sw.department, "pk", None),
                        "department": getattr(sw.department, "name", None),
                        "balance": str(sw.balance),
                    }
                )
        except Exception:  # noqa: BLE001
            pass
        return _ok(
            {
                "balance": str(balance) if balance is not None else None,
                "currency": "INR",
                "sub_wallets": sub_wallets,
                "source": "PORTAL_DATA",
                "note": "Authoritative wallet actions (recharge/transfer) remain in the Wallet portal page.",
            },
            actions=[
                {"id": "open_wallet", "label": "Open Wallet", "href": "/wallet", "enabled": True},
                {
                    "id": "wallet_recharge",
                    "label": "Recharge wallet",
                    "prompt": "I want to recharge my wallet.",
                    "enabled": True,
                    "requires_confirmation": True,
                    "hint": "Prepares a recharge proposal; payment still uses the portal/Razorpay flow.",
                },
                {
                    "id": "wallet_credit",
                    "label": "Request wallet credit",
                    "prompt": "Request wallet credit.",
                    "href": "/wallet/credit-facility",
                    "enabled": True,
                    "requires_confirmation": True,
                    "hint": "Credit requests need Main Administrator approval.",
                },
            ],
        )
    except Exception as exc:
        return _err("wallet_unavailable", f"Wallet lookup failed: {exc}")


def _get_next_booking(*, arguments: dict, user) -> dict:
    from django.utils import timezone

    from iic_booking.equipment.models import Booking

    _ = arguments
    now = timezone.now()
    qs = (
        Booking.objects.filter(user=user)
        .exclude(status__in=["CANCELLED", "REJECTED", "COMPLETED"])
        .select_related("equipment")
        .prefetch_related("daily_slots")
        .order_by("created_at")[:50]
    )
    best = None
    best_start = None
    for b in qs:
        slots = list(b.daily_slots.all())
        starts = [s.start_datetime for s in slots if getattr(s, "start_datetime", None)]
        if not starts:
            continue
        start = min(starts)
        if start < now:
            continue
        if best_start is None or start < best_start:
            best = b
            best_start = start
    if not best:
        return _ok(
            {"booking": None, "note": "No upcoming booking found for this user.", "source": "PORTAL_DATA"},
            actions=[{"id": "open_my_bookings", "label": "My Bookings", "href": "/my-bookings", "enabled": True}],
        )
    eq = best.equipment
    return _ok(
        {
            "booking_id": best.pk,
            "equipment": getattr(eq, "name", None),
            "status": getattr(best, "status", None),
            "start": best_start.isoformat() if best_start else None,
            "source": "PORTAL_DATA",
        },
        actions=[
            {
                "id": f"open_booking_{best.pk}",
                "label": f"View booking #{best.pk}",
                "href": f"/my-bookings?booking={best.pk}",
                "enabled": True,
            }
        ],
    )


def _own_booking_or_err(*, booking_id, user):
    from iic_booking.equipment.models import Booking

    if not booking_id:
        # Fall back to latest booking for caller
        b = (
            Booking.objects.filter(user=user)
            .select_related("equipment")
            .prefetch_related("daily_slots", "sample_trace_events")
            .order_by("-created_at")
            .first()
        )
        if not b:
            return None, _err("booking_not_found", "No booking found for this user")
        return b, None
    try:
        b = (
            Booking.objects.select_related("equipment")
            .prefetch_related("daily_slots", "sample_trace_events")
            .get(pk=int(booking_id), user=user)
        )
        return b, None
    except Booking.DoesNotExist:
        return None, _err("booking_not_found", "Booking not found for this user")
    except (TypeError, ValueError):
        return None, _err("invalid_booking_id", "booking_id must be an integer")


def _get_sample_status(*, arguments: dict, user) -> dict:
    booking, err = _own_booking_or_err(booking_id=arguments.get("booking_id"), user=user)
    if err:
        return err
    events = list(booking.sample_trace_events.order_by("-created_at")[:10])
    latest = events[0] if events else None
    rows = [
        {
            "status": getattr(e, "status", None),
            "reason": (getattr(e, "reason", None) or "")[:240] or None,
            "created_at": e.created_at.isoformat() if getattr(e, "created_at", None) else None,
        }
        for e in events
    ]
    return _ok(
        {
            "booking_id": booking.pk,
            "booking_status": getattr(booking, "status", None),
            "equipment": getattr(booking.equipment, "name", None),
            "latest_sample_status": getattr(latest, "status", None) if latest else None,
            "events": rows,
            "source": "PORTAL_DATA",
        },
        actions=[
            {
                "id": f"open_booking_{booking.pk}",
                "label": f"Open booking #{booking.pk}",
                "href": f"/my-bookings?booking={booking.pk}",
                "enabled": True,
            }
        ],
    )


def _get_booking_results(*, arguments: dict, user) -> dict:
    booking, err = _own_booking_or_err(booking_id=arguments.get("booking_id"), user=user)
    if err:
        return err
    from iic_booking.equipment.booking_results_service import has_material_result_files, merge_booking_result_files

    available = False
    try:
        available = bool(has_material_result_files(booking))
    except Exception:
        available = False
    names: list[str] = []
    try:
        merged = merge_booking_result_files(booking=booking, s3_files=[], request=None)
        for entry in merged[:12]:
            name = str(entry.get("name") or "").strip()
            if name:
                names.append(name)
    except Exception:
        names = []
    return _ok(
        {
            "booking_id": booking.pk,
            "equipment": getattr(booking.equipment, "name", None),
            "booking_status": getattr(booking, "status", None),
            "results_available": available,
            "file_names": names,
            "download": "Use the authenticated Results section in My Bookings — Copilot does not expose direct public storage URLs.",
            "source": "PORTAL_DATA",
        },
        actions=[
            {
                "id": f"open_results_{booking.pk}",
                "label": "Open results in portal",
                "href": f"/my-bookings?booking={booking.pk}&tab=results",
                "enabled": True,
            }
        ],
    )


def _get_sample_deadline(*, arguments: dict, user) -> dict:
    booking, err = _own_booking_or_err(booking_id=arguments.get("booking_id"), user=user)
    if err:
        return err
    from iic_booking.equipment.sample_submission_deadline_reminders import compute_sample_submission_deadline

    deadline = compute_sample_submission_deadline(booking)
    return _ok(
        {
            "booking_id": booking.pk,
            "equipment": getattr(booking.equipment, "name", None),
            "deadline": deadline.isoformat() if deadline else None,
            "note": None
            if deadline
            else "Could not compute a sample submission deadline from portal slot/equipment data.",
            "source": "PORTAL_DATA",
        },
        actions=[
            {
                "id": f"open_booking_{booking.pk}",
                "label": f"Open booking #{booking.pk}",
                "href": f"/my-bookings?booking={booking.pk}",
                "enabled": True,
            }
        ],
    )


def _estimate_booking_cost(*, arguments: dict, user) -> dict:
    """Live rough estimate via ChargeCalculationEngine (portal calculate remains authoritative)."""
    from decimal import Decimal

    from iic_booking.equipment.calculators import ChargeCalculationEngine, TimeCalculationEngine
    from iic_booking.equipment.models import ChargeProfile, ChargeProfilePricingProfile, Equipment
    from iic_booking.equipment.print_3d_views import get_charge_estimate_guest_user
    from iic_booking.users.models.user_type import UserType

    def _eq_pk(equipment) -> int | None:
        # Support ORM models and lightweight test doubles (id or pk).
        raw = getattr(equipment, "pk", None)
        if raw is None:
            raw = getattr(equipment, "id", None)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    equipment_id = arguments.get("equipment_id")
    if not equipment_id:
        return _err("missing_equipment_id", "equipment_id is required")
    try:
        eq = Equipment.objects.get(pk=int(equipment_id))
    except Exception:
        return _err("equipment_not_found", f"Equipment {equipment_id} not found")

    eq_pk = _eq_pk(eq)
    if eq_pk is None:
        return _err("equipment_not_found", f"Equipment {equipment_id} not found")

    public_mode = bool(arguments.get("public") or arguments.get("anonymous"))
    actor = user
    if public_mode or actor is None or not getattr(actor, "is_authenticated", False):
        actor = get_charge_estimate_guest_user()
        user_type = str(arguments.get("user_type") or UserType.EXTERNAL)
    else:
        user_type = str(
            arguments.get("user_type")
            or getattr(actor, "user_type", None)
            or UserType.STUDENT
        )

    cp = (
        ChargeProfile.objects.filter(
            equipment=eq,
            user_type=user_type,
            pricing_profile=ChargeProfilePricingProfile.STANDARD,
            is_active=True,
        ).first()
        or ChargeProfile.objects.filter(
            equipment=eq,
            pricing_profile=ChargeProfilePricingProfile.STANDARD,
            is_active=True,
        ).first()
    )
    if not cp:
        return _ok(
            {
                "equipment_id": eq_pk,
                "equipment_name": eq.name,
                "estimate": None,
                "note": "No active charge profile found. Open the booking flow for an authoritative estimate.",
                "source": "PORTAL_DATA",
            },
            actions=[
                {
                    "id": "open_book_equipment",
                    "label": f"Review charges — {eq.name}",
                    "href": f"/book-equipment?equipment={eq_pk}",
                    "enabled": True,
                    "requires_confirmation": not public_mode,
                }
            ],
        )

    # Prefer defaults from dynamic input fields; fall back to A=1.
    input_values: dict = {}
    try:
        from iic_booking.equipment.models import DynamicInputField

        for f in DynamicInputField.objects.filter(equipment=eq, user_type=user_type):
            if f.default_value not in (None, ""):
                try:
                    input_values[f.field_key] = float(f.default_value)
                except (TypeError, ValueError):
                    input_values[f.field_key] = f.default_value
    except Exception:  # noqa: BLE001
        pass
    if "A" not in input_values:
        input_values["A"] = 1.0
    # Allow explicit overrides from tool args (A–G)
    for key in list("ABCDEFG"):
        if key in arguments and arguments[key] is not None:
            try:
                input_values[key] = float(arguments[key])
            except (TypeError, ValueError):
                input_values[key] = arguments[key]

    try:
        total_time_minutes = TimeCalculationEngine.calculate_time(
            cp,
            input_values,
            slot_duration_minutes=eq.slot_duration_minutes,
        )
        total_charge, breakdown = ChargeCalculationEngine.calculate_charge(
            cp,
            input_values,
            total_time_minutes,
            selected_parameters=None,
        )
    except Exception as exc:  # noqa: BLE001
        return _err("estimate_failed", f"Could not estimate charge: {exc}")

    note = (
        "Rough estimate using default/sample inputs and the standard charge profile "
        f"for user type “{user_type}”. Portal booking/calculate remains authoritative "
        "(accessories, PI rates, GST, and live inputs may change the total)."
    )
    if public_mode:
        note += " Sign in for a personalized estimate and to book."

    actions = [
        {
            "id": "open_book_equipment",
            "label": f"Review charges — {eq.name}",
            "href": f"/book-equipment?equipment={eq_pk}",
            "enabled": True,
            "requires_confirmation": not public_mode,
            "hint": "Portal calculate is the source of truth for cost.",
        }
    ]
    if public_mode:
        actions.append(
            {
                "id": "sign_in_for_estimate",
                "label": "Sign in for personalized estimate",
                "href": "/auth",
                "enabled": True,
            }
        )

    return _ok(
        {
            "equipment_id": eq_pk,
            "equipment_name": eq.name,
            "user_type": user_type,
            "profile_type": cp.profile_type,
            "input_values": input_values,
            "total_time_minutes": float(total_time_minutes) if total_time_minutes is not None else None,
            "estimate": float(Decimal(str(total_charge))),
            "currency": "INR",
            "breakdown": breakdown,
            "note": note,
            "source": "PORTAL_DATA",
            "public": public_mode,
        },
        actions=actions,
    )


def _search_documentation(*, arguments: dict, user) -> dict:
    from iic_booking.research_copilot.services import rag as rag_svc
    from iic_booking.research_copilot.services.context_builder import build_context

    query = str(arguments.get("query") or arguments.get("q") or "").strip()
    if len(query) < 2:
        return _err("query_too_short", "Provide a documentation query")
    ctx = build_context(user)
    retrieval = rag_svc.retrieve(
        query=query,
        role_bucket=ctx.role_bucket,
        department_id=ctx.department_id,
        user=user,
        conversation=None,
    )
    return _ok(
        {
            "intent": retrieval.intent,
            "low_confidence": retrieval.low_confidence,
            "citations": rag_svc.citations_as_dicts(retrieval.citations),
            "context_preview": (retrieval.context_block or "")[:1200],
        }
    )


def _recommend_software(*, arguments: dict, user) -> dict:
    """Reuse R6 AnalysisSoftwareCatalog / EquipmentAnalysisSoftware — no new mapping system."""
    _ = user
    from django.db.models import Q

    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog, EquipmentAnalysisSoftware

    equipment_id = arguments.get("equipment_id")
    query = str(arguments.get("query") or arguments.get("q") or "").strip().lower()
    file_type = str(arguments.get("file_type") or arguments.get("extension") or "").strip().lower().lstrip(".")
    rows: list[dict] = []

    if equipment_id:
        qs = (
            EquipmentAnalysisSoftware.objects.filter(equipment_id=int(equipment_id), catalog__is_active=True)
            .select_related("catalog")
            .prefetch_related("catalog__capabilities")
            .order_by("sort_order", "catalog__name")
        )
        for i, row in enumerate(qs[:12]):
            cat = row.catalog
            caps = [c.name for c in cat.capabilities.all()[:8]]
            stars = 5 if row.is_default else max(2, 5 - min(i, 3))
            rows.append(
                {
                    "software_id": str(cat.id),
                    "name": cat.name,
                    "slug": cat.slug,
                    "is_default": row.is_default,
                    "stars": stars,
                    "description": (getattr(cat, "description", None) or "")[:240],
                    "capabilities": caps,
                    "category": getattr(cat, "category", "") or "",
                }
            )
    else:
        qs = AnalysisSoftwareCatalog.objects.filter(is_active=True).prefetch_related("capabilities").order_by("name")
        if query or file_type:
            filt = Q()
            token = file_type or query
            for part in {token, query, file_type} - {""}:
                filt |= (
                    Q(name__icontains=part)
                    | Q(description__icontains=part)
                    | Q(category__icontains=part)
                    | Q(capabilities__name__icontains=part)
                    | Q(capabilities__slug__icontains=part)
                )
            # Common SEM/DM heuristics from catalog text (no invented license claims)
            if file_type in {"dm3", "dm4"}:
                filt |= Q(name__icontains="digitalmicrograph") | Q(description__icontains="dm4") | Q(
                    description__icontains="dm3"
                )
            qs = qs.filter(filt).distinct()
        for i, cat in enumerate(qs[:12]):
            caps = [c.name for c in cat.capabilities.all()[:8]]
            rows.append(
                {
                    "software_id": str(cat.id),
                    "name": cat.name,
                    "slug": cat.slug,
                    "is_default": False,
                    "stars": 4 if i == 0 else 3,
                    "description": (getattr(cat, "description", None) or "")[:240],
                    "capabilities": caps,
                    "category": getattr(cat, "category", "") or "",
                    "file_type_query": file_type or None,
                    "license_note": "Live license/availability is confirmed only by the Remote Analysis scheduler at launch time.",
                }
            )

    actions = [
        {
            "id": "open_software_catalog",
            "label": "Open Software Catalog",
            "href": "/remote-analysis/software-catalog",
            "enabled": True,
        }
    ]
    if equipment_id:
        actions.insert(
            0,
            {
                "id": "analyze_data",
                "label": "Analyze Data",
                "href": "/my-bookings",
                "enabled": True,
                "hint": "Open a completed booking to start Remote Analysis with recommended software.",
            },
        )
    return _ok(rows, actions=actions)


def _prepare_create_booking(*, arguments: dict, user) -> dict:
    from iic_booking.users.legacy_ledger.booking_lock import end_user_booking_is_locked

    locked, lock_message = end_user_booking_is_locked(user)
    equipment_id = arguments.get("equipment_id")
    day = arguments.get("date") or ""
    href = "/book-equipment"
    params = []
    if equipment_id:
        params.append(f"equipment={equipment_id}")
    if day:
        params.append(f"date={day}")
    if params:
        href = href + "?" + "&".join(params)
    return _ok(
        {
            "requires_confirmation": True,
            "message": lock_message or (
                "I can open the booking flow with these details. Booking is confirmed only after the portal booking API succeeds under your permissions."
            ),
            "equipment_id": equipment_id,
            "date": day,
            "booking_locked": locked,
        },
        actions=[
            {
                "id": "book_equipment",
                "label": "Book Equipment",
                "href": href,
                "enabled": not locked,
                "requires_confirmation": True,
                "hint": lock_message if locked else "Opens the booking flow — confirm in the portal. Copilot does not create bookings itself.",
            }
        ],
    )


def _prepare_cancel_booking(*, arguments: dict, user) -> dict:
    booking_id = arguments.get("booking_id")
    if not booking_id:
        return _err("missing_booking_id", "booking_id is required")
    from iic_booking.equipment.models import Booking

    try:
        booking = Booking.objects.select_related("equipment").get(pk=int(booking_id), user=user)
    except Booking.DoesNotExist:
        return _err("booking_not_found", "Booking not found for this user")
    return _ok(
        {
            "requires_confirmation": True,
            "booking_id": booking.pk,
            "equipment": getattr(booking.equipment, "name", None),
            "status": getattr(booking, "status", None),
            "message": "Cancellation uses the existing portal cancellation API and policy. Confirm in My Bookings.",
        },
        actions=[
            {
                "id": "cancel_booking",
                "label": "Cancel Booking",
                "href": f"/my-bookings?booking={booking.pk}&action=cancel",
                "enabled": True,
                "requires_confirmation": True,
                "hint": "Opens booking details — confirm cancellation in the portal under your permissions.",
            }
        ],
    )


def _prepare_launch_remote_analysis(*, arguments: dict, user) -> dict:
    booking_id = arguments.get("booking_id")
    if not booking_id:
        return _err("missing_booking_id", "booking_id is required")
    from iic_booking.equipment.models import Booking

    try:
        booking = Booking.objects.get(pk=int(booking_id), user=user)
    except Booking.DoesNotExist:
        return _err("booking_not_found", "Booking not found for this user")
    return _ok(
        {
            "requires_confirmation": True,
            "booking_id": booking.pk,
            "message": "Full desktop Remote Analysis continues through Analysis Workspace.",
        },
        actions=[
            {
                "id": "launch_remote_analysis",
                "label": "Open Analysis Workspace",
                "href": f"/analysis-workspace/{booking.pk}",
                "enabled": True,
                "requires_confirmation": True,
                "hint": "Opens Analysis Workspace — reservation/session starts only after portal confirmation.",
            }
        ],
    )


def _create_support_ticket(*, arguments: dict, user) -> dict:
    _ = (arguments, user)
    return _ok(
        {"requires_confirmation": True, "message": "Open Tickets to create a support request with conversation context."},
        actions=[
            {
                "id": "create_support_ticket",
                "label": "Open Tickets",
                "href": "/tickets",
                "enabled": True,
                "requires_confirmation": True,
                "hint": "Opens Tickets — create the support request yourself in the portal.",
            }
        ],
    )


_HANDLERS = {
    "search_equipment": _search_equipment,
    "search_slots": _search_slots,
    "search_bookings": _search_bookings,
    "get_next_booking": _get_next_booking,
    "get_wallet": _get_wallet,
    "search_documentation": _search_documentation,
    "recommend_software": _recommend_software,
    "get_sample_status": _get_sample_status,
    "get_booking_results": _get_booking_results,
    "get_sample_deadline": _get_sample_deadline,
    "estimate_booking_cost": _estimate_booking_cost,
    "create_booking": _prepare_create_booking,
    "cancel_booking": _prepare_cancel_booking,
    "create_support_ticket": _create_support_ticket,
    "launch_remote_analysis": _prepare_launch_remote_analysis,
}


def _role_bucket_for_user(user) -> str:
    from iic_booking.research_copilot.services.context_builder import _role_bucket

    user_type = getattr(user, "user_type", None) or getattr(user, "role", None) or ""
    if hasattr(user_type, "value"):
        user_type = user_type.value
    return _role_bucket(str(user_type or ""))


def execute_tool(*, name: str, arguments: dict, user) -> dict:
    """Execute a registered tool. Mutating tools only prepare authorized portal action cards."""
    from iic_booking.research_copilot.services.audit import audit_tool_executed

    handler = _HANDLERS.get(name)
    if not handler:
        result = _err("unknown_tool", f"Tool '{name}' is not registered")
        audit_tool_executed(user=user, name=name, ok=False, arguments=arguments, result=result)
        return result

    spec = next((t for t in TOOL_REGISTRY if t.name == name), None)
    role = _role_bucket_for_user(user)
    if spec and "*" not in spec.roles and role not in spec.roles and role != "admin":
        result = _err("forbidden", f"Tool '{name}' is not available for role '{role}'")
        audit_tool_executed(user=user, name=name, ok=False, arguments=arguments, result=result)
        return result

    try:
        result = handler(arguments=arguments or {}, user=user)
    except Exception as exc:  # noqa: BLE001 — tool boundary
        result = _err("tool_failed", f"Tool '{name}' failed: {exc}")
    audit_tool_executed(
        user=user,
        name=name,
        ok=bool(result.get("ok")),
        arguments=arguments,
        result=result,
    )
    return result


def enrich_actions_from_message(*, user, text: str, base_actions: list[dict] | None = None) -> list[dict]:
    """Heuristic action enrichment for common intents (safe navigation cards)."""
    actions = list(base_actions or [])
    lower = (text or "").lower()
    seen = {a.get("id") for a in actions}

    def add(action: dict) -> None:
        if action["id"] not in seen:
            actions.insert(0, action)
            seen.add(action["id"])

    if any(w in lower for w in ("book", "slot", "availability", "sem", "fesem", "tem", "xrd")):
        add(
            {
                "id": "book_equipment",
                "label": "Book Equipment",
                "href": "/book-equipment",
                "enabled": True,
                "requires_confirmation": True,
                "hint": "Suggestion only — confirm booking in the portal booking flow.",
            }
        )
        add({"id": "open_equipments", "label": "Browse Equipments", "href": "/equipments", "enabled": True})
    if any(w in lower for w in ("software", "digitalmicrograph", "imagej", "origin", "matlab", "analyze")):
        add(
            {
                "id": "recommend_software",
                "label": "Software Catalog",
                "href": "/remote-analysis/software-catalog",
                "enabled": True,
            }
        )
    if any(w in lower for w in ("my booking", "upcoming", "cancel", "sample status", "my result")):
        add({"id": "open_my_bookings", "label": "My Bookings", "href": "/my-bookings", "enabled": True})
    if "wallet" in lower:
        add({"id": "open_wallet", "label": "Open Wallet", "href": "/wallet", "enabled": True})
    if any(w in lower for w in ("result", "download")):
        add({"id": "open_my_bookings_results", "label": "My Results", "href": "/my-bookings", "enabled": True})
    return actions
