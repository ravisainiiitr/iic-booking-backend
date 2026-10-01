"""Preferred slot on booking templates and the opt-in "if my slot is taken" fallback at submit time."""

from __future__ import annotations

import threading
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingInputTemplate, DailySlot
from iic_booking.equipment.template_slot_preference import resolve_preferred_slot
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import SubWalletRepository
from iic_booking.users.tests.factories import UserFactory

URL = "/api/booking-templates/"


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def _student_with_wallet(f, *, balance="10000.00", **limits):
    student = f.student()
    faculty = UserFactory(user_type=UserType.FACULTY, department=f.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=student,
        faculty=faculty,
        wallet=wallet,
        status=WalletJoinRequestStatus.APPROVED,
        responded_at=timezone.now() - timedelta(days=30),
        **limits,
    )
    sub = SubWalletRepository.get_or_create(wallet, f.department)
    sub.credit(Decimal(balance), description="Recharge")
    return student, sub


def _template(user, eq, start, *, if_slot_taken="ask", consented=False, slot_count=1):
    local = timezone.localtime(start)
    return BookingInputTemplate.objects.create(
        user=user,
        equipment=eq,
        name=f"T {local:%a %H%M} {if_slot_taken}",
        preferred_weekday=local.weekday(),
        preferred_start_time=local.time().replace(second=0, microsecond=0),
        preferred_slot_count=slot_count,
        if_slot_taken=if_slot_taken,
        if_slot_taken_consented_at=timezone.now() if consented else None,
    )


def _book(f, user, eq, slots, template=None):
    body = {
        "slot_ids": [s.pk for s in slots],
        "start_time": slots[0].start_datetime.isoformat(),
        "end_time": slots[-1].end_datetime.isoformat(),
        "input_values": {},
        "waitlist_on_failure": False,
    }
    if template is not None:
        body["booking_template_id"] = template.pk
    return f.client_for(user).post(f"/api/equipments/{eq.pk}/book/", body, format="json")


# --- storage and validation ---------------------------------------------------------------------


