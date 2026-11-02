"""UPS health analysis: turns a UPS's readings and power events into findings an Officer in Charge can act on.

Each check looks for an existing problem or for an early sign of one, from the UPS's own history (up to 30 days of
5-minute points and 90 days of power events), compared with the UPS's own baseline where that makes sense:

- current state        on battery, low battery, fault / bypass, overload, battery replacement flag (from the alerts)
- backup time          runtime at full charge shorter than the instruments need to shut down safely
- battery health       runtime-at-load at full charge falling against the first weeks of history; float voltage
                       falling; a battery that drained faster than the UPS predicted during a real power cut
- battery age          years since the batteries were fitted (valve-regulated lead-acid lasts about 3-5 years,
                       less when warm)
- temperature          sustained heat (battery life roughly halves for every 10 °C above 25 °C), rising trend
- mains quality        frequent power cuts, input voltage outside ±10 % of nominal, unsteady frequency
- load                 load growing towards the configured limit (straight-line trend of daily medians)
- connectivity         the network card often unreachable
- anomalies            a metric far outside its own 7-day range (robust z-score), anything the rules do not name

Levels: ``urgent`` (act now), ``attention`` (early attention: plan action within days), ``watch`` (no action yet).
The checks are deterministic so the same data always gives the same findings; the language model only words the
summary (see ``advisor``).
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from django.utils import timezone

from . import service
from .models import UpsDevice, UpsPowerEvent, UpsReading

LEVEL_ORDER = {"ok": 0, "watch": 1, "attention": 2, "urgent": 3}
WEIGHT = {"watch": 5, "attention": 20, "urgent": 50}
FULL_CHARGE = 95  # battery % from which a runtime reading counts as "at full charge"


@dataclass
class Finding:
    code: str
    level: str
    title: str
    detail: str
    action: str
    evidence: dict = field(default_factory=dict)


@dataclass
class Assessment:
    level: str
    risk_score: int
    findings: list[Finding]
    metrics: dict

    def findings_dicts(self) -> list[dict]:
        return [asdict(f) for f in self.findings]


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _mad_z(value, baseline):
    """Robust z-score of ``value`` against ``baseline`` (median / median absolute deviation)."""
    base = [v for v in baseline if v is not None]
    if value is None or len(base) < 24:
        return None
    med = statistics.median(base)
    mad = statistics.median(abs(v - med) for v in base)
    if mad == 0:
        return None
    return (value - med) / (1.4826 * mad)


def _slope_per_day(points):
    """Least-squares slope (units per day) of [(days, value)]."""
    if len(points) < 5:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return None if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _round(v, n=1):
    return None if v is None else round(v, n)


def assess(device: UpsDevice, now=None) -> Assessment:
    now = now or timezone.now()
    readings = list(UpsReading.objects.filter(device=device, read_at__gte=now - timedelta(days=30)).order_by("read_at"))
    events = list(UpsPowerEvent.objects.filter(device=device, started_at__gte=now - timedelta(days=90)))
    ok = [r for r in readings if r.reachable]
    day = [r for r in ok if r.read_at >= now - timedelta(hours=24)]
    week = [r for r in ok if r.read_at >= now - timedelta(days=7)]
    latest = readings[-1] if readings else None
    findings: list[Finding] = []
    metrics: dict = {"readings_30d": len(readings), "history_days": 0}
    if readings:
        metrics["history_days"] = round((now - readings[0].read_at).total_seconds() / 86400, 1)

    if not device.map_verified:
        findings.append(Finding(
            "map_unverified", "watch", "Readings not yet verified",
            "The Modbus register addresses for this UPS have not been checked against its network card, so the "
            "numbers below may be wrong.",
            "Ask the department administrator to verify the register map and tick 'map verified'."))

    # --- current state (same rules as the alerts) ---
    if latest is not None:
        if latest.read_at < now - service.SILENT_AFTER:
            findings.append(Finding(
                "no_data", "attention", "No recent data",
                f"The last reading is from {timezone.localtime(latest.read_at):%d %b %H:%M}.",
                "Check that the department server and the UPS network card are on and connected."))
        else:
            for rule, (severity, title, message) in service.conditions(device, latest).items():
                level = "urgent" if severity == "critical" else "attention"
                action = {
                    "ups_on_battery": "Save work and be ready to shut instruments down before the backup time runs out.",
                    "ups_low_battery": "Shut down sensitive instruments safely now.",
                    "ups_fault": "Call the UPS service engineer; the instruments are not protected.",
                    "ups_overload": "Move non-essential loads off this UPS.",
                    "ups_replace_battery": "Arrange a battery replacement.",
                    "ups_unreachable": "Check the UPS network card's power and network cable.",
                }.get(rule, "Check the UPS.")
                findings.append(Finding(rule.removeprefix("ups_"), level, title.split(": ", 1)[-1].capitalize(),
                                        message, action))
    else:
        findings.append(Finding("no_data", "attention", "No data yet",
                                "The sync agent has not sent any reading for this UPS.",
                                "Check the UPS address in the portal and that the department server is running."))

    # --- backup time at full charge ---
    full = [r for r in week if r.battery_percent is not None and r.battery_percent >= FULL_CHARGE
            and r.runtime_minutes is not None and not r.on_battery]
    runtime_now = _median([r.runtime_minutes for r in full[-12:]])
    metrics["runtime_full_minutes"] = _round(runtime_now)
    if runtime_now is not None and runtime_now < device.min_runtime_minutes:
        level = "urgent" if runtime_now < device.min_runtime_minutes / 2 else "attention"
        findings.append(Finding(
            "short_backup", level, "Backup time too short",
            f"At full charge the UPS reports about {runtime_now:.0f} min of backup at the present load; the instruments "
            f"need {device.min_runtime_minutes} min to shut down safely.",
            "Reduce the load on this UPS or have the batteries tested and replaced.",
            {"runtime_minutes": _round(runtime_now), "needed_minutes": device.min_runtime_minutes}))

    # --- battery health: runtime x load at full charge, now against the earliest history ---
    def energy(rs):
        return [r.runtime_minutes * r.load_percent for r in rs
                if r.battery_percent is not None and r.battery_percent >= FULL_CHARGE and not r.on_battery
                and r.runtime_minutes and r.load_percent and r.load_percent >= 5]

    recent = energy([r for r in ok if r.read_at >= now - timedelta(days=3)])
    early_cut = readings[0].read_at + timedelta(days=7) if readings else now
    early = energy([r for r in ok if r.read_at < min(early_cut, now - timedelta(days=10))])
    if len(recent) >= 12 and len(early) >= 12:
        drop = 1 - statistics.median(recent) / statistics.median(early)
        metrics["capacity_change_percent"] = round(-drop * 100, 1)
        if drop >= 0.2:
            findings.append(Finding(
                "battery_capacity_falling", "urgent" if drop >= 0.4 else "attention", "Battery capacity falling",
                f"For the same load, the backup time at full charge is about {drop * 100:.0f}% lower than in the "
                "first week of monitoring. Batteries usually fade like this before they fail.",
                "Have the batteries load-tested and plan their replacement.",
                {"drop_percent": round(drop * 100, 1)}))

    float_recent = _median([r.battery_voltage for r in ok if r.read_at >= now - timedelta(days=3)
                            and r.battery_percent is not None and r.battery_percent >= FULL_CHARGE and not r.on_battery])
    float_early = _median([r.battery_voltage for r in ok if r.read_at < min(early_cut, now - timedelta(days=10))
                           and r.battery_percent is not None and r.battery_percent >= FULL_CHARGE and not r.on_battery])
    if float_recent and float_early:
        change = float_recent / float_early - 1
        metrics["float_voltage_change_percent"] = round(change * 100, 1)
        if change <= -0.05:
            findings.append(Finding(
                "battery_voltage_falling", "attention", "Battery voltage dropping",
                f"Fully charged battery voltage is {float_recent:.1f} V, {abs(change) * 100:.0f}% below the "
                f"{float_early:.1f} V seen at the start of monitoring. A weak cell or charger problem can cause this.",
                "Have the battery string and charger checked.",
                {"now_v": _round(float_recent), "before_v": _round(float_early)}))

    # --- real discharge against the UPS's own estimate ---
    worst = None
    for e in events:
        if e.kind != "on_battery" or e.minutes < 3 or not e.start_runtime_minutes:
            continue
        if e.start_battery_percent is None or e.end_battery_percent is None:
            continue
        observed = (e.start_battery_percent - e.end_battery_percent) / e.minutes
        expected = e.start_battery_percent / e.start_runtime_minutes
        if expected > 0:
            ratio = observed / expected
            if worst is None or ratio > worst[0]:
                worst = (ratio, e)
    if worst is not None:
        metrics["discharge_ratio"] = round(worst[0], 2)
        if worst[0] >= 1.5:
            e = worst[1]
            findings.append(Finding(
                "fast_discharge", "urgent" if worst[0] >= 2.5 else "attention", "Battery drains faster than expected",
                f"During the power cut on {timezone.localtime(e.started_at):%d %b %H:%M} the battery fell from "
                f"{e.start_battery_percent:.0f}% to {e.end_battery_percent:.0f}% in {e.minutes:.0f} min, about "
                f"{worst[0]:.1f}x faster than the UPS's own runtime estimate. The real backup time is likely shorter "
                "than shown.",
                "Have the batteries load-tested; do not rely on the displayed backup time.",
                {"ratio": round(worst[0], 2)}))

    # --- battery age ---
    temp_week = _median([r.temperature_c for r in week])
    if device.battery_installed_on:
        years = (timezone.localdate(now) - device.battery_installed_on).days / 365.25
        life = 4.0 if (temp_week or 25) <= 30 else 3.0
        metrics["battery_age_years"] = round(years, 1)
        if years >= life:
            findings.append(Finding(
                "battery_old", "attention", "Batteries at end of expected life",
                f"The batteries were fitted {years:.1f} years ago; batteries of this kind usually last about "
                f"{life:.0f} years{' at this temperature' if life < 4 else ''}.",
                "Plan and budget a battery replacement.", {"years": round(years, 1)}))
        elif years >= life - 0.5:
            findings.append(Finding(
                "battery_ageing", "watch", "Batteries approaching end of life",
                f"The batteries were fitted {years:.1f} years ago (expected life about {life:.0f} years).",
                "Start planning the replacement.", {"years": round(years, 1)}))

    # --- temperature ---
    temp_day = _median([r.temperature_c for r in day])
    metrics["temperature_c"] = _round(temp_day)
    if temp_day is not None:
        if temp_day >= 35:
            findings.append(Finding(
                "too_hot", "urgent", "UPS running hot",
                f"The UPS has been at about {temp_day:.0f} °C over the last day.",
                "Check the room cooling and the UPS fans and vents now.", {"temperature_c": _round(temp_day)}))
        elif temp_day >= 30:
            findings.append(Finding(
                "warm", "attention", "UPS room warm",
                f"The UPS has been at about {temp_day:.0f} °C over the last day. Battery life roughly halves for every "
                "10 °C above 25 °C.",
                "Improve cooling around the UPS.", {"temperature_c": _round(temp_day)}))
    daily_temp = _daily(ok, "temperature_c", now, 14)
    slope = _slope_per_day(daily_temp)
    if slope is not None and slope >= 0.3 and (temp_day or 0) >= 27:
        findings.append(Finding(
            "temperature_rising", "watch", "Temperature creeping up",
            f"The UPS temperature has risen by about {slope:.1f} °C a day over the last two weeks.",
            "Check the room air-conditioning and the UPS fans.", {"per_day": round(slope, 2)}))

    # --- mains quality ---
    cuts = [e for e in events if e.kind == "on_battery"]
    cuts_day = [e for e in cuts if e.started_at >= now - timedelta(hours=24)]
    cuts_week = [e for e in cuts if e.started_at >= now - timedelta(days=7)]
    metrics["power_cuts_24h"], metrics["power_cuts_7d"] = len(cuts_day), len(cuts_week)
    metrics["minutes_on_battery_7d"] = round(sum(e.minutes for e in cuts_week), 1)
    if len(cuts_day) >= 3 or len(cuts_week) >= 10:
        findings.append(Finding(
            "frequent_power_cuts", "attention", "Frequent power cuts",
            f"{len(cuts_day)} power cut(s) in the last 24 hours and {len(cuts_week)} in the last 7 days "
            f"({metrics['minutes_on_battery_7d']:.0f} min on battery). Each cut wears the battery.",
            "Report the supply problem to the electrical maintenance section.",
            {"day": len(cuts_day), "week": len(cuts_week)}))
    volts = [r.input_voltage for r in day if r.input_voltage is not None and not r.on_battery]
    if len(volts) >= 12:
        nominal = device.nominal_input_voltage or 230
        outside = sum(1 for v in volts if abs(v - nominal) > 0.1 * nominal) / len(volts)
        metrics["input_voltage_outside_percent"] = round(outside * 100, 1)
        metrics["input_voltage_range"] = [round(min(volts)), round(max(volts))]
        if outside >= 0.05:
            findings.append(Finding(
                "unstable_mains", "attention", "Mains voltage out of range",
                f"Input voltage was outside {nominal} V ±10% for {outside * 100:.0f}% of the last day "
                f"({min(volts):.0f}-{max(volts):.0f} V). The UPS works harder and the battery wears faster.",
                "Ask the electrical maintenance section to check the supply to this lab.",
                {"outside_percent": round(outside * 100, 1)}))
    freq = [r.output_frequency for r in day if r.output_frequency is not None]
    if len(freq) >= 12 and max(abs(f - 50) for f in freq) > 1:
        findings.append(Finding(
            "frequency_unsteady", "watch", "Output frequency unsteady",
            f"Output frequency ranged {min(freq):.1f}-{max(freq):.1f} Hz over the last day.",
            "Have the UPS checked at the next service.", {}))

    # --- load trend ---
    daily_load = _daily(ok, "load_percent", now, 14)
    load_now = _median([r.load_percent for r in day])
    metrics["load_percent"] = _round(load_now)
    slope = _slope_per_day(daily_load)
    if slope is not None:
        metrics["load_trend_per_day"] = round(slope, 2)
    if load_now is not None and load_now <= device.high_load_percent and slope and slope > 0.1:
        days_left = (device.high_load_percent - load_now) / slope
        if days_left <= 30:
            findings.append(Finding(
                "load_growing", "attention" if days_left <= 7 else "watch", "Load growing towards the limit",
                f"Load is about {load_now:.0f}% and rising {slope:.1f} points a day; at this rate it reaches "
                f"{device.high_load_percent}% in about {days_left:.0f} days.",
                "Check what was added to this UPS and move non-essential equipment off it.",
                {"days_left": round(days_left)}))
    elif load_now is not None and 80 <= load_now <= device.high_load_percent:
        findings.append(Finding(
            "load_high", "watch", "Load close to the limit",
            f"Load is about {load_now:.0f}% of the UPS capacity; backup time is short at high load.",
            "Avoid adding equipment to this UPS.", {"load_percent": _round(load_now)}))

    # --- connectivity ---
    day_all = [r for r in readings if r.read_at >= now - timedelta(hours=24)]
    if len(day_all) >= 12:
        down = sum(1 for r in day_all if not r.reachable) / len(day_all)
        metrics["unreachable_percent_24h"] = round(down * 100, 1)
        if down >= 0.1 and not any(f.code == "unreachable" for f in findings):
            findings.append(Finding(
                "flaky_network", "watch", "Network card drops out",
                f"The UPS could not be read {down * 100:.0f}% of the time over the last day, so a problem could go "
                "unnoticed.",
                "Check the UPS network card and its cable or switch port.", {"percent": round(down * 100, 1)}))

    # --- anomalies against the UPS's own week ---
    named = {f.code for f in findings}
    last_hour = [r for r in ok if r.read_at >= now - timedelta(hours=1)]
    base = [r for r in week if r.read_at < now - timedelta(hours=1)]
    labels = {"output_voltage": ("Output voltage", "V"), "battery_voltage": ("Battery voltage", "V"),
              "temperature_c": ("Temperature", "°C"), "load_percent": ("Load", "%"),
              "input_voltage": ("Input voltage", "V")}
    covered = {"temperature_c": {"too_hot", "warm"}, "load_percent": {"overload", "load_growing"},
               "input_voltage": {"unstable_mains", "on_battery"}, "battery_voltage": {"battery_voltage_falling"}}
    for name, (label, unit) in labels.items():
        if covered.get(name, set()) & named:
            continue
        now_v = _median([getattr(r, name) for r in last_hour])
        z = _mad_z(now_v, [getattr(r, name) for r in base])
        if z is not None and abs(z) >= 6:
            usual = _median([getattr(r, name) for r in base])
            findings.append(Finding(
                f"unusual_{name}", "watch", f"Unusual {label.lower()}",
                f"{label} is {now_v:.1f} {unit} over the last hour against a usual {usual:.1f} {unit} this week.",
                "Look at the UPS and what is connected to it.", {"now": _round(now_v), "usual": _round(usual)}))

    findings.sort(key=lambda f: -LEVEL_ORDER[f.level])
    level = max((f.level for f in findings), key=lambda lv: LEVEL_ORDER[lv], default="ok")
    score = min(100, sum(WEIGHT[f.level] for f in findings if f.code != "map_unverified"))
    return Assessment(level=level, risk_score=score, findings=findings, metrics=metrics)


def _daily(readings, name, now, days):
    """[(day index, median value)] for the last ``days`` days."""
    buckets: dict[int, list] = {}
    for r in readings:
        age = (now - r.read_at).total_seconds() / 86400
        value = getattr(r, name)
        if age <= days and value is not None:
            buckets.setdefault(int(days - age), []).append(value)
    return [(d, statistics.median(v)) for d, v in sorted(buckets.items()) if len(v) >= 6]
