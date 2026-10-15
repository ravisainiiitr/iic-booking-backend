"""AI and automation: instrument finder, best slots, ticket triage, OIC anomaly alerts, no-show prompts, admin API."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from iic_booking.automation import instrument_finder, no_show, oic_alerts, slot_suggestions, ticket_triage
from iic_booking.automation.models import AutomationActionLog, AutomationSettings, TicketTriage
from iic_booking.research_copilot.tests.test_copilot_intelligence import _client, _World

API = "/api/v1/automation"
SEND = "iic_booking.communication.service.CommunicationService.send_email"


class _Lab(_World):
    def __init__(self):
        super().__init__()
        from iic_booking.users.models.user_type import UserType
        from iic_booking.users.tests.factories import UserFactory

        self.xrd = self.equipment("Bruker D8 Advance XRD", "XRD-B1")
        self.fesem = self.equipment("Field Emission Scanning Electron Microscope (FESEM)", "FESEM-01")
        self.tem = self.equipment("Transmission Electron Microscope", "TEM-200")
        self.admin = UserFactory(user_type=UserType.ADMIN, is_active=True)
        self.oic = UserFactory(user_type=UserType.FACULTY, department=self.department, is_active=True)
        self.operator = UserFactory(user_type=UserType.OPERATOR, department=self.department, is_active=True)
        from iic_booking.equipment.models import EquipmentManager, EquipmentOperator
        from iic_booking.users.models import User

        User.objects.filter(pk__in=[self.admin.pk, self.oic.pk, self.operator.pk]).update(is_active=True)
        for u in (self.admin, self.oic, self.operator):
            u.refresh_from_db()
        EquipmentManager.objects.create(equipment=self.xrd, manager=self.oic)
        EquipmentOperator.objects.create(equipment=self.xrd, operator=self.operator)


@pytest.fixture
def lab(db, settings):
    settings.COPILOT_LLM_PROVIDER = "fallback"
    return _Lab()


def _cfg(**kw) -> AutomationSettings:
    cfg = AutomationSettings.get_singleton()
    for k, v in kw.items():
        setattr(cfg, k, v)
    cfg.save()
    return cfg


# =============================================================================== defaults


@pytest.mark.django_db
def test_switches_that_send_or_act_are_off_by_default():
    cfg = AutomationSettings.get_singleton()
    assert cfg.instrument_finder_enabled and cfg.smart_slots_enabled  # read-only features
    assert not cfg.instrument_finder_llm_enabled
    assert not cfg.ticket_triage_enabled and not cfg.ticket_auto_assign_enabled
    assert not cfg.oic_alerts_enabled and not cfg.no_show_prompts_enabled


@pytest.mark.django_db
def test_migration_seeds_schedules_and_templates():
    import importlib

    from django.apps import apps
    from django_celery_beat.models import PeriodicTask

    from iic_booking.communication.models import CommunicationTemplate

    seed = importlib.import_module("iic_booking.automation.migrations.0001_initial").seed
    seed(apps, None)
    seed(apps, None)  # idempotent
    assert PeriodicTask.objects.filter(task__startswith="automation.").count() == 2
    tasks = set(PeriodicTask.objects.filter(task__startswith="automation.").values_list("task", flat=True))
    assert tasks == {"automation.send_weekly_oic_anomaly_alerts", "automation.send_no_show_prompts"}
    codes = set(CommunicationTemplate.objects.filter(code__startswith="automation_").values_list("code", flat=True))
    assert codes == {oic_alerts.TEMPLATE_CODE, no_show.TEMPLATE_CODE}


# =============================================================================== instrument finder


@pytest.mark.django_db
class TestInstrumentFinder:
    def test_crystal_structure_of_thin_film_is_xrd(self):
        res = instrument_finder.analyse("I want to know the crystal structure of my thin film")
        assert res.method == "rules"
        assert res.sample == "thin film"
        assert res.techniques[0].key == "xrd"
        assert "crystal structure" in res.techniques[0].why

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("surface morphology of nanoparticles", "fesem"),
            ("which elements are present in my alloy", "eds"),
            ("band gap of my semiconductor", "uv"),
            ("thermal stability of a polymer", "tga"),
        ],
    )
    def test_goals(self, text, expected):
        keys = [h.key for h in instrument_finder.analyse(text).techniques]
        assert expected in keys[:3], (text, keys)

    def test_ranks_visible_instruments_with_reasons(self, lab):
        out = instrument_finder.find_instruments(lab.student, "crystal structure of my thin film")
        assert out["results"], out
        top = out["results"][0]
        assert top["equipment_id"] == lab.xrd.pk
        assert top["reasons"] and top["book_href"].endswith(f"equipment_id={lab.xrd.pk}")
        assert lab.fesem.pk not in [r["equipment_id"] for r in out["results"][:1]]

    def test_hidden_equipment_is_never_suggested(self, lab):
        from iic_booking.users.models.user_group import UserGroup

        group = UserGroup.objects.create(name="G-hidden", code="GHID")
        lab.xrd.visibility_group = group
        lab.xrd.save(update_fields=["visibility_group"])
        out = instrument_finder.find_instruments(lab.student, "crystal structure of my thin film")
        assert lab.xrd.pk not in [r["equipment_id"] for r in out["results"]]

    def test_rules_hit_never_calls_the_model(self, lab):
        _cfg(instrument_finder_llm_enabled=True)
        with patch("iic_booking.research_copilot.services.llm_gateway.get_gateway") as gw:
            instrument_finder.analyse("crystal structure please", use_llm=True)
        gw.assert_not_called()

    def test_model_fallback_is_whitelisted_capped_and_strips_pii(self, lab):
        _cfg(instrument_finder_llm_enabled=True, llm_daily_call_limit=1)
        from django.core.cache import cache

        cache.clear()
        gateway = MagicMock()
        gateway.generate.return_value = SimpleNamespace(text='{"keys": ["xrd", "made_up"]}', provider="x", error_category="")
        text = "zzqx frobnicate my widget, mail me at someone@example.com or 9876543210"
        with patch("iic_booking.research_copilot.services.llm_gateway.get_gateway", return_value=gateway):
            res = instrument_finder.analyse(text, use_llm=True)
            again = instrument_finder.analyse(text, use_llm=True)
        assert [h.key for h in res.techniques] == ["xrd"] and res.method == "rules+ai"
        assert again.techniques == []  # daily cap of 1 reached
        sent = gateway.generate.call_args[0][0][1]["content"]
        assert "example.com" not in sent and "9876543210" not in sent
        actions = list(AutomationActionLog.objects.filter(kind="instrument_finder_llm").values_list("action", flat=True))
        assert sorted(actions) == ["called", "skipped"]

    def test_model_failure_falls_back_to_tips(self, lab):
        _cfg(instrument_finder_llm_enabled=True)
        with patch("iic_booking.research_copilot.services.llm_gateway.get_gateway", side_effect=RuntimeError("down")):
            out = instrument_finder.find_instruments(lab.student, "zzqx frobnicate", use_llm=True)
        assert out["results"] == [] and out["tips"]
        assert AutomationActionLog.objects.filter(kind="instrument_finder_llm", action="failed").exists()

    def test_api(self, lab):
        r = _client(lab.student).get(f"{API}/instrument-finder/", {"q": "crystal structure of my thin film"})
        assert r.status_code == 200 and r.data["enabled"] is True
        assert r.data["results"][0]["equipment_id"] == lab.xrd.pk
        _cfg(instrument_finder_enabled=False)
        r = _client(lab.student).get(f"{API}/instrument-finder/", {"q": "crystal structure"})
        assert r.data == {"enabled": False, "results": [], "techniques": []}


# =============================================================================== best slots


def _row(slot_id, start: datetime):
    return {"slot_id": slot_id, "date": start.date().isoformat(), "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat()}


def _at(day: date, hour: int) -> datetime:
    return timezone.make_aware(datetime(day.year, day.month, day.day, hour))


class TestRankRuns:
    TODAY = date(2026, 10, 1)

    def test_earliest_and_usual_time(self):
        runs = [
            [_row(1, _at(self.TODAY + timedelta(days=1), 9))],
            [_row(2, _at(self.TODAY + timedelta(days=2), 14))],
            [_row(3, _at(self.TODAY + timedelta(days=3), 14))],
            [_row(4, _at(self.TODAY + timedelta(days=9), 11))],
        ]
        out = slot_suggestions.rank_runs(runs, [14], today=self.TODAY)
        ids = [p["slot_ids"][0] for p in out]
        assert ids[0] == 1 and out[0]["reasons"] == ["Earliest free time"]
        usual = next(p for p in out if p["slot_ids"] == [2])
        assert usual["reasons"][0].startswith("Starts at your usual time")
        assert len({p["date"] for p in out}) == len(out)  # one suggestion per day
        assert all("_pref" not in p for p in out)

    def test_no_history_still_suggests(self):
        runs = [[_row(i, _at(self.TODAY + timedelta(days=i), 10))] for i in range(1, 9)]
        out = slot_suggestions.rank_runs(runs, [], today=self.TODAY)
        assert len(out) == slot_suggestions.MAX_SUGGESTIONS
        assert out[0]["slot_ids"] == [1]

    def test_empty(self):
        assert slot_suggestions.rank_runs([], [10], today=self.TODAY) == []


@pytest.mark.django_db
class TestSuggest:
    def test_back_to_back_runs_and_alternatives(self, lab):
        today = timezone.localdate()
        late = today + timedelta(days=8)
        rows = [_row(10, _at(late, 10)), _row(11, _at(late, 11)), _row(12, _at(late, 15))]
        alt_rows = [_row(20, _at(today + timedelta(days=1), 9)), _row(21, _at(today + timedelta(days=1), 10))]

        def fake(*, user, equipment_id, **kw):
            data = rows if equipment_id == lab.xrd.pk else alt_rows
            return SimpleNamespace(ok=True, rows=data, message="", slot_window_max_date=None)

        other = lab.equipment("Rigaku SmartLab XRD", "XRD-R2")
        with patch("iic_booking.research_copilot.services.v2.slot_availability.find_bookable_slots", side_effect=fake):
            out = slot_suggestions.suggest(lab.student, lab.xrd, 2, today=today)
        assert [s["slot_ids"] for s in out["suggestions"]] == [[10, 11]]
        assert [a["equipment_id"] for a in out["alternatives"]] == [other.pk]

    def test_api_switch(self, lab):
        _cfg(smart_slots_enabled=False)
        r = _client(lab.student).get(f"{API}/slot-suggestions/", {"equipment_id": lab.xrd.pk, "slot_count": 2})
        assert r.status_code == 200 and r.data["enabled"] is False
        _cfg(smart_slots_enabled=True)
        r = _client(lab.student).get(f"{API}/slot-suggestions/", {"equipment_id": "x"})
        assert r.status_code == 400


# =============================================================================== ticket triage


def _ticket(user, subject, description="", **kw):
    from iic_booking.support.models import Ticket

    return Ticket.objects.create(user=user, subject=subject, description=description, **kw)


@pytest.mark.django_db
class TestTicketTriage:
    def test_instrument_problem_routes_to_oic(self, lab):
        t = _ticket(lab.student, "XRD-B1 not working", "The detector shows an error, urgent for my thesis.")
        res = ticket_triage.triage(t)
        assert res.topic == "equipment_issue" and res.ticket_type == "equipment"
        assert res.equipment.pk == lab.xrd.pk and res.assignee.pk == lab.oic.pk and res.route_role == "oic"
        assert res.priority == "high"
        assert res.confidence >= ticket_triage.AUTO_ASSIGN_MIN_CONFIDENCE
        assert "Officer in Charge" in res.reply_draft

    def test_sample_question_goes_to_operator(self, lab):
        t = _ticket(lab.student, "Sample courier", "When should I submit my sample for XRD-B1?")
        res = ticket_triage.triage(t)
        assert res.topic == "sample" and res.assignee.pk == lab.operator.pk and res.route_role == "operator"

    def test_payment_stays_with_admin(self, lab):
        t = _ticket(lab.student, "Wallet refund missing", "My refund for a cancelled booking is not in the wallet")
        res = ticket_triage.triage(t)
        assert res.topic == "payment" and res.route_role == "admin" and res.assignee is None

    def test_unclear_ticket_is_low_confidence(self, lab):
        res = ticket_triage.triage(_ticket(lab.student, "Hello", "Question"))
        assert res.topic == "general" and res.confidence < 0.5 and res.assignee is None

    def test_off_by_default_no_row(self, lab, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            t = _ticket(lab.student, "XRD-B1 not working")
        assert not TicketTriage.objects.filter(ticket=t).exists()

    def test_on_suggests_but_does_not_assign(self, lab, django_capture_on_commit_callbacks):
        _cfg(ticket_triage_enabled=True)
        with django_capture_on_commit_callbacks(execute=True):
            t = _ticket(lab.student, "XRD-B1 not working", "vacuum fault")
        t.refresh_from_db()
        row = TicketTriage.objects.get(ticket=t)
        assert row.suggested_assignee_id == lab.oic.pk and row.status == "suggested"
        assert t.assigned_to_id is None
        assert AutomationActionLog.objects.filter(kind="ticket_triage", action="suggested", target_id=str(t.pk)).exists()

    def test_auto_assign_when_switched_on(self, lab, django_capture_on_commit_callbacks):
        _cfg(ticket_triage_enabled=True, ticket_auto_assign_enabled=True)
        with patch("iic_booking.support.ticket_service.notify_ticket_assignee") as notify, \
                django_capture_on_commit_callbacks(execute=True):
            t = _ticket(lab.student, "XRD-B1 not working", "vacuum fault")
        t.refresh_from_db()
        assert t.assigned_to_id == lab.oic.pk
        notify.assert_called_once()
        assert AutomationActionLog.objects.filter(kind="ticket_assign", action="assigned").count() == 1

    def test_auto_assign_never_overrides_existing_assignee(self, lab, django_capture_on_commit_callbacks):
        _cfg(ticket_triage_enabled=True, ticket_auto_assign_enabled=True)
        with django_capture_on_commit_callbacks(execute=True):
            t = _ticket(lab.student, "XRD-B1 not working", "vacuum fault", assigned_to=lab.operator)
        t.refresh_from_db()
        assert t.assigned_to_id == lab.operator.pk

    def test_decision_api(self, lab):
        _cfg(ticket_triage_enabled=True)
        t = _ticket(lab.student, "XRD-B1 not working", "urgent, thesis", related_equipment=lab.xrd)
        assert _client(lab.other).get(f"{API}/tickets/{t.pk}/triage/").status_code == 403
        r = _client(lab.admin).get(f"{API}/tickets/{t.pk}/triage/")
        assert r.status_code == 200 and r.data["saved"] is False and r.data["priority"] == "high"
        with patch("iic_booking.support.ticket_service.notify_ticket_assignee"), patch(SEND) as send:
            r = _client(lab.admin).post(f"{API}/tickets/{t.pk}/triage/decision/", {"accept": True}, format="json")
        assert r.status_code == 200 and r.data["status"] == "accepted"
        send.assert_not_called()  # the reply draft is never sent
        t.refresh_from_db()
        assert t.priority == "high" and t.assigned_to_id == lab.oic.pk and t.ticket_type == "equipment"
        assert AutomationActionLog.objects.filter(kind="ticket_triage", action="accepted").exists()

    def test_decision_needs_switch(self, lab):
        t = _ticket(lab.student, "XRD-B1 not working")
        r = _client(lab.admin).post(f"{API}/tickets/{t.pk}/triage/decision/", {"accept": True}, format="json")
        assert r.status_code == 409


# =============================================================================== OIC weekly alerts


def _past_booking(lab, eq, day: date, hours: int, status="BOOKED"):
    b, _ = lab.booking(lab.student, eq, _at(day, 9), slot_count=hours)
    if status != "BOOKED":
        b.status = status
        b.save(update_fields=["status"])
    return b


@pytest.mark.django_db
class TestOicAlerts:
    MONDAY = date(2026, 9, 21)

    def _baseline(self, lab):
        for w in range(1, 5):
            _past_booking(lab, lab.xrd, self.MONDAY - timedelta(days=7 * w), 6)

    def test_utilisation_drop_flagged(self, lab):
        self._baseline(lab)
        _past_booking(lab, lab.xrd, self.MONDAY + timedelta(days=1), 1)
        monday, alerts = oic_alerts.compute_alerts(self.MONDAY, cfg=AutomationSettings.get_singleton())
        flagged = {a.equipment_id: a for a in alerts}
        assert lab.xrd.pk in flagged
        assert [f["kind"] for f in flagged[lab.xrd.pk].flags] == ["utilisation_drop"]

    def test_steady_use_not_flagged(self, lab):
        self._baseline(lab)
        _past_booking(lab, lab.xrd, self.MONDAY + timedelta(days=1), 6)
        _, alerts = oic_alerts.compute_alerts(self.MONDAY, cfg=AutomationSettings.get_singleton())
        assert lab.xrd.pk not in [a.equipment_id for a in alerts]

    def test_disruptions_flagged(self, lab):
        from iic_booking.equipment.models import BookingEvent

        for _ in range(2):
            b = _past_booking(lab, lab.tem, self.MONDAY + timedelta(days=2), 1)
            ev = BookingEvent.objects.create(booking=b, event_type="ABSENT", new_status="ABSENT")
            BookingEvent.objects.filter(pk=ev.pk).update(created_at=_at(self.MONDAY + timedelta(days=2), 12))
        _, alerts = oic_alerts.compute_alerts(self.MONDAY, cfg=AutomationSettings.get_singleton())
        tem = next(a for a in alerts if a.equipment_id == lab.tem.pk)
        assert [f["kind"] for f in tem.flags] == ["repeated_disruptions"]

    def test_dry_run_and_switch_and_dedupe(self, lab):
        self._baseline(lab)
        with patch(SEND) as send:
            dry = oic_alerts.run(dry_run=True, monday=self.MONDAY)
            off = oic_alerts.run(dry_run=False, monday=self.MONDAY)
            assert send.call_count == 0
            assert dry["equipment_flagged"] == 1 and dry["recipients"] == 1 and off["sent"] == 0
            _cfg(oic_alerts_enabled=True)
            first = oic_alerts.run(dry_run=False, monday=self.MONDAY)
            second = oic_alerts.run(dry_run=False, monday=self.MONDAY)
        assert first["sent"] == 1 and second["sent"] == 0 and second["skipped"] == 1
        assert send.call_count == 1
        kwargs = send.call_args.kwargs
        assert kwargs["recipient"].pk == lab.oic.pk and kwargs["template"] == oic_alerts.TEMPLATE_CODE
        assert kwargs["metadata"]["notification_category"] == "staff"
        assert "XRD-B1" in kwargs["template_context"]["report_text"]

    def test_task_is_noop_when_off(self, lab):
        from iic_booking.automation.tasks import send_weekly_oic_anomaly_alerts

        with patch("iic_booking.equipment.peak_window.seconds_until_peak_end_for_deferral", return_value=0), \
                patch("iic_booking.automation.oic_alerts.run") as run:
            assert send_weekly_oic_anomaly_alerts() == {"skipped": "switched off"}
        run.assert_not_called()


# =============================================================================== no-show prompts


@pytest.mark.django_db
class TestNoShow:
    def _setup(self, lab):
        lab.xrd.sample_submission_lead_hours = 24
        lab.xrd.save(update_fields=["sample_submission_lead_hours"])
        day = timezone.localdate()
        b, _ = lab.booking(lab.student, lab.xrd, _at(day, 10), slot_count=2)
        now = _at(day, 20)
        return b, day, now

    def test_candidate_and_exclusions(self, lab):
        from iic_booking.equipment.models import BookingSampleTrace

        b, day, now = self._setup(lab)
        assert [x.pk for x in no_show.candidates(day=day, now=now)] == [b.pk]
        assert list(no_show.candidates(day=day, now=_at(day, 12) + timedelta(minutes=30))) == []  # grace not over
        BookingSampleTrace.objects.create(booking=b, status="SAMPLE_RECEIVED")
        assert list(no_show.candidates(day=day, now=now)) == []

    def test_walk_in_equipment_excluded(self, lab):
        b, day, now = self._setup(lab)
        lab.xrd.sample_submission_lead_hours = 0
        lab.xrd.sample_collect_deadline_hours = 0
        lab.xrd.save(update_fields=["sample_submission_lead_hours", "sample_collect_deadline_hours"])
        assert list(no_show.candidates(day=day, now=now)) == []

    def test_run_respects_switch_and_prompts_once(self, lab):
        b, day, now = self._setup(lab)
        with patch(SEND) as send:
            dry = no_show.run(dry_run=True, day=day, now=now)
            off = no_show.run(dry_run=False, day=day, now=now)
            assert dry["bookings"] == 1 and dry["recipients"] == 1 and off["emails_sent"] == 0
            _cfg(no_show_prompts_enabled=True)
            first = no_show.run(dry_run=False, day=day, now=now)
            second = no_show.run(dry_run=False, day=day, now=now)
        assert first["emails_sent"] == 1 and second["bookings"] == 0 and second["already_prompted"] == 1
        assert send.call_count == 1 and send.call_args.kwargs["recipient"].pk == lab.operator.pk
        b.refresh_from_db()
        assert b.status == "BOOKED"  # the prompt never changes the booking
        assert AutomationActionLog.objects.filter(kind="no_show_prompt", action="sent", target_id=str(b.pk)).count() == 1


# =============================================================================== admin API


@pytest.mark.django_db
class TestAdminApi:
    def test_settings_admin_only_and_logged(self, lab):
        assert _client(lab.student).get(f"{API}/settings/").status_code == 403
        assert _client(lab.oic).patch(f"{API}/settings/", {"oic_alerts_enabled": True}, format="json").status_code == 403
        r = _client(lab.admin).get(f"{API}/settings/")
        assert r.status_code == 200 and r.data["oic_alerts_enabled"] is False
        r = _client(lab.admin).patch(f"{API}/settings/", {"no_show_grace_minutes": 9999}, format="json")
        assert r.status_code == 400
        r = _client(lab.admin).patch(f"{API}/settings/", {"ticket_triage_enabled": True, "oic_alert_min_cancellations": 5}, format="json")
        assert r.status_code == 200 and r.data["ticket_triage_enabled"] is True and r.data["updated_by"]
        log = AutomationActionLog.objects.get(kind="settings")
        assert log.actor_id == lab.admin.pk and "ticket_triage_enabled" in log.summary

    @pytest.mark.parametrize("kind", ["oic-alerts", "no-show", "ticket-triage"])
    def test_previews_are_dry_runs(self, lab, kind):
        _ticket(lab.student, "XRD-B1 not working")
        assert _client(lab.oic).get(f"{API}/preview/{kind}/").status_code == 403
        with patch(SEND) as send:
            r = _client(lab.admin).get(f"{API}/preview/{kind}/")
        assert r.status_code == 200 and r.data["dry_run"] is True
        send.assert_not_called()
        assert not TicketTriage.objects.exists()

    def test_log_and_features(self, lab):
        _cfg(ticket_triage_enabled=True)
        _client(lab.admin).patch(f"{API}/settings/", {"oic_alerts_enabled": True}, format="json")
        r = _client(lab.admin).get(f"{API}/log/")
        assert r.status_code == 200 and r.data["count"] == 1
        assert _client(lab.student).get(f"{API}/log/").status_code == 403
        r = _client(lab.student).get(f"{API}/features/")
        assert r.data == {"instrument_finder": True, "smart_slots": True}
