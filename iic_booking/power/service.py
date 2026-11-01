"""Readings from the sync agent, history thinning and UPS alerts.

Alert rules (one open ``sync.AlertEvent`` per UPS and rule, found by fingerprint; resolved when the condition clears):
- ``ups_unreachable``  the agent could not read the UPS (network card off, cable, wrong address)      — warning
- ``ups_on_battery``   mains power lost, the UPS is running on battery                                 — critical
- ``ups_low_battery``  battery below the UPS's own low-battery flag or the configured percentage       — critical
- ``ups_overload``     overload flag or load above the configured percentage                           — warning
- ``ups_fault``        UPS fault, or running on bypass (no protection)                                 — critical
- ``ups_replace_battery`` the UPS asks for a battery replacement                                       — warning
- ``ups_silent``       no reading from the agent for 5 minutes (agent or network down)                 — warning
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import UpsDevice, UpsReading
from .register_maps import FIELDS

HISTORY_EVERY = timedelta(minutes=5)
KEEP_HISTORY = timedelta(days=30)
SILENT_AFTER = timedelta(minutes=5)
NUMERIC = ("input_voltage", "output_voltage", "output_frequency", "load_percent", "battery_percent",
           "runtime_minutes", "battery_voltage", "temperature_c")
FLAGS = ("on_battery", "on_bypass", "low_battery", "overload", "fault", "replace_battery")


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1e9 else None  # drop NaN / absurd values


def _flag(value):
    return None if value is None else bool(value)


def conditions(device: UpsDevice, r: UpsReading) -> dict[str, tuple[str, str, str]]:
    """Active alert rules for a reading: {rule: (severity, title, message)}."""
    name = device.name
    out: dict[str, tuple[str, str, str]] = {}
    if not r.reachable:
        out["ups_unreachable"] = ("warning", f"{name}: not reachable", r.error or "The sync agent could not read this UPS.")
        return out
    if r.on_battery:
        out["ups_on_battery"] = ("critical", f"{name}: on battery", "Mains power is off; the UPS is running on battery"
                                 + (f" ({r.runtime_minutes:.0f} min left)." if r.runtime_minutes is not None else "."))
    low = r.low_battery or (r.battery_percent is not None and r.battery_percent < device.low_battery_percent)
    if low:
        out["ups_low_battery"] = ("critical", f"{name}: battery low",
                                  f"Battery at {r.battery_percent:.0f}%." if r.battery_percent is not None else "Low battery.")
    if r.overload or (r.load_percent is not None and r.load_percent > device.high_load_percent):
        out["ups_overload"] = ("warning", f"{name}: load high",
                               f"Load {r.load_percent:.0f}%." if r.load_percent is not None else "Overload reported.")
    if r.fault or r.on_bypass:
        out["ups_fault"] = ("critical", f"{name}: {'on bypass' if r.on_bypass and not r.fault else 'fault'}",
                            "The connected instruments are not protected.")
    if r.replace_battery:
        out["ups_replace_battery"] = ("warning", f"{name}: replace battery", "The UPS asks for a battery replacement.")
    return out


def _fingerprint(device: UpsDevice, rule: str) -> str:
    return hashlib.sha256(f"ups:{device.pk}:{rule}".encode()).hexdigest()[:64]


def sync_alerts(device: UpsDevice, active: dict[str, tuple[str, str, str]], *, rules=None) -> None:
    """Open alerts for active rules (once), resolve open alerts whose condition cleared."""
    from iic_booking.sync.models import AlertEvent, AlertLifecycleStatus, AlertSeverity

    open_statuses = (AlertLifecycleStatus.NEW, AlertLifecycleStatus.ACKNOWLEDGED, AlertLifecycleStatus.SUPPRESSED)
    severities = {"warning": AlertSeverity.WARNING, "critical": AlertSeverity.CRITICAL}
    managed = rules if rules is not None else ("ups_unreachable", "ups_on_battery", "ups_low_battery", "ups_overload",
                                               "ups_fault", "ups_replace_battery")
    now = timezone.now()
    for rule in managed:
        fp = _fingerprint(device, rule)
        existing = AlertEvent.objects.filter(fingerprint=fp, status__in=open_statuses).first()
        if rule in active and existing is None:
            severity, title, message = active[rule]
            AlertEvent.objects.create(
                rule_code=rule, category="ups", severity=severities[severity], title=title[:200], message=message[:1000],
                fingerprint=fp, sync_agent=device.sync_agent, department=device.department,
                equipment=device.equipment.first(),
                details={"ups_id": device.pk, "host": device.host, "model": device.model},
            )
        elif rule not in active and existing is not None:
            existing.status = AlertLifecycleStatus.RESOLVED
            existing.resolved_at = now
            existing.resolved_by = "UPS monitor"
            existing.resolution = "Condition cleared"
            existing.save(update_fields=["status", "resolved_at", "resolved_by", "resolution", "updated_at"])


def record(agent, items: list[dict[str, Any]]) -> dict[str, int]:
    """Store readings the agent sent for its own UPS units; unknown or foreign devices are ignored."""
    stored = ignored = 0
    devices = {d.pk: d for d in UpsDevice.objects.filter(sync_agent=agent, enabled=True)}
    for item in items[:100]:
        device = devices.get(item.get("device_id")) if isinstance(item, dict) else None
        if device is None:
            ignored += 1
            continue
        read_at = parse_datetime(str(item.get("read_at") or "")) or timezone.now()
        if timezone.is_naive(read_at):
            read_at = timezone.make_aware(read_at)
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        reading = UpsReading(
            device=device, read_at=min(read_at, timezone.now()), reachable=bool(item.get("reachable", True)),
            error=str(item.get("error") or "")[:200],
            raw={k: v for k, v in values.items() if k not in FIELDS and isinstance(v, (int, float, bool))},
        )
        for f in NUMERIC:
            setattr(reading, f, _num(values.get(f)))
        for f in FLAGS:
            setattr(reading, f, _flag(values.get(f)))
        with transaction.atomic():
            # Keep the newest reading, and older ones only every 5 minutes (history): the previous newest goes
            # unless it is at least 5 minutes after the history point before it.
            last = UpsReading.objects.filter(device=device, read_at__lte=reading.read_at).order_by("-read_at").first()
            if last is not None and not _is_history(last):
                last.delete()
            reading.save()
        sync_alerts(device, conditions(device, reading))
        stored += 1
    UpsReading.objects.filter(read_at__lt=timezone.now() - KEEP_HISTORY).delete()
    return {"stored": stored, "ignored": ignored}


def _is_history(reading: UpsReading) -> bool:
    """A reading that was kept as a 5-minute history point (its predecessor is at least 5 minutes older)."""
    prev = UpsReading.objects.filter(device=reading.device, read_at__lt=reading.read_at).order_by("-read_at").first()
    return prev is None or reading.read_at - prev.read_at >= HISTORY_EVERY


def check_silent(now=None) -> int:
    """UPS units whose agent sent nothing for 5 minutes get a 'silent' alert (resolved by the next reading)."""
    now = now or timezone.now()
    count = 0
    for device in UpsDevice.objects.filter(enabled=True).select_related("sync_agent"):
        last = UpsReading.objects.filter(device=device).order_by("-read_at").values_list("read_at", flat=True).first()
        silent = last is None or last < now - SILENT_AFTER
        active = {"ups_silent": ("warning", f"{device.name}: no data",
                                 "No reading from the sync agent for 5 minutes.")} if silent else {}
        sync_alerts(device, active, rules=("ups_silent",))
        count += silent
    return count


def latest(device: UpsDevice) -> UpsReading | None:
    return device.readings.order_by("-read_at").first()