@pytest.mark.django_db
def test_preferred_slot_is_saved_and_serialized(egs_factory):
    eq = egs_factory.equipment()
    client = egs_factory.client_for(egs_factory.student())
    resp = client.post(
        URL,
        {"equipment": eq.pk, "name": "Wed 10", "preferred_slot": {"weekday": 2, "start_time": "10:00", "slot_count": 2}},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    assert resp.data["preferred_slot"] == {
        "weekday": 2, "weekday_name": "Wednesday", "start_time": "10:00", "slot_count": 2, "slot_master": None,
    }
    assert resp.data["if_slot_taken"] == "ask"
    assert resp.data["if_slot_taken_consented_at"] is None
    t = BookingInputTemplate.objects.get(pk=resp.data["id"])
    assert (t.preferred_weekday, t.preferred_start_time, t.preferred_slot_count) == (2, time(10, 0), 2)


@pytest.mark.django_db
def test_templates_without_preference_still_work(egs_factory):
    eq = egs_factory.equipment()
    client = egs_factory.client_for(egs_factory.student())
    resp = client.post(URL, {"equipment": eq.pk, "name": "Plain", "input_values": {"A": "1"}}, format="json")
    assert resp.status_code == 201
    assert resp.data["preferred_slot"] is None
    assert resp.data["if_slot_taken"] == "ask"
    pref = client.get(f"{URL}{resp.data['id']}/preferred-slot/")
    assert pref.status_code == 200
    assert pref.data == {"has_preference": False, "if_slot_taken": "ask"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "preferred, message",
    [
        ({"weekday": 7, "start_time": "10:00"}, "weekday"),
        ({"weekday": True, "start_time": "10:00"}, "weekday"),
        ({"weekday": 1, "start_time": "25:00"}, "start time"),
        ({"weekday": 1, "start_time": "10:00", "slot_count": 0}, "Number of slots"),
        ({"weekday": 1, "start_time": "10:00", "slot_count": 25}, "Number of slots"),
        ("Monday", "preferred_slot must be"),
    ],
)
def test_invalid_preferred_slot_is_rejected(egs_factory, preferred, message):
    eq = egs_factory.equipment()
    resp = egs_factory.client_for(egs_factory.student()).post(
        URL, {"equipment": eq.pk, "name": "Bad", "preferred_slot": preferred}, format="json"
    )
    assert resp.status_code == 400
    assert message in resp.data["error"]
    assert not BookingInputTemplate.objects.exists()


@pytest.mark.django_db
def test_slot_master_must_belong_to_the_equipment(egs_factory):
    eq, other = egs_factory.equipment(), egs_factory.equipment()
    own = egs_factory.slot(eq, egs_factory.future()).slot_master
    foreign = egs_factory.slot(other, egs_factory.future()).slot_master
    client = egs_factory.client_for(egs_factory.student())
    bad = client.post(
        URL,
        {"equipment": eq.pk, "name": "X", "preferred_slot": {"weekday": 1, "start_time": "10:00", "slot_master": foreign.pk}},
        format="json",
    )
    assert bad.status_code == 400
    ok = client.post(
        URL,
        {"equipment": eq.pk, "name": "Y", "preferred_slot": {"weekday": 1, "start_time": "10:00", "slot_master": own.pk}},
        format="json",
    )
    assert ok.status_code == 201
    assert ok.data["preferred_slot"]["slot_master"] == own.pk


@pytest.mark.django_db
def test_auto_booking_needs_explicit_consent(egs_factory):
    eq = egs_factory.equipment()
    client = egs_factory.client_for(egs_factory.student())
    body = {
        "equipment": eq.pk,
        "name": "Auto",
        "preferred_slot": {"weekday": 3, "start_time": "11:00"},
        "if_slot_taken": "next_available_same_day",
    }
    refused = client.post(URL, body, format="json")
    assert refused.status_code == 400
    assert "consent" in refused.data["error"]

    created = client.post(URL, {**body, "auto_book_consent": True}, format="json")
    assert created.status_code == 201, created.data
    assert created.data["if_slot_taken"] == "next_available_same_day"
    consented_at = created.data["if_slot_taken_consented_at"]
    assert consented_at

    # Editing other fields keeps the consent; switching mode needs it again; "ask" clears it.
    tid = created.data["id"]
    kept = client.patch(f"{URL}{tid}/", {"name": "Auto 2"}, format="json")
    assert kept.data["if_slot_taken_consented_at"] == consented_at
    switched = client.patch(f"{URL}{tid}/", {"if_slot_taken": "next_available_any"}, format="json")
    assert switched.status_code == 400
    assert client.patch(
        f"{URL}{tid}/", {"if_slot_taken": "next_available_any", "auto_book_consent": True}, format="json"
    ).status_code == 200
    cleared = client.patch(f"{URL}{tid}/", {"if_slot_taken": "ask"}, format="json")
    assert cleared.data["if_slot_taken"] == "ask"
    assert cleared.data["if_slot_taken_consented_at"] is None

    bogus = client.patch(f"{URL}{tid}/", {"if_slot_taken": "book_anything"}, format="json")
    assert bogus.status_code == 400


@pytest.mark.django_db
def test_removing_the_preferred_slot_resets_the_fallback(egs_factory):
    eq = egs_factory.equipment()
    client = egs_factory.client_for(egs_factory.student())
    created = client.post(
        URL,
        {
            "equipment": eq.pk, "name": "Auto", "preferred_slot": {"weekday": 3, "start_time": "11:00"},
            "if_slot_taken": "next_available_any", "auto_book_consent": True,
        },
        format="json",
    ).data
    resp = client.patch(f"{URL}{created['id']}/", {"preferred_slot": None}, format="json")
    assert resp.status_code == 200
    assert resp.data["preferred_slot"] is None
    assert resp.data["if_slot_taken"] == "ask"


@pytest.mark.django_db
def test_preferred_slot_endpoint_is_private(egs_factory):
    eq = egs_factory.equipment()
    template = _template(egs_factory.student(), eq, egs_factory.future())
    stranger = egs_factory.client_for(egs_factory.student())
    assert stranger.get(f"{URL}{template.pk}/preferred-slot/").status_code == 404


# --- next occurrence ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_next_occurrence_selects_the_free_preferred_slots(egs_factory):
    eq = egs_factory.equipment()
    user = egs_factory.student()
    start = egs_factory.future(days=3, hour=10)
    first, second = egs_factory.slot(eq, start), egs_factory.slot(eq, start + timedelta(hours=1))
    template = _template(user, eq, start, slot_count=2)

    resp = egs_factory.client_for(user).get(f"{URL}{template.pk}/preferred-slot/")
    assert resp.status_code == 200
    assert resp.data["status"] == "available"
    assert resp.data["slot_ids"] == [first.pk, second.pk]
    assert resp.data["date"] == timezone.localtime(start).date().isoformat()

    # The current inputs may need a different number of slots than the template saved.
    one = egs_factory.client_for(user).get(f"{URL}{template.pk}/preferred-slot/", {"slot_count": 1})
    assert one.data["slot_ids"] == [first.pk]
    assert egs_factory.client_for(user).get(f"{URL}{template.pk}/preferred-slot/", {"slot_count": 0}).status_code == 400


@pytest.mark.django_db
def test_occupied_preferred_slot_suggests_nearest_alternatives(egs_factory):
    eq = egs_factory.equipment()
    user = egs_factory.student()
    start = egs_factory.future(days=3, hour=10)
    egs_factory.booking(egs_factory.student(), eq, start)
    later = egs_factory.slot(eq, start + timedelta(hours=2))
    next_day = egs_factory.slot(eq, start + timedelta(days=1))
    template = _template(user, eq, start)

    data = resolve_preferred_slot(template, user)
    assert data["status"] == "occupied"
    assert "already booked" in data["message"]
    assert [a["slot_ids"] for a in data["alternatives"]] == [[later.pk], [next_day.pk]]
    assert data["auto_next"] is None


@pytest.mark.django_db
def test_occupied_preferred_slot_with_opt_in_previews_the_auto_choice(egs_factory):
    eq = egs_factory.equipment()
    user = egs_factory.student()
    start = egs_factory.future(days=3, hour=10)
    egs_factory.booking(egs_factory.student(), eq, start)
    earlier = egs_factory.slot(eq, start - timedelta(hours=1))
    later = egs_factory.slot(eq, start + timedelta(hours=3))
    template = _template(user, eq, start, if_slot_taken="next_available_same_day", consented=True)

    data = resolve_preferred_slot(template, user)
    assert data["status"] == "occupied"
    # "Next" means after the preferred time; the earlier slot is still offered as an alternative.
    assert data["auto_next"]["slot_ids"] == [later.pk]
    assert {tuple(a["slot_ids"]) for a in data["alternatives"]} == {(earlier.pk,), (later.pk,)}


@pytest.mark.django_db
def test_next_week_occurrence_is_not_open_before_the_wednesday_opening(egs_factory):
    eq = egs_factory.equipment(slot_window_reference_weekday=2, slot_window_reference_time=time(21, 0))
    user = egs_factory.student()
    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    now = timezone.make_aware(datetime.combine(monday, time(10, 0)))
    template = BookingInputTemplate.objects.create(
        user=user, equipment=eq, name="Mon 9", preferred_weekday=0, preferred_start_time=time(9, 0)
    )

    data = resolve_preferred_slot(template, user, now=now)
    assert data["status"] == "not_open"
    assert data["date"] == (monday + timedelta(days=7)).isoformat()
    assert data["window"]["opens_at"].startswith((monday + timedelta(days=2)).isoformat())
    assert "opens for booking on" in data["message"]
    assert "9:00 PM" in data["message"]

    # After the opening the same weekday next week is inside the window.
    after = timezone.make_aware(datetime.combine(monday + timedelta(days=2), time(21, 5)))
    later = resolve_preferred_slot(template, user, now=after)
    assert later["status"] == "no_matching_slot"
    assert later["date"] == (monday + timedelta(days=7)).isoformat()


@pytest.mark.django_db
def test_slot_master_wins_over_start_time_match(egs_factory):
    eq = egs_factory.equipment()
    user = egs_factory.student()
    start = egs_factory.future(days=3, hour=10)
    egs_factory.slot(eq, start)
    shifted = egs_factory.slot(eq, start + timedelta(hours=4))
    template = _template(user, eq, start)
    template.preferred_slot_master = shifted.slot_master
    template.save()
    assert resolve_preferred_slot(template, user)["slot_ids"] == [shifted.pk]


def _gapped_day(f, eq, *, days=3):
    """09:30-11:00, 11:30-13:00, 14:00-15:30, 16:00-17:30: slots with breaks between them."""
    day = f.future(days=days, hour=9)
    return [
        f.slot(eq, day + timedelta(minutes=offset), minutes=90)
        for offset in (30, 150, 300, 420)
    ]


@pytest.mark.django_db
def test_preferred_run_continues_across_breaks_between_slots(egs_factory):
    eq = egs_factory.equipment(slot_duration_minutes=90)
    user = egs_factory.student()
    rows = _gapped_day(egs_factory, eq)
    template = _template(user, eq, rows[1].start_datetime, slot_count=2)

    data = resolve_preferred_slot(template, user)
    assert data["status"] == "available"
    assert data["slot_ids"] == [rows[1].pk, rows[2].pk]

    last = _template(user, eq, rows[3].start_datetime, slot_count=2)
    assert resolve_preferred_slot(last, user)["status"] == "no_matching_slot"
    assert resolve_preferred_slot(template, user, slot_count=5)["status"] == "no_matching_slot"


@pytest.mark.django_db
def test_auto_next_and_alternatives_cross_breaks_but_not_booked_slots(egs_factory):
    eq = egs_factory.equipment(slot_duration_minutes=90)
    user = egs_factory.student()
    rows = _gapped_day(egs_factory, eq)
    rows[0].status = "BOOKED"
    rows[0].save(update_fields=["status"])
    template = _template(
        user, eq, rows[0].start_datetime, slot_count=2, if_slot_taken="next_available_same_day", consented=True
    )

    data = resolve_preferred_slot(template, user)
    assert data["status"] == "occupied"
    assert data["auto_next"]["slot_ids"] == [rows[1].pk, rows[2].pk]
    assert [a["slot_ids"] for a in data["alternatives"]] == [[rows[1].pk, rows[2].pk]]

    rows[2].status = "BOOKED"
    rows[2].save(update_fields=["status"])
    data = resolve_preferred_slot(template, user)
    assert data["auto_next"] is None
    assert data["alternatives"] == []


# --- submit: races and the opt-in fallback ---------------------------------------------------------


@pytest.mark.django_db
def test_second_submit_for_the_same_slot_loses_with_alternatives(egs_factory, no_portal_lock):
    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=10)
    slot = egs_factory.slot(eq, start)
    other = egs_factory.slot(eq, start + timedelta(hours=1))
    winner, _ = _student_with_wallet(egs_factory)
    loser, loser_wallet = _student_with_wallet(egs_factory)
    template = _template(loser, eq, start)

    first = _book(egs_factory, winner, eq, [slot])
    assert first.status_code == 201, first.data
    second = _book(egs_factory, loser, eq, [slot], template)
    assert second.status_code == 400
    assert second.data["slot_taken"] is True
    assert [a["slot_ids"] for a in second.data["slot_alternatives"]] == [[other.pk]]
    assert not Booking.objects.filter(user=loser).exists()
    loser_wallet.refresh_from_db()
    assert loser_wallet.balance == Decimal("10000.00")
    slot.refresh_from_db()
    assert slot.booking.user_id == winner.pk


