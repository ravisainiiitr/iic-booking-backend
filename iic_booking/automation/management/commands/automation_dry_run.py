"""Dry run of every AI / automation switch: what each would do, counts only. Sends and changes nothing.

Safe for public CI logs: prints switch states, counts and equipment codes, never names, emails or message text.

    python manage.py automation_dry_run              # this week / today
    python manage.py automation_dry_run --weeks 4    # OIC alerts for each of the last 4 full weeks
    python manage.py automation_dry_run --days 14    # no-show prompts for each of the last 14 days
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Dry run of the AI and automation features (counts only, nothing is sent or changed)."

    def add_arguments(self, parser):
        parser.add_argument("--weeks", type=int, default=4)
        parser.add_argument("--days", type=int, default=14)
        parser.add_argument("--tickets", type=int, default=60)
        parser.add_argument("--finder-samples", type=int, default=200)

    def handle(self, *args, **opts):
        from iic_booking.automation.audit import settings_row
        from iic_booking.automation.models import AutomationSettings

        cfg = settings_row()
        w = self.stdout.write
        w("== switches ==")
        for f in ("instrument_finder_enabled", "instrument_finder_llm_enabled", "llm_daily_call_limit",
                  "smart_slots_enabled", "ticket_triage_enabled", "ticket_auto_assign_enabled", "oic_alerts_enabled",
                  "no_show_prompts_enabled", "no_show_grace_minutes"):
            w(f"  {f} = {getattr(cfg, f)!r}")
        w(f"  settings row saved={AutomationSettings.objects.filter(pk=1).exists()}")
        self._oic(w, opts["weeks"])
        self._no_show(w, opts["days"])
        self._tickets(w, opts["tickets"])
        self._finder(w, opts["finder_samples"])
        w("\nDONE (dry run; nothing sent or changed)")

    def _oic(self, w, weeks):
        from iic_booking.automation.oic_alerts import compute_alerts, last_full_week, recipients_for, week_key

        w(f"\n== weekly OIC anomaly alerts (last {weeks} full weeks) ==")
        monday = last_full_week()
        emails = []
        for i in range(max(1, weeks)):
            m = monday - timedelta(days=7 * i)
            _, alerts = compute_alerts(m)
            rec = recipients_for(alerts)
            kinds = Counter(f["kind"] for a in alerts for f in a.flags)
            emails.append(len(rec))
            codes = ", ".join(f"{a.code}:{'+'.join(f['kind'][:4] for f in a.flags)}" for a in alerts[:15])
            w(f"  {week_key(m)} equipment flagged={len(alerts)} flags={dict(kinds)} OIC emails={len(rec)} [{codes}]")
        w(f"  average OIC emails per week={sum(emails) / len(emails):.1f}")

    def _no_show(self, w, days):
        from iic_booking.automation.audit import settings_row
        from iic_booking.automation.no_show import candidates, recipients_for

        w(f"\n== likely no-show prompts (each of the last {days} days, as if run at 17:30) ==")
        grace = settings_row().no_show_grace_minutes
        today = timezone.localdate()
        total_b = total_e = 0
        for i in range(max(1, days), 0, -1):
            day = today - timedelta(days=i)
            at = timezone.make_aware(datetime.combine(day, time(17, 30)))
            # Today's status is used, so bookings since marked Completed / Not Utilized no longer count.
            rows = list(candidates(day=day, now=at, grace_minutes=grace))
            rec = recipients_for(rows)
            total_b += len(rows)
            total_e += len(rec)
            w(f"  {day} {day:%a} bookings={len(rows)} operator emails={len(rec)}")
        w(f"  per week: bookings={7 * total_b / max(1, days):.1f} emails={7 * total_e / max(1, days):.1f}")
        live = list(candidates(grace_minutes=grace))
        w(f"  right now (today): bookings={len(live)}")

    def _tickets(self, w, limit):
        from iic_booking.automation.ticket_triage import AUTO_ASSIGN_MIN_CONFIDENCE, triage
        from iic_booking.support.models import Ticket

        w(f"\n== ticket triage on the last {limit} tickets ==")
        rows = list(Ticket.objects.select_related("related_equipment", "user").order_by("-created_at")[:limit])
        topics, routes, agree = Counter(), Counter(), 0
        confident = with_eq = assignable = 0
        for t in rows:
            r = triage(t)
            topics[r.topic] += 1
            routes[r.route_role] += 1
            agree += int(r.ticket_type == t.ticket_type)
            with_eq += int(r.equipment is not None)
            confident += int(r.confidence >= AUTO_ASSIGN_MIN_CONFIDENCE)
            assignable += int(r.confidence >= AUTO_ASSIGN_MIN_CONFIDENCE and r.assignee is not None and not t.assigned_to_id)
        n = max(1, len(rows))
        w(f"  tickets={len(rows)} topics={dict(topics)} route={dict(routes)}")
        w(f"  suggested type equals the type the user chose={agree} ({100 * agree / n:.0f}%)")
        w(f"  equipment identified={with_eq} confident (>= {AUTO_ASSIGN_MIN_CONFIDENCE})={confident} "
          f"would be auto-assigned (unassigned + handler found)={assignable}")

    def _finder(self, w, limit):
        from iic_booking.automation.instrument_finder import analyse
        from iic_booking.research_copilot.models import KnowledgeGap

        w(f"\n== instrument finder rules on up to {limit} unanswered assistant questions ==")
        texts = list(KnowledgeGap.objects.order_by("-created_at").values_list("query_summary", flat=True)[:limit])
        hits = sum(1 for t in texts if analyse(t).techniques)
        w(f"  questions={len(texts)} with a technique suggestion={hits}")
        for probe in ("crystal structure of my thin film", "elements present in my powder", "band gap of nanoparticles",
                      "surface roughness of a coating", "thermal stability of polymer"):
            keys = [h.key for h in analyse(probe).techniques[:3]]
            w(f"  '{probe}' -> {keys}")
