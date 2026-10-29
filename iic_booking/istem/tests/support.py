"""Test helpers: a scripted fake transport (no network) and fully mapped local bookings."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, DailySlot, Equipment, SlotMaster
from iic_booking.istem.choices import CodeType, UserLinkState
from iic_booking.istem.client import TransportResponse
from iic_booking.istem.errors import IstemError
from iic_booking.istem.models import IstemCodeMapping, IstemEquipmentMapping, IstemUserLink
from iic_booking.users.models.billing import ExternalBillingProfile
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

# Placeholder values used only by tests; never real credentials.
FAKE_USERNAME = "unit-test-user"
FAKE_PASSWORD = "unit-test-password-not-real"
FAKE_TOKEN = "unit-test-token-not-real"
BASE_URL = "https://istem.invalid/"

LIVE_SETTINGS = {
    "ISTEM_ENABLED": True,
    "ISTEM_MODE": "live",
    "ISTEM_BASE_URL": BASE_URL,
    "ISTEM_USERNAME": FAKE_USERNAME,
    "ISTEM_PASSWORD": FAKE_PASSWORD,
}


# The institute the fake token belongs to ([SPEC] /auth/token returns institute_id, user_id and user_entity).
FAKE_INSTITUTE_ID = "213"


def token_response(token=FAKE_TOKEN, expires_in=3600, institute_id=FAKE_INSTITUTE_ID):
    message = {"access_token": token, "token_type": "Bearer", "expires_in": expires_in, "user_id": 2740, "user_entity": "IN"}
    if institute_id is not None:
        message["institute_id"] = int(institute_id)
    return TransportResponse(200, json.dumps({"result": "SUCCESS", "message": message}))


def json_response(body, status=200):
    return TransportResponse(status, json.dumps(body))


def staging_token_response(token=FAKE_TOKEN, expires_in=3600):
    """[STAGING] /auth/token as captured on 2026-10-28: no institute_id, user_id or user_entity."""
    message = {"access_token": token, "token_type": "Bearer", "expires_in": expires_in}
    return TransportResponse(200, json.dumps({"result": "SUCCESS", "message": message}))


def listing_row(request_id, *, confirmation_id=None, status_code="Pending", fbr_prefix="19760"):
    """One Get All Bookings row ([STAGING] shape, identifiers only)."""
    return {"book_req_id": str(request_id), "book_cnfrm_id": str(request_id) if confirmation_id else None,
            "confirmation_id": str(confirmation_id) if confirmation_id else None,
            "fbc_no": f"{fbr_prefix}{request_id}", "generated_fbc_no": f"{fbr_prefix}{request_id}",
            "status_code": status_code, "institute_id": FAKE_INSTITUTE_ID}


def listing_response(*rows, total=None):
    """[STAGING] Get All Bookings answer: newest request first."""
    return json_response({"status": "SUCCESS", "message": "Booking get successfully",
                          "totalrecord": total if total is not None else len(rows), "records": len(rows),
                          "page": "1", "limit": "100", "result": list(rows)})


NOT_FOUND = {"status": "ERROR", "message": "booking not found"}


class FakeTransport:
    """Returns (or raises) scripted responses in order and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def send(self, method, url, *, headers, data=None, params=None, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "data": data, "params": params})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, IstemError):
            raise item
        return item

    def paths(self):
        return [c["url"].removeprefix(BASE_URL) for c in self.calls]


def apply_live(settings, **overrides):
    for key, value in {**LIVE_SETTINGS, **overrides}.items():
        setattr(settings, key, value)


def make_booking(*, profile_type="HOUR", total=Decimal("1500.00"), rate=Decimal("500.00"), status=BookingStatus.BOOKED,
                 samples="3", with_billing=True, gstin="09AAAAA0000A1Z5", user=None):
    equipment = Equipment.objects.create(
        name="FESEM", code=f"IS{uuid.uuid4().hex[:5].upper()}", slot_duration_minutes=60, user_rating_enabled=False
    )
    profile = ChargeProfile.objects.create(
        equipment=equipment, user_type=UserType.EXTERNAL, profile_type=profile_type, primary_unit_charge=rate,
        require_istem_fbr=True,
    )
    user = user or UserFactory(user_type=UserType.EXTERNAL, name="Asha Verma", admin_approved=True)
    if with_billing:
        ExternalBillingProfile.objects.get_or_create(user=user, defaults={
            "billing_name": "Asha Labs", "gstin": gstin, "billing_address_line1": "12 Station Road",
            "billing_city": "Roorkee", "billing_state": "Uttarakhand", "billing_pincode": "247667",
        })
    booking = Booking.objects.create(
        user=user, equipment=equipment, charge_profile=profile, user_type_snapshot=UserType.EXTERNAL, status=status,
        total_charge=total, total_time_minutes=180, virtual_booking_id=f"IIC{equipment.code}{uuid.uuid4().hex[:4]}",
        input_values={"A": samples},
    )
    start = timezone.now().replace(microsecond=0) + timedelta(days=2)
    master = SlotMaster.objects.create(equipment=equipment, slot_number=1, open_time=start.time(),
                                       close_time=(start + timedelta(hours=3)).time(), is_active=True)
    DailySlot.objects.create(slot_master=master, date=start.date(), start_datetime=start,
                             end_datetime=start + timedelta(hours=3), status="BOOKED", booking=booking)
    return booking


def map_equipment(equipment, *, verified=True, istem_id="12206", sample_key="A", listed=True, institute_id=FAKE_INSTITUTE_ID):
    """``listed``: the mapping was found in I-STEM's Equipment List for ``institute_id`` (as verify_equipment_mappings
    would record it)."""
    return IstemEquipmentMapping.objects.create(
        equipment=equipment, istem_equipment_id=istem_id, sample_count_input_key=sample_key,
        verified_at=timezone.now() if verified else None,
        remote_checked_at=timezone.now() if listed else None, remote_found=True if listed else None,
        remote_institute_id=institute_id if listed else "",
    )


def map_codes(equipment, *, profile_type="HOUR", user_type=UserType.EXTERNAL):
    for code_type, key, value, eq in (
        (CodeType.SERVICE_TYPE, profile_type, "Characterization", None),
        (CodeType.EQUIPMENT_UOM, profile_type, "Hour", None),
        (CodeType.USAGE_TYPE, user_type, "Industry User", None),
        (CodeType.USER_TYPE, user_type, "Industry User", None),
        (CodeType.ANALYSIS_TYPE, profile_type, "analysis1", equipment),
    ):
        IstemCodeMapping.objects.create(code_type=code_type, local_key=key, istem_value=value, equipment=eq)


def link_user(user, public_user_id=None):
    """Links ``user``; by default to 35151, or the next free id (two linked accounts may not share one)."""
    if public_user_id is None:
        taken = set(IstemUserLink.objects.filter(state=UserLinkState.LINKED).values_list("public_user_id", flat=True))
        public_user_id = next(str(n) for n in range(35151, 36000) if str(n) not in taken)
    return IstemUserLink.objects.create(user=user, public_user_id=public_user_id, state=UserLinkState.LINKED,
                                        linked_at=timezone.now())


def ready_booking(**kwargs):
    """A booking whose every Create Booking field can be produced, with a linked I-STEM user."""
    booking = make_booking(**kwargs)
    map_equipment(booking.equipment)
    map_codes(booking.equipment, profile_type=kwargs.get("profile_type", "HOUR"))
    link_user(booking.user)
    return booking