@pytest.mark.django_db
def test_lost_slot_books_the_next_one_when_the_template_allows(egs_factory, no_portal_lock):
    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=10)
    slot = egs_factory.slot(eq, start)
    egs_factory.slot(eq, start - timedelta(hours=1))
    next_slot = egs_factory.slot(eq, start + timedelta(hours=2))
    egs_factory.slot(eq, start + timedelta(days=1))
    winner, _ = _student_with_wallet(egs_factory)
    student, wallet = _student_with_wallet(egs_factory)
    template = _template(student, eq, start, if_slot_taken="next_available_same_day", consented=True)

    assert _book(egs_factory, winner, eq, [slot]).status_code == 201
    resp = _book(egs_factory, student, eq, [slot], template)
    assert resp.status_code == 201, resp.data
    fallback = resp.data["slot_fallback"]
    assert fallback["slot_ids"] == [next_slot.pk]
    assert fallback["mode"] == "next_available_same_day"
    assert "was just taken" in fallback["message"]
    next_slot.refresh_from_db()
    assert next_slot.booking.user_id == student.pk
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("9990.00")


@pytest.mark.django_db
def test_same_day_fallback_never_moves_to_another_day(egs_factory, no_portal_lock):
    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=10)
    slot = egs_factory.slot(eq, start)
    tomorrow = egs_factory.slot(eq, start + timedelta(days=1))
    winner, _ = _student_with_wallet(egs_factory)
    student, _ = _student_with_wallet(egs_factory)
    same_day = _template(student, eq, start, if_slot_taken="next_available_same_day", consented=True)
    any_day = _template(student, eq, start, if_slot_taken="next_available_any", consented=True)

    assert _book(egs_factory, winner, eq, [slot]).status_code == 201
    refused = _book(egs_factory, student, eq, [slot], same_day)
    assert refused.status_code == 400
    assert [a["slot_ids"] for a in refused.data["slot_alternatives"]] == [[tomorrow.pk]]
    booked = _book(egs_factory, student, eq, [slot], any_day)
    assert booked.status_code == 201, booked.data
    assert booked.data["slot_fallback"]["slot_ids"] == [tomorrow.pk]


