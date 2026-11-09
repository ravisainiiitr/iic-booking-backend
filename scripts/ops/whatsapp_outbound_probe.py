"""READ-ONLY: why a portal user did or did not get WhatsApp messages (9 Nov 2026).

Prints only states, counts, event names, statuses and error text (runs of 8 or more digits masked), never a phone
number, email, message text or Meta message id: the run log is public. Input: USER_ID (portal user id), optional
BOOKING_REF (virtual booking id or booking pk). Writes nothing.
"""

import os
import re

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django.setup()

from iic_booking.equipment.models import Booking  # noqa: E402
from iic_booking.whatsapp import conf  # noqa: E402
from iic_booking.whatsapp.models import WhatsAppConsentEvent, WhatsAppMessage, WhatsAppOptIn  # noqa: E402
from iic_booking.whatsapp.template_catalogue import BY_EVENT  # noqa: E402
from iic_booking.whatsapp.templates import is_approved  # noqa: E402


def scrub(text) -> str:
    return re.sub(r"\d{8,}", lambda m: "*" * (len(m.group()) - 2) + m.group()[-2:], str(text or ""))[:200]


uid = int(os.environ.get("USER_ID") or 0)
ref = (os.environ.get("BOOKING_REF") or "").strip()

print("SENDING enabled=", conf.enabled(), "pilot_numbers=", len(conf.pilot_numbers()), "allow_all=", conf.allow_all(),
      "waba_set=", bool(conf.waba_id()))
for event in ("booking_confirmed", "payment_successful", "booking_cancelled", "booking_completed", "booking_reminder"):
    entry = BY_EVENT.get(event)
    if entry:
        name = conf.template_name(event)
        print("TEMPLATE", event, "name=", name, "approved=", is_approved(name, entry.language))

row = WhatsAppOptIn.objects.filter(user_id=uid).first()
if row is None:
    print("OPT-IN user", uid, ": no record (never answered, so nothing is sent)")
else:
    print("OPT-IN user", uid, ": opted_in=", row.opted_in, "source=", row.source, "has_number=", bool(row.phone_e164),
          "number_on_pilot_list=", bool(row.phone_e164) and conf.may_send_to(row.phone_e164),
          "opted_in_at=", row.opted_in_at, "opted_out_at=", row.opted_out_at)


def shape(number: str) -> str:
    """A number's shape without the number: length, first 3 characters, last 2 digits (e.g. len=13 +91…10)."""
    n = str(number or "")
    return f"len={len(n)} {n[:3]}…{n[-2:]}" if len(n) > 5 else f"len={len(n)}"


import django.conf as _dc  # noqa: E402

raw = getattr(_dc.settings, "WHATSAPP_PILOT_NUMBERS", "")
raw_items = raw if isinstance(raw, (list, tuple)) else [x for x in str(raw).split(",")]
print("PILOT raw entries=", len(raw_items), "raw shapes=", [shape(str(x).strip()) for x in raw_items])
print("PILOT normalised shapes=", [shape(x) for x in sorted(conf.pilot_numbers())])
if row is not None:
    print("OPT-IN number shape=", shape(row.phone_e164))
for e in WhatsAppConsentEvent.objects.filter(user_id=uid).order_by("id")[:20]:
    print("CONSENT", e.created_at, e.action, e.source)

msgs = WhatsAppMessage.objects.filter(user_id=uid).order_by("id")
print("MESSAGES user", uid, "count=", msgs.count())
for m in msgs[:50]:
    print("  MSG", m.pk, m.created_at, m.event, m.template, m.status, "key=", scrub(m.event_key), "error=", scrub(m.error))

if ref:
    b = Booking.objects.filter(virtual_booking_id=ref).first()
    if b is None and ref.isdigit():
        b = Booking.objects.filter(pk=int(ref)).first()
    if b is None:
        print("BOOKING", ref, "not found")
    else:
        print("BOOKING", b.pk, "status=", b.status, "user_id=", b.user_id, "created_at=", b.created_at,
              "payment_settled_at=", b.payment_settled_at)
        for ev in b.events.order_by("created_at")[:30]:
            print("  EVENT", ev.created_at, ev.event_type, getattr(ev, "previous_status", ""), "->",
                  getattr(ev, "new_status", ""))
        bm = WhatsAppMessage.objects.filter(event_key__startswith=f"booking:{b.pk}:")
        print("  BOOKING MESSAGES count=", bm.count())
        for m in bm:
            print("  MSG", m.pk, m.created_at, m.event, m.status, "error=", scrub(m.error))
