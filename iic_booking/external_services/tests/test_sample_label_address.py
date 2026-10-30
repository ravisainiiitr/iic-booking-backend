"""The sender of a sample label is remembered: a new address typed in the label dialog goes into the address book
(reused when identical), and the next dialog preselects the address of the latest label. The address list also tells
the dialog whether QR labels are switched on."""

import pytest

from iic_booking.external_services.models import ExternalServicesSettings, SavedPostalAddress

from .conftest import client_for
from .test_logistics import ADDRESS, API, booking, on  # noqa: F401

pytestmark = pytest.mark.django_db


def _label(user, booking, body):
    r = client_for(user).post(f"{API}bookings/{booking.pk}/sample-label/", {"sample_count": 1, **body}, format="json")
    assert r.status_code == 200, r.content
    return r


def test_typed_sender_is_saved_once_and_preselected_next_time(on, booking):
    user = booking.user
    before = client_for(user).get(f"{API}addresses/").json()
    assert before["last_label_address_id"] is None and before["sample_labels_enabled"] is True

    _label(user, booking, {"sender": ADDRESS})
    _label(user, booking, {"sender": ADDRESS})  # the same address typed again is not duplicated
    saved = SavedPostalAddress.objects.get(user=user)
    assert saved.line1 == ADDRESS["line1"] and saved.recipient_name == ADDRESS["recipient_name"]

    body = client_for(user).get(f"{API}addresses/").json()
    assert body["last_label_address_id"] == saved.pk
    assert [a["id"] for a in body["addresses"]] == [saved.pk]


def test_latest_label_wins_over_older_addresses(on, booking):
    user = booking.user
    other = SavedPostalAddress.objects.create(user=user, recipient_name="Lab Two", line1="1 Mall Road", city="Delhi",
                                              state="Delhi", postal_code="110001", is_default=True)
    _label(user, booking, {"sender": ADDRESS})
    typed = SavedPostalAddress.objects.exclude(pk=other.pk).get(user=user)
    assert client_for(user).get(f"{API}addresses/").json()["last_label_address_id"] == typed.pk
    _label(user, booking, {"address_id": other.pk})
    assert client_for(user).get(f"{API}addresses/").json()["last_label_address_id"] == other.pk


def test_full_address_book_still_makes_the_label(on, booking):
    user = booking.user
    for i in range(20):
        SavedPostalAddress.objects.create(user=user, recipient_name=f"R{i}", line1="x", city="c", state="s",
                                          postal_code="247667")
    _label(user, booking, {"sender": ADDRESS})
    assert SavedPostalAddress.objects.filter(user=user).count() == 20
    assert client_for(user).get(f"{API}addresses/").json()["last_label_address_id"] is None


def test_switched_off_labels_are_reported(booking):
    s = ExternalServicesSettings.load()
    s.sample_labels_enabled = False
    s.save()
    assert client_for(booking.user).get(f"{API}addresses/").json()["sample_labels_enabled"] is False