@pytest.mark.django_db
def test_fallback_keeps_the_run_length(egs_factory, no_portal_lock):
    eq = egs_factory.equipment(time_formula="120")
    start = egs_factory.future(days=3, hour=10)
    wanted = [egs_factory.slot(eq, start), egs_factory.slot(eq, start + timedelta(hours=1))]
    egs_factory.slot(eq, start + timedelta(hours=3))  # lone free slot: the next row is booked
    egs_factory.booking(egs_factory.student(), eq, start + timedelta(hours=4))
    run = [egs_factory.slot(eq, start + timedelta(hours=5)), egs_factory.slot(eq, start + timedelta(hours=6))]
    winner, _ = _student_with_wallet(egs_factory)
    student, _ = _student_with_wallet(egs_factory)
    template = _template(student, eq, start, if_slot_taken="next_available_same_day", consented=True, slot_count=2)

    assert _book(egs_factory, winner, eq, [wanted[1]]).status_code == 201
    resp = _book(egs_factory, student, eq, wanted, template)
    assert resp.status_code == 201, resp.data
    assert resp.data["slot_fallback"]["slot_ids"] == [s.pk for s in run]
    assert resp.data["total_time_minutes"] == 120


@pytest.mark.django_db
def test_slot_taken_between_checks_and_lock_uses_the_locked_fallback(egs_factory, monkeypatch, no_portal_lock):
    from iic_booking.equipment import api_views

    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=10)
    slot = egs_factory.slot(eq, start)
    next_slot = egs_factory.slot(eq, start + timedelta(hours=1))
    rival = egs_factory.student()
    student, _ = _student_with_wallet(egs_factory)
    template = _template(student, eq, start, if_slot_taken="next_available_same_day", consented=True)

    real_limit_check = api_views._student_spending_limit_response

    def _rival_books_first(*args, **kwargs):
        # Runs after every pre-lock check passed with the slot still free: the race is lost at the lock.
        rival_booking = egs_factory.booking(rival, eq, start + timedelta(days=5))
        DailySlot.objects.filter(pk=slot.pk).update(status="BOOKED", booking=rival_booking)
        return real_limit_check(*args, **kwargs)

    monkeypatch.setattr(api_views, "_student_spending_limit_response", _rival_books_first)
    resp = _book(egs_factory, student, eq, [slot], template)
    assert resp.status_code == 201, resp.data
    assert resp.data["slot_fallback"]["slot_ids"] == [next_slot.pk]


