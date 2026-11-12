from decimal import Decimal

from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework import status

from iic_booking.users.models import ExternalBillingProfile, UserType
from .models import Booking, BookingSampleTrace, BookingStatus, Equipment, SampleTraceStatus
from .api_views import get_external_gst_percent
from .document_exports import (
    build_booking_invoice_pdf,
    build_shipping_label_pdf,
    build_return_shipping_label_pdf,
    build_proforma_invoice_pdf,
    build_proforma_invoice_multi_pdf,
)


def _get_or_create_billing_profile(user):
    return ExternalBillingProfile.objects.get_or_create(user=user)[0]


def _check_can_access_booking(user, booking: Booking) -> bool:
    # Booking owner can access; admin-panel users can access any.
    if booking.user_id == user.id:
        return True
    user_type = str(getattr(user, "user_type", "")).lower()
    return user_type in ("admin", "manager", "operator", "finance")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_invoice_pdf(request, booking_id: int):
    try:
        booking = Booking.objects.select_related("user", "equipment").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    if not _check_can_access_booking(request.user, booking):
        return Response({"error": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)

    billing = _get_or_create_billing_profile(booking.user)
    pdf = build_booking_invoice_pdf(booking=booking, billing_profile=billing)
    filename = f"invoice_{getattr(booking, 'virtual_booking_id', None) or booking.booking_id}.pdf"
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_shipping_label_pdf(request, booking_id: int):
    try:
        booking = Booking.objects.select_related("user", "equipment").prefetch_related(
            "equipment__equipment_managers__manager"
        ).get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    if not _check_can_access_booking(request.user, booking):
        return Response({"error": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)

    billing = _get_or_create_billing_profile(booking.user)
    pdf = build_shipping_label_pdf(booking=booking, billing_profile=billing)
    filename = f"shipping_label_{getattr(booking, 'virtual_booking_id', None) or booking.booking_id}.pdf"
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_return_shipping_label_pdf(request, booking_id: int):
    """
    Return-shipping label (IIC -> user). Only applicable when external user opted to receive samples back.
    """
    try:
        booking = Booking.objects.select_related("user", "equipment").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    if not _check_can_access_booking(request.user, booking):
        return Response({"error": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)

    if not getattr(booking, "sample_return_after_analysis", False):
        return Response({"error": "Return shipping label is unavailable for this booking."}, status=status.HTTP_400_BAD_REQUEST)

    analyzed = booking.status == BookingStatus.COMPLETED or BookingSampleTrace.objects.filter(
        booking=booking, status=SampleTraceStatus.COMPLETED
    ).exists()
    if not analyzed:
        return Response(
            {"error": "Return shipping label is available only after analysis is completed."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    billing = _get_or_create_billing_profile(booking.user)
    pdf = build_return_shipping_label_pdf(booking=booking, billing_profile=billing)
    filename = f"return_shipping_label_{getattr(booking, 'virtual_booking_id', None) or booking.booking_id}.pdf"
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def equipment_proforma_invoice_pdf(request, equipment_id: int):
    """
    Create a proforma invoice (estimate) for an equipment.

    Body:
      - base_charge: number|string (required)
      - description: string (optional)
    Backend will compute GST for external users and total.
    """
    try:
        equipment = Equipment.objects.get(pk=equipment_id)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)

    base_charge_raw = request.data.get("base_charge")
    if base_charge_raw is None or str(base_charge_raw).strip() == "":
        return Response({"error": "base_charge is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        base_charge = Decimal(str(base_charge_raw)).quantize(Decimal("0.01"))
    except Exception:
        return Response({"error": "Invalid base_charge."}, status=status.HTTP_400_BAD_REQUEST)

    gst_percent = Decimal("0")
    # Note: Industry user type is stored as UserType.INSTITUTE (value: "Industry")
    if UserType.is_external_user(getattr(request.user, "user_type", None) or ""):
        gst_percent = Decimal(str(get_external_gst_percent() or 0))

    gst_amount = (base_charge * gst_percent / Decimal("100")).quantize(Decimal("0.01")) if gst_percent > 0 else Decimal("0.00")
    total = (base_charge + gst_amount).quantize(Decimal("0.01"))

    from .proforma_store import ProformaError, issue_single, pdf_bytes

    billing = _get_or_create_billing_profile(request.user)
    try:
        record = issue_single(user=request.user, billing_profile=billing, equipment=equipment, base_charge=base_charge,
                              gst_amount=gst_amount, description=str(request.data.get("description") or ""))
    except ProformaError as exc:
        return Response(exc.payload(), status=exc.status)
    pdf = pdf_bytes(record)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{record.number}.pdf"'
    resp["X-Proforma-Numbers"] = record.number
    return resp


@api_view(["POST"])
@permission_classes([AllowAny])
def proforma_invoice_pdf_download(request):
    """
    Generate and download proforma invoice PDF with IIT Roorkee letterhead, user details,
    date/time of request, line items, and computer-generated disclaimer.

    Body: {
      "line_items": [
        {
          "equipment_id", "equipment_code", "equipment_name",
          "input_values": {...},
          "input_labels_and_values": {"Label": value, ...},  // optional, for PDF display
          "charge_breakdown": [{"description": "...", "amount": number}],
          "base_charge", "gst_amount", "total_charge"
        }
      ],
      "subtotal": "0.00",
      "total_gst": "0.00",
      "total_amount": "0.00"
    }
    """
    data = request.data or {}
    line_items = data.get("line_items")
    if not isinstance(line_items, list) or len(line_items) == 0:
        return Response(
            {"error": "Provide a non-empty line_items array."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    subtotal = str(data.get("subtotal") or "0")
    total_gst = str(data.get("total_gst") or "0")
    total_amount = str(data.get("total_amount") or "0")
    try:
        Decimal(subtotal)
        Decimal(total_gst)
        Decimal(total_amount)
    except Exception:
        return Response(
            {"error": "Invalid subtotal, total_gst, or total_amount."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from .proforma_store import ProformaError, issue_for_estimate, merged_pdf

    request_dt = timezone.now()
    pdf_user = request.user if request.user and request.user.is_authenticated else None
    try:
        records = issue_for_estimate(pdf_user, line_items, request_dt)
    except ProformaError as exc:
        return Response(exc.payload(), status=exc.status)
    pdf = merged_pdf(records)
    numbers = [r.number for r in records]
    filename = numbers[0] if len(numbers) == 1 else f"proforma_invoices_{request_dt.strftime('%Y%m%d_%H%M%S')}"
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
    # Every proforma in the file is saved: the page links to them under My proforma invoices.
    resp["X-Proforma-Numbers"] = ",".join(numbers)
    return resp

