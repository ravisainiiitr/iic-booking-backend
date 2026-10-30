"""REST API for external-user services (stage 2: the central external booking window)."""

from __future__ import annotations

from functools import wraps

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import AnonRateThrottle
from rest_framework.response import Response

from . import window as window_mod
from . import window_admin
from .models import ExternalBookingWindowConfig, ExternalBookingWindowOpening
from .window_admin import WindowAdminError


def _admin_api(fn):
    @wraps(fn)
    def inner(request, *args, **kwargs):
        try:
            window_admin.require_main_admin(request.user)
            return fn(request, *args, **kwargs)
        except WindowAdminError as exc:
            return Response(exc.payload(), status=exc.status)

    return inner


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def external_window_view(request):
    """The current external booking window (dates open to external users, next opening, in IST)."""
    w = window_mod.external_window()
    body = w.as_dict()
    body["applies_to_you"] = bool(w.enabled and window_mod.is_external_booking_user(request.user))
    raw = request.query_params.get("date")
    if raw:
        from django.utils.dateparse import parse_date

        d = parse_date(raw)
        if d is None:
            return Response({"error": "date must be YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        opens = window_mod.date_opens_at(w, d)
        body["date"] = {
            "date": d.isoformat(),
            "open": w.contains(d) if w.enabled else None,
            "opens_at": timezone.localtime(opens).isoformat() if opens else None,
        }
    return Response(body)


def _admin_payload():
    config = ExternalBookingWindowConfig.load()
    w = window_mod.compute(config)
    forced = window_mod.compute(config, force_enabled=True)
    openings = list(
        ExternalBookingWindowOpening.objects.order_by("-week_start").values(
            "week_start", "recorded_at", "slots_prepared_at", "quota_prepared_at", "slots_created", "snapshots_created"
        )[:12]
    )
    return {
        "config": window_admin.config_dict(config),
        "window": w.as_dict(),
        "window_if_enabled": forced.as_dict(),
        "openings": openings,
        "history": window_admin.history(20),
    }


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
@_admin_api
def admin_window_view(request):
    if request.method == "PATCH":
        window_admin.update(request.user, {k: request.data.get(k) for k in request.data})
    return Response(_admin_payload())


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_admin_api
def admin_window_preview(request):
    return Response(window_admin.preview({k: request.data.get(k) for k in request.data}))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_admin_api
def admin_window_pause(request):
    window_admin.pause(request.user, request.data.get("reason"))
    return Response(_admin_payload())


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_admin_api
def admin_window_resume(request):
    window_admin.resume(request.user, request.data.get("reason"))
    return Response(_admin_payload())


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_admin_api
def admin_window_advance(request):
    window_admin.advance(request.user, request.data.get("weeks"), request.data.get("reason"))
    return Response(_admin_payload())


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_admin_api
def admin_window_history(request):
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", 50))))
    except (TypeError, ValueError):
        limit = 50
    return Response({"history": window_admin.history(limit)})


# --- special requests ------------------------------------------------------------------------------------------------

def _service_errors(fn):
    """Map the service errors (each carries status, code and a user-facing message) to responses."""
    from .policy import PolicyError
    from .special_requests import SpecialRequestError

    @wraps(fn)
    def inner(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except (SpecialRequestError, PolicyError, WindowAdminError) as exc:
            return Response(exc.payload(), status=exc.status)

    return inner


def _data(request) -> dict:
    return {k: request.data.get(k) for k in request.data} if hasattr(request.data, "keys") else {}


def _json_data(request) -> dict:
    """JSON bodies keep lists and objects (alternatives, input_values)."""
    return dict(request.data) if isinstance(request.data, dict) else _data(request)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def special_policy_view(request):
    from . import policy
    from .models import ExternalServicesSettings, PolicyKey

    s = ExternalServicesSettings.load()
    return Response({
        "enabled": s.special_requests_enabled,
        "available_to_you": bool(s.special_requests_enabled and window_mod.is_external_booking_user(request.user)),
        "short_notice_days": s.short_notice_days,
        "payment_window_hours": s.payment_window_hours,
        "max_open_special_requests": s.max_open_special_requests,
        "short_notice_policy": policy.policy_dict(policy.current(PolicyKey.SHORT_NOTICE)),
        "notice": (
            "This is a special booking request, not a confirmed booking. The requested slot is subject to review, "
            "equipment availability, applicable quota, Officer in Charge approval, and payment requirements."
        ),
    })


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@_service_errors
def special_requests_view(request):
    from . import special_requests as svc

    if request.method == "POST":
        urg = svc.create(request.user, _json_data(request))
        return Response(svc.serialize(urg), status=status.HTTP_201_CREATED)
    return Response({"requests": svc.my_requests(request.user)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_service_errors
def special_request_preview(request):
    from . import special_requests as svc

    return Response(svc.preview(request.user, _json_data(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_service_errors
def special_request_detail(request, request_id):
    from . import special_requests as svc

    return Response(svc.get_mine(request.user, request_id))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_service_errors
def special_request_respond(request, request_id):
    from . import special_requests as svc

    return Response(svc.serialize(svc.respond(request.user, request_id, request.data.get("message"))))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_service_errors
def special_request_withdraw(request, request_id):
    from . import special_requests as svc

    return Response(svc.serialize(svc.withdraw(request.user, request_id, request.data.get("reason"))))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_service_errors
def special_request_acknowledge(request, request_id):
    from . import special_requests as svc
    from .special_payment import acknowledge_short_notice

    acknowledge_short_notice(request.user, request_id, request.data.get("short_notice_ack_version"))
    return Response(svc.get_mine(request.user, request_id))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_service_errors
def staff_special_requests(request):
    from . import special_staff

    return Response({"requests": special_staff.staff_list(request.user,
                                                          status_filter=request.query_params.get("status", ""))})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_service_errors
def staff_special_request_detail(request, request_id):
    from . import special_staff

    return Response(special_staff.staff_detail(request.user, request_id))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_service_errors
def staff_special_request_clarify(request, request_id):
    from . import special_staff

    return Response(special_staff.clarify(request.user, request_id, request.data.get("message")))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_service_errors
def staff_special_request_reject(request, request_id):
    from . import special_staff

    return Response(special_staff.reject(request.user, request_id, request.data.get("reason")))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_service_errors
def staff_special_request_slots(request, request_id):
    from django.utils.dateparse import parse_date

    from . import special_staff
    from .special_requests import SpecialRequestError

    start = parse_date(request.query_params.get("start") or "") or timezone.localdate()
    try:
        days = int(request.query_params.get("days") or 7)
    except ValueError:
        raise SpecialRequestError("days must be a number.", field="days")
    return Response(special_staff.candidate_slots(request.user, request_id, start, days))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_service_errors
def staff_special_request_allocate(request, request_id):
    from . import special_staff
    from .special_requests import SpecialRequestError

    slot_ids = request.data.get("slot_ids")
    if not isinstance(slot_ids, list):
        raise SpecialRequestError("slot_ids must be a list of slot ids.", field="slot_ids")
    return Response(special_staff.allocate(
        request.user, request_id, slot_ids, expected_total=request.data.get("expected_total"),
        note=request.data.get("note") or "",
    ))


# --- settings and policy wording (Main Administrator) ----------------------------------------------------------------

@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
@_admin_api
@_service_errors
def admin_settings_view(request):
    from . import policy
    from .models import ExternalServicesSettings, PolicyKey, PolicyText

    if request.method == "PATCH":
        policy.update_settings(request.user, _data(request))
    s = ExternalServicesSettings.load()
    return Response({
        "settings": policy.settings_dict(s),
        "policies": {
            key: [policy.policy_dict(p) for p in PolicyText.objects.filter(key=key).order_by("-version")[:10]]
            for key in PolicyKey.values
        },
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_admin_api
@_service_errors
def admin_policy_add(request):
    from . import policy
    from .models import PolicyKey

    key = request.data.get("key")
    if key not in PolicyKey.values:
        return Response({"error": "Unknown policy.", "code": "invalid", "field": "key"}, status=400)
    approved = window_admin._bool(request.data.get("approved", False), "approved")
    p = policy.add_version(request.user, key, request.data.get("text"), approved=approved,
                           approval_note=request.data.get("approval_note") or "")
    return Response(policy.policy_dict(p), status=status.HTTP_201_CREATED)


# --- invoices, copies, labels, receipt ---------------------------------------------------------------------------------

def _all_errors(fn):
    from .documents import DocumentError
    from .invoices import InvoiceError
    from .logistics import LogisticsError
    from .policy import PolicyError
    from .special_requests import SpecialRequestError
    from .sric_dispatch import DispatchError

    @wraps(fn)
    def inner(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except (InvoiceError, LogisticsError, DocumentError, PolicyError, SpecialRequestError, DispatchError,
                WindowAdminError) as exc:
            return Response(exc.payload(), status=exc.status)

    return inner


def _pdf(data: bytes, filename: str):
    from django.http import HttpResponse

    resp = HttpResponse(data, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp["Cache-Control"] = "no-store"
    return resp


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def booking_invoice_view(request, booking_id):
    from . import invoices

    if request.method == "POST":
        invoices.set_preference(request.user, booking_id, request.data.get("option"),
                                request.data.get("early_invoice_ack_version"))
    return Response(invoices.status_for_user(request.user, booking_id))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def booking_invoice_request_early(request, booking_id):
    from . import invoices

    invoices.request_early(request.user, booking_id)
    return Response(invoices.status_for_user(request.user, booking_id), status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_all_errors
def booking_provisional_invoice(request, booking_id):
    from iic_booking.equipment.models import Booking

    from . import documents, invoices, logistics

    booking = Booking.objects.select_related("equipment", "user").filter(pk=booking_id).first()
    allowed = booking is not None and (
        booking.user_id == request.user.pk or invoices.staff_may_handle(request.user, booking)
        or logistics.staff_can_receive(request.user, booking)
    )
    if not allowed:
        return Response({"error": "Booking not found.", "code": "not_found"}, status=404)
    ref = getattr(booking, "virtual_booking_id", "") or booking.pk
    return _pdf(documents.provisional_invoice_pdf(booking=booking), f"provisional_invoice_{ref}.pdf")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_all_errors
def special_request_provisional_invoice(request, request_id):
    from . import documents
    from .special_requests import _owned

    urg = _owned(request.user, request_id)
    return _pdf(documents.provisional_invoice_pdf(special_request=urg), f"provisional_estimate_{urg.pk}.pdf")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_all_errors
def invoice_document_download(request, document_id):
    from django.http import FileResponse

    from . import invoices

    doc = invoices.document_for_download(request.user, document_id)
    resp = FileResponse(doc.file.open("rb"), content_type="application/pdf", as_attachment=True,
                        filename=f"GST_invoice_{doc.request.snapshot.get('booking_reference') or doc.request_id}.pdf")
    resp["Cache-Control"] = "no-store"
    return resp


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def addresses_view(request):
    from . import logistics

    if request.method == "POST":
        a = logistics.save_address(request.user, _data(request))
        return Response(logistics.address_dict(a), status=status.HTTP_201_CREATED)
    from .models import ExternalServicesSettings

    return Response({
        "addresses": logistics.my_addresses(request.user),
        "last_label_address_id": logistics.last_label_address_id(request.user),
        "sample_labels_enabled": ExternalServicesSettings.load().sample_labels_enabled,
    })


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
@_all_errors
def address_detail_view(request, address_id):
    from . import logistics

    if request.method == "DELETE":
        return Response({"result": logistics.remove_address(request.user, address_id)})
    return Response(logistics.address_dict(logistics.save_address(request.user, _data(request), address_id=address_id)))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def booking_copy_request(request, booking_id):
    from . import logistics

    copy = logistics.request_copy(request.user, booking_id, _json_data(request))
    return Response(logistics.copy_dict(copy), status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_all_errors
def my_copies_view(request):
    from . import logistics

    return Response({"copies": logistics.my_copies(request.user)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def copy_cancel_view(request, copy_id):
    from . import logistics

    return Response(logistics.copy_dict(logistics.cancel_copy(request.user, copy_id)))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def booking_sample_label(request, booking_id):
    from . import logistics

    label, pdf = logistics.create_sample_label(request.user, booking_id, _json_data(request))
    return _pdf(pdf, f"sample_label_{label.reference}.pdf")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_all_errors
def staff_invoice_requests(request):
    from . import invoices

    return Response({"requests": invoices.staff_queue(request.user, status_filter=request.query_params.get("status", ""),
                                                      q=request.query_params.get("q", ""))})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def staff_invoice_upload(request, request_id):
    from . import invoices

    replace = str(request.data.get("replace", "")).lower() in ("true", "1", "yes")
    doc = invoices.upload_document(
        request.user, request_id, request.FILES.get("file"),
        booking_reference=request.data.get("booking_reference") or "",
        invoice_number=request.data.get("invoice_number") or "", replace=replace,
        replacement_reason=request.data.get("replacement_reason") or "",
    )
    return Response({"document_id": doc.pk, "sha256": doc.sha256, "uploaded_at": doc.uploaded_at.isoformat()},
                    status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def staff_invoice_resolve(request, request_id):
    from . import sric_dispatch

    req = sric_dispatch.resolve(request.user, request_id, request.data.get("action") or "", request.data.get("note") or "")
    return Response({"id": req.pk, "status": req.status})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_all_errors
def staff_copies(request):
    from . import logistics

    return Response({"copies": logistics.staff_copy_queue(request.user,
                                                          status_filter=request.query_params.get("status", ""))})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def staff_copy_prepared(request, copy_id):
    from . import logistics

    return Response(logistics.copy_dict(logistics.mark_prepared(request.user, copy_id), staff=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def staff_copy_dispatched(request, copy_id):
    from . import logistics

    copy = logistics.mark_dispatched(request.user, copy_id, dispatch_date=request.data.get("dispatch_date"),
                                     reference=request.data.get("speed_post_reference") or "",
                                     notes=request.data.get("notes") or "")
    return Response(logistics.copy_dict(copy, staff=True))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_all_errors
def staff_copy_envelope(request, copy_id):
    from . import logistics

    return _pdf(logistics.envelope_pdf(request.user, copy_id), f"envelope_{copy_id}.pdf")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def staff_sample_lookup(request):
    from . import logistics

    return Response(logistics.staff_lookup(request.user, request.data.get("code") or ""))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def staff_sample_confirm(request):
    from . import logistics

    return Response(logistics.staff_confirm(request.user, request.data.get("code") or "",
                                            outcome=request.data.get("outcome") or "",
                                            notes=request.data.get("notes") or ""))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_all_errors
def staff_sample_scan(request):
    """Opening a scanned label: office receipt, lab receipt or details, decided by the server for this user."""
    from . import logistics

    return Response(logistics.scan(request.user, request.data.get("code") or ""))


class SampleLabelCourierThrottle(AnonRateThrottle):
    """Per client IP, signed in or not: the label link is opened from a phone camera, often without a session."""

    scope = "sample_label_courier"
    rate = "60/hour"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([SampleLabelCourierThrottle])
@_all_errors
def sample_label_courier_status(request):
    """What a scanned label shows its holder: the printed facts and whether courier details may still be given."""
    from . import logistics

    return Response(logistics.courier_status(request.data.get("code") or ""))


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([SampleLabelCourierThrottle])
@_all_errors
def sample_label_courier_submit(request):
    """First scan by the sender: the courier and tracking number, accepted once per label (later only on the portal)."""
    from . import logistics

    data = request.data
    return Response(logistics.courier_submit(
        data.get("code") or "", courier_slug=str(data.get("courier_slug") or ""),
        courier_name=str(data.get("courier_name") or ""), tracking_number=str(data.get("tracking_number") or ""),
    ))


# --- booking checkout (payment choice; nothing held before payment) --------------------------------------------------

def _checkout_errors(fn):
    from .checkout import CheckoutError

    @wraps(fn)
    def inner(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except CheckoutError as exc:
            return Response(exc.payload(), status=exc.status)

    return inner


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_checkout_errors
def checkout_create(request):
    """Price an external self-booking and return the payment options. Holds nothing."""
    from . import checkout as checkout_mod

    body = _json_data(request)
    try:
        equipment_id = int(body.pop("equipment_id"))
    except (KeyError, TypeError, ValueError):
        return Response({"error": "equipment_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    checkout = checkout_mod.create(request.user, equipment_id, body)
    return Response(checkout_mod.payload(checkout), status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@_checkout_errors
def checkout_detail(request, public_id):
    from . import checkout as checkout_mod

    return Response(checkout_mod.payload(checkout_mod.get_for_user(request.user, public_id)))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_checkout_errors
def checkout_pay(request, public_id):
    """Apply a payment choice: wallet-only confirms now; online choices return the Razorpay order to open."""
    from . import checkout as checkout_mod

    result = checkout_mod.choose(request.user, public_id, str(_data(request).get("choice") or ""))
    checkout = checkout_mod.get_for_user(request.user, public_id)
    return Response({"checkout": checkout_mod.payload(checkout), "order": result["order"]})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_checkout_errors
def checkout_cancel(request, public_id):
    from . import checkout as checkout_mod

    return Response(checkout_mod.payload(checkout_mod.cancel(request.user, public_id)))


# --- government credit and invoice mode ----------------------------------------------------------------------------------

def _credit_errors(fn):
    from .govt_credit import GovtCreditError

    @wraps(fn)
    def inner(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except GovtCreditError as exc:
            return Response(exc.payload(), status=exc.status)

    return inner


def _credit_dict(req) -> dict:
    return {
        "id": str(req.public_id), "status": req.status, "status_label": req.get_status_display(),
        "organisation": req.organisation, "amount": f"{req.amount:.2f}", "proforma_reference": req.proforma_reference,
        "purpose": req.purpose, "credit_until": req.credit_until.isoformat(),
        "settlement_date": req.settlement_date.isoformat(), "invoice_mode": req.invoice_mode,
        "department_id": req.subwallet.department_id, "decided_at": req.decided_at, "settled_at": req.settled_at,
        "created_at": req.created_at,
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@_credit_errors
def invoice_mode_view(request):
    """GET the user's invoice mode; POST {"mode": "per_booking" | "consolidated"}."""
    from . import govt_credit

    if request.method == "POST":
        govt_credit.set_invoice_mode(request.user, str(request.data.get("mode") or ""))
    return Response({"mode": govt_credit.invoice_mode(request.user),
                     "credit_open": govt_credit.open_credit(request.user) is not None})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_credit_errors
def consolidated_invoice_release(request):
    """Send the user's completed bookings held for a consolidated invoice to SRIC as one request."""
    from . import govt_credit

    count = govt_credit.release_consolidated(request.user)
    return Response({"released": count})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_credit_errors
def consolidated_proforma_create(request):
    """Body: {"items": [{"description", "amount"}]}. Returns the consolidated pro forma invoice PDF."""
    from django.http import HttpResponse

    from . import govt_credit

    proforma = govt_credit.create_proforma(request.user, _json_data(request).get("items"))
    response = HttpResponse(govt_credit.proforma_pdf(proforma), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{proforma.reference}.pdf"'
    response["X-Proforma-Reference"] = proforma.reference
    return response


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@_credit_errors
def govt_credit_requests(request):
    """GET the user's credit requests; POST (multipart) a new one: proforma_reference, department_id, organisation,
    amount, credit_until, settlement_date, invoice_mode, purpose, approval_letter (file)."""
    from django.utils.dateparse import parse_date

    from . import govt_credit
    from .models import GovtCreditRequest

    if request.method == "POST":
        req = govt_credit.submit(
            request.user,
            proforma_reference=request.data.get("proforma_reference"),
            department_id=request.data.get("department_id"),
            organisation=request.data.get("organisation"),
            amount=request.data.get("amount"),
            credit_until=parse_date(str(request.data.get("credit_until") or "")),
            settlement_date=parse_date(str(request.data.get("settlement_date") or "")),
            approval_letter=request.FILES.get("approval_letter"),
            invoice_mode_choice=str(request.data.get("invoice_mode") or "consolidated"),
            purpose=request.data.get("purpose") or "",
        )
        return Response(_credit_dict(req), status=status.HTTP_201_CREATED)
    rows = GovtCreditRequest.objects.filter(user=request.user).select_related("subwallet")[:50]
    return Response({"requests": [_credit_dict(r) for r in rows]})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@_credit_errors
def govt_credit_settle(request, public_id):
    """Accounts In Charge: body {"reference"} (SRIC receipt / UTR) once the payment is realised."""
    from . import govt_credit

    req = govt_credit.settle(request.user, public_id, reference=str(request.data.get("reference") or ""))
    return Response(_credit_dict(req))


def govt_credit_decide(request, token):
    """The Head IIC's Accept / Reject link. GET shows a confirmation page (nothing is decided by opening the link);
    POST from that page applies the decision. The signed token is the credential."""
    from django.http import HttpResponse
    from django.middleware.csrf import get_token
    from django.utils.html import escape

    from . import govt_credit

    def page(body: str, code: int = 200) -> HttpResponse:
        return HttpResponse(
            "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<title>IIC credit request</title><style>body{font-family:system-ui,sans-serif;max-width:640px;"
            "margin:40px auto;padding:0 16px;color:#1f2937}button{padding:10px 22px;border:0;border-radius:6px;"
            "color:#fff;font-size:15px;cursor:pointer}textarea{width:100%;min-height:70px}</style></head><body>"
            f"<h2>IIC Booking Portal: credit request</h2>{body}</body></html>", status=code)

    try:
        req, action, email = govt_credit.read_token(token)
        if request.method == "POST":
            req = govt_credit.decide(token, note=str(request.POST.get("note") or ""))
            return page(f"<p>Thank you. The request from <b>{escape(req.organisation)}</b> is now "
                        f"<b>{escape(req.get_status_display())}</b>, and the user has been told.</p>")
    except govt_credit.GovtCreditError as exc:
        return page(f"<p>{escape(exc.message)}</p>", exc.status)
    if req.status != "pending":
        return page(f"<p>This request has already been decided ({escape(req.get_status_display())}).</p>")
    colour, verb = ("#15803d", "Accept") if action == "accept" else ("#b91c1c", "Reject")
    return page(
        f"<p><b>{escape(req.organisation)}</b> asks for credit of <b>Rs. {req.amount:.2f}</b> against pro forma "
        f"invoice {escape(req.proforma_reference)}, to be settled by {req.settlement_date:%d %b %Y}.</p>"
        f"<p>Approver: {escape(email)}</p>"
        f"<form method='post'><input type='hidden' name='csrfmiddlewaretoken' value='{get_token(request)}'>"
        "<p><label>Note (optional)<br><textarea name='note'></textarea></label></p>"
        f"<button style='background:{colour}'>{verb} this request</button></form>"
    )