@pytest.mark.django_db
def test_fallback_still_enforces_spending_limits(egs_factory, no_portal_lock):
    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=10)
    slot = egs_factory.slot(eq, start)
    egs_factory.slot(eq, start + timedelta(hours=1))
    winner, _ = _student_with_wallet(egs_factory)
    student, wallet = _student_with_wallet(egs_factory, spending_limit_enabled=True, weekly_limit_inr=5)
    template = _template(student, eq, start, if_slot_taken="next_available_same_day", consented=True)

    assert _book(egs_factory, winner, eq, [slot]).status_code == 201
    resp = _book(egs_factory, student, eq, [slot], template)
    assert resp.status_code == 400
    assert "spending limit" in resp.data["error"]
    assert not Booking.objects.filter(user=student).exists()
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("10000.00")


@pytest.mark.django_db
def test_fallback_is_off_without_consent_or_for_someone_elses_template(egs_factory, no_portal_lock):
    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=10)
    slot = egs_factory.slot(eq, start)
    egs_factory.slot(eq, start + timedelta(hours=1))
    winner, _ = _student_with_wallet(egs_factory)
    student, _ = _student_with_wallet(egs_factory)
    other, _ = _student_with_wallet(egs_factory)
    unconsented = _template(student, eq, start, if_slot_taken="next_available_same_day", consented=False)
    foreign = _template(other, eq, start, if_slot_taken="next_available_same_day", consented=True)

    assert _book(egs_factory, winner, eq, [slot]).status_code == 201
    assert _book(egs_factory, student, eq, [slot], unconsented).status_code == 400
    assert _book(egs_factory, student, eq, [slot], foreign).status_code == 400
    assert not Booking.objects.filter(user=student).exists()


