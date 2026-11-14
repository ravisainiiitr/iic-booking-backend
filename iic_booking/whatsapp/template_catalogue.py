"""The WhatsApp templates the portal uses. This file is the one approved list: the sync command submits from it.

The three iic_booking_* entries repeat, word for word, what is already at Meta; their names stay as they are because Meta
keys templates by name. New templates carry a line about STOP in the footer. ``trigger`` says what in the portal sends the
message. A template belongs here only when the portal has that step: users cannot request a reschedule, so the one template
for it simply reports that a booking was rescheduled.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

LANGUAGE = "en"
FOOTER_STOP = "Reply STOP to stop WhatsApp messages."
SIGN_OFF = "\n\nIIT Roorkee"
NAME_RE = re.compile(r"^[a-z0-9_]{1,512}$")
BODY_MAX, HEADER_MAX, FOOTER_MAX = 1024, 60, 60
_VARIABLE = re.compile(r"\{\{(\d+)\}\}")


@dataclass(frozen=True)
class Template:
    event: str
    name: str
    category: str
    header: str
    body: str
    footer: str
    params: tuple[str, ...]  # what each {{n}} means, in order
    examples: tuple[str, ...]
    recipient: str
    trigger: str | None
    language: str = LANGUAGE
    code_expiry_minutes: int = 0  # set only on an authentication template

    @property
    def is_authentication(self) -> bool:
        return self.category == "AUTHENTICATION"

    def payload(self) -> dict:
        if self.is_authentication:  # Meta writes the wording itself: a template only chooses these options
            return {"name": self.name, "language": self.language, "category": self.category, "components": [
                {"type": "BODY", "add_security_recommendation": True},
                {"type": "FOOTER", "code_expiration_minutes": self.code_expiry_minutes},
                {"type": "BUTTONS", "buttons": [{"type": "OTP", "otp_type": "COPY_CODE", "text": "Copy code"}]},
            ]}
        components: list[dict] = []
        if self.header:
            components.append({"type": "HEADER", "format": "TEXT", "text": self.header})
        components.append({"type": "BODY", "text": self.body, "example": {"body_text": [list(self.examples)]}})
        if self.footer:
            components.append({"type": "FOOTER", "text": self.footer})
        return {"name": self.name, "language": self.language, "category": self.category,
                "parameter_format": "positional", "components": components}

    def wording_hash(self) -> str:
        """Changes when the wording or category changes: this is what allows a controlled correction."""
        text = json.dumps([self.category, self.header, self.body, self.footer], sort_keys=True)
        return hashlib.sha256(text.encode()).hexdigest()


_DETAILS = "Booking ID: {{2}}\nEquipment: {{3}}\nDate: {{4}}"
_BOOKER = "The user who made the booking"
_NAME, _REF, _EQ, _DATE, _TIME = "Asha Verma", "XRD202600123", "X-Ray Diffractometer", "10 Nov 2026", "10:00 AM"


def _new(event, name, header, opening, closing, *, params=("user name", "booking ID", "equipment", "date"),
         examples=(_NAME, _REF, _EQ, _DATE), details=_DETAILS, trigger=None, recipient=_BOOKER) -> Template:
    body = f"Dear {{{{1}}}},\n\n{opening}\n\n{details}\n\n{closing}{SIGN_OFF}"
    return Template(event=event, name=name, category="UTILITY", header=header, body=body, footer=FOOTER_STOP,
                    params=tuple(params), examples=tuple(examples), recipient=recipient, trigger=trigger)


_WITH_TIME = {"params": ("user name", "booking ID", "equipment", "date", "time"), "examples": (_NAME, _REF, _EQ, _DATE, _TIME),
              "details": _DETAILS + "\nTime: {{5}}"}
_WITH_AMOUNT = {"params": ("user name", "booking ID", "equipment", "amount"), "examples": (_NAME, _REF, _EQ, "Rs. 1,500.00"),
                "details": "Booking ID: {{2}}\nEquipment: {{3}}\nAmount: {{4}}"}
_REFUND_AMOUNT = {"params": ("user name", "booking ID", "amount"), "examples": (_NAME, _REF, "Rs. 1,500.00"),
                  "details": "Booking ID: {{2}}\nRefund amount: {{3}}"}
_SLOT = {"params": ("user name", "equipment", "date", "time"), "examples": (_NAME, _EQ, _DATE, _TIME),
         "details": "Equipment: {{2}}\nDate: {{3}}\nTime: {{4}}"}
_PORTAL = "the IIT Roorkee Equipment Booking Portal"

CATALOGUE: tuple[Template, ...] = (
    Template(
        event="booking_confirmed", name="iic_booking_confirmed", category="UTILITY", header="Booking Confirmation",
        body=("Dear {{1}},\n\nYour equipment booking has been confirmed.\n\nBooking ID: {{2}}\nEquipment: {{3}}\nDate: {{4}}\n"
              f"Time: {{{{5}}}}\n\nPlease visit {_PORTAL} for booking details.{SIGN_OFF}"),
        footer="", params=("user name", "booking ID", "equipment", "date", "time"), examples=(_NAME, _REF, _EQ, _DATE, _TIME),
        recipient=_BOOKER, trigger="Booking created as Booked, or moved to Booked from Pending or Awaiting payment, including by online or offline payment",
    ),
    Template(
        event="booking_cancelled", name="iic_booking_cancelled", category="UTILITY", header="Booking Cancellation",
        body=("Dear {{1}},\n\nYour equipment booking has been cancelled.\n\nBooking ID: {{2}}\nEquipment: {{3}}\nDate: {{4}}\n\n"
              f"For further information, please visit {_PORTAL}.{SIGN_OFF}"),
        footer="", params=("user name", "booking ID", "equipment", "date"), examples=(_NAME, _REF, _EQ, _DATE),
        recipient=_BOOKER, trigger="Booking event Cancelled or Refunded",
    ),
    Template(
        event="booking_completed", name="iic_booking_completed", category="UTILITY", header="Booking Completed",
        body=("Dear {{1}},\n\nYour equipment booking has been successfully completed.\n\nBooking ID: {{2}}\nEquipment: {{3}}\n"
              f"Completion Date: {{{{4}}}}\n\nThank you for using {_PORTAL}.{SIGN_OFF}"),
        footer="", params=("user name", "booking ID", "equipment", "completion date"), examples=(_NAME, _REF, _EQ, _DATE),
        recipient=_BOOKER, trigger="Booking event Completed",
    ),
    _new("booking_rejected", "iitr_booking_rejected", "Booking Not Accepted",
         "Your equipment booking could not be accepted by the laboratory.",
         f"The reason and the next steps are shown on {_PORTAL}.", trigger="Laboratory rejects a 3D printing or laser cutting job"),
    _new("booking_rescheduled", "iitr_booking_rescheduled", "Booking Rescheduled",
         "Your equipment booking has been rescheduled. The new slot is shown below.",
         f"Please check the updated details on {_PORTAL}.", trigger="Booking event Rescheduled", **_WITH_TIME),
    _new("booking_reminder", "iitr_booking_reminder", "Booking Reminder",
         "This is a reminder of your equipment booking.",
         f"The booking details are on {_PORTAL}.", trigger="Daily booking reminder task", **_WITH_TIME),
    _new("payment_successful", "iitr_payment_successful", "Payment Received",
         "We have received the payment for your equipment booking.",
         f"The payment details are on {_PORTAL}.", trigger="Online payment order marked Paid", **_WITH_AMOUNT),
    _new("payment_failed", "iitr_payment_failed", "Payment Not Completed",
         "The payment for your equipment booking was not completed.",
         f"You can check the booking and try again on {_PORTAL}.", trigger="Online payment order marked Failed", **_WITH_AMOUNT),
    _new("refund_completed", "iitr_refund_completed", "Refund Completed",
         "The refund for your equipment booking has been completed.",
         f"The details are shown on {_PORTAL}.", trigger="Wallet refund made, bank refund closed, or gateway refund processed",
         **_REFUND_AMOUNT),
    _new("waitlist_slot_available", "iitr_waitlist_slot_available", "Waitlist Slot Available",
         "A slot has become available for equipment you are waitlisted for.",
         f"Slots are not held. To book it, please visit {_PORTAL}.", trigger="Freed slot within 48 hours, same step as the email",
         recipient="Each active waitlist user for that equipment", **_SLOT),
    _new("remote_analysis_ready", "iitr_remote_analysis_ready", "Analysis PC Ready",
         "The Analysis PC for your remote analysis is ready and is being held for you.",
         f"Please start your session on {_PORTAL} before the hold ends. After that the PC goes to the next person in the queue.",
         params=("user name", "booking ID", "equipment", "minutes held"), examples=(_NAME, _REF, _EQ, "10"),
         details="Booking ID: {{2}}\nEquipment: {{3}}\nHeld for: {{4}} minutes",
         trigger="Remote Analysis queue gives the booking an Analysis PC and opens its check-in hold"),
    _new("remote_analysis_results_uploaded", "iitr_remote_analysis_results_uploaded", "Analysis Results Uploaded",
         "All the results of your remote analysis session have been uploaded.",
         f"You can open them in My Research on {_PORTAL}.",
         params=("user name", "booking ID", "equipment", "files"), examples=(_NAME, _REF, _EQ, "40 files (18.2 GB)"),
         details="Booking ID: {{2}}\nEquipment: {{3}}\nUploaded: {{4}}",
         trigger="The Analysis PC finished uploading a session's results outbox to My Research"),
    Template(
        event="otp_code", name="iitr_verification_code", category="AUTHENTICATION", header="",
        body="{{1}} is your verification code. For your security, do not share this code.",
        footer="This code expires in 5 minutes.", params=("one-time code",), examples=("482913",),
        recipient="The person who asked for the code",
        trigger="Proving a WhatsApp number, signing in with a proved number, or an I-STEM OTP the person asked to get on WhatsApp",
        code_expiry_minutes=5,
    ),
)
BY_EVENT = {t.event: t for t in CATALOGUE}


def validate() -> list[str]:
    """Meta's documented rules for the whole list, so a bad entry is caught here instead of being rejected there."""
    problems: list[str] = []
    seen_names: set[tuple[str, str]] = set()
    seen_wording: set[tuple[str, str]] = set()
    for t in CATALOGUE:
        label = t.name
        if not NAME_RE.match(t.name):
            problems.append(f"{label}: invalid name")
        if (t.name, t.language) in seen_names:
            problems.append(f"{label}: duplicate name")
        seen_names.add((t.name, t.language))
        if (t.body, t.footer) in seen_wording:
            problems.append(f"{label}: same wording as another template")
        seen_wording.add((t.body, t.footer))
        if len(t.body) > BODY_MAX:
            problems.append(f"{label}: body too long")
        if len(t.header) > HEADER_MAX or len(t.footer) > FOOTER_MAX:
            problems.append(f"{label}: header or footer too long")
        numbers = [int(n) for n in _VARIABLE.findall(t.body)]
        if numbers != list(range(1, len(numbers) + 1)):
            problems.append(f"{label}: variables must be in order from 1")
        if len(numbers) != len(t.params) or len(numbers) != len(t.examples):
            problems.append(f"{label}: variables, params and examples differ")
        if t.is_authentication:  # Meta's fixed wording starts with the code, so the variable rule below does not apply
            if not 1 <= t.code_expiry_minutes <= 90:
                problems.append(f"{label}: code expiry must be 1 to 90 minutes")
        elif t.body.lstrip().startswith("{{") or t.body.rstrip().endswith("}}"):
            problems.append(f"{label}: body must not start or end with a variable")
        if "{{" in t.header or "{{" in t.footer:
            problems.append(f"{label}: variable outside the body")
        if t.category not in ("UTILITY", "MARKETING", "AUTHENTICATION"):
            problems.append(f"{label}: invalid category")
        if re.search(r"IIC|Instrumentation", " ".join((t.body, t.header, t.footer, *t.examples))):
            problems.append(f"{label}: names an individual centre")
    return problems