@pytest.mark.django_db
def test_free_slot_with_template_books_normally(egs_factory, no_portal_lock):
    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=10)
    slot = egs_factory.slot(eq, start)
    student, _ = _student_with_wallet(egs_factory)
    template = _template(student, eq, start, if_slot_taken="next_available_any", consented=True)
    resp = _book(egs_factory, student, eq, [slot], template)
    assert resp.status_code == 201, resp.data
    assert "slot_fallback" not in resp.data


# --- real concurrency (PostgreSQL row locks) -------------------------------------------------------


def _concurrent_submits(f, monkeypatch, eq, slot, students, templates):
    from django.db import close_old_connections

    from iic_booking.equipment import api_views

    barrier = threading.Barrier(len(students), timeout=30)
    real_limit_check = api_views._student_spending_limit_response

    def _line_up(*args, **kwargs):
        # Every request has passed its pre-lock checks with the slot free before any of them locks it.
        barrier.wait()
        return real_limit_check(*args, **kwargs)

    monkeypatch.setattr(api_views, "_student_spending_limit_response", _line_up)
    results = [None] * len(students)

    def _run(i):
        try:
            results[i] = _book(f, students[i], eq, [slot], templates[i] if templates else None)
        finally:
            close_old_connections()
            connection.close()

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(len(students))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


postgres_only = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Row-lock race needs PostgreSQL (SQLite ignores SELECT FOR UPDATE)"
)


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_concurrent_submits_for_one_slot_have_exactly_one_winner(egs_factory, monkeypatch, no_portal_lock):
    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=10)
    slot = egs_factory.slot(eq, start)
    students = [_student_with_wallet(egs_factory)[0] for _ in range(8)]

    results = _concurrent_submits(egs_factory, monkeypatch, eq, slot, students, None)
    codes = sorted(r.status_code for r in results)
    assert codes == [201] + [400] * 7, [r.data for r in results]
    assert Booking.objects.filter(equipment=eq).count() == 1
    slot.refresh_from_db()
    assert slot.status == "BOOKED"


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_concurrent_auto_next_submits_get_distinct_slots(egs_factory, monkeypatch, no_portal_lock):
    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=8)
    slot = egs_factory.slot(eq, start)
    spare = [egs_factory.slot(eq, start + timedelta(hours=h)) for h in range(1, 4)]
    students = [_student_with_wallet(egs_factory)[0] for _ in range(6)]
    templates = [
        _template(s, eq, start, if_slot_taken="next_available_same_day", consented=True) for s in students
    ]

    results = _concurrent_submits(egs_factory, monkeypatch, eq, slot, students, templates)
    winners = [r for r in results if r.status_code == 201]
    # One gets the preferred slot, three get the three later slots, two find nothing left.
    assert len(winners) == 4, [r.data for r in results]
    assert sum(1 for r in winners if "slot_fallback" in r.data) == 3
    booked = list(DailySlot.objects.filter(slot_master__equipment=eq, status="BOOKED").values_list("booking_id", flat=True))
    assert len(booked) == 4 and len(set(booked)) == 4
    assert {s.pk for s in [slot, *spare]} == set(
        DailySlot.objects.filter(slot_master__equipment=eq, status="BOOKED").values_list("pk", flat=True)
    )
