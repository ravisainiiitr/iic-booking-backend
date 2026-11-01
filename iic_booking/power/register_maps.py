"""Modbus register maps the sync agent uses to read a UPS.

A point: ``{"name", "table": "holding"|"input"|"coil"|"discrete", "address" (0-based), "type": "uint16"|"int16"|
"uint32"|"int32"|"float32"|"bool"|"bit", "bit" (for "bit"), "scale" (multiplier), "word_order": "big"|"little"}``.
Known point names map onto ``UpsReading`` fields (see ``FIELDS``); others are kept in ``raw``.

IMPORTANT — the Vertiv template lists the points to read, but its register addresses are NOT verified: Vertiv's
Unity / IntelliSlot cards publish a model- and firmware-specific Modbus point list (card web page / Vertiv Unity
"Modbus reference" for the UPS model). Copy the addresses from that list into a custom map (or this template) and
tick "map verified" on the UPS; until then the portal shows the readings as unverified.
"""

from __future__ import annotations

FIELDS = {
    "input_voltage", "output_voltage", "output_frequency", "load_percent", "battery_percent", "runtime_minutes",
    "battery_voltage", "temperature_c", "on_battery", "on_bypass", "low_battery", "overload", "fault",
    "replace_battery",
}

# Points to read from a Vertiv UPS through a Unity card. Addresses are placeholders to be replaced from the card's
# published Modbus point list (see the module docstring).
VERTIV_UNITY_TEMPLATE = [
    {"name": "input_voltage", "table": "input", "address": 0, "type": "uint16", "scale": 0.1, "unit": "V"},
    {"name": "output_voltage", "table": "input", "address": 1, "type": "uint16", "scale": 0.1, "unit": "V"},
    {"name": "output_frequency", "table": "input", "address": 2, "type": "uint16", "scale": 0.1, "unit": "Hz"},
    {"name": "load_percent", "table": "input", "address": 3, "type": "uint16", "scale": 1, "unit": "%"},
    {"name": "battery_percent", "table": "input", "address": 4, "type": "uint16", "scale": 1, "unit": "%"},
    {"name": "runtime_minutes", "table": "input", "address": 5, "type": "uint16", "scale": 1, "unit": "min"},
    {"name": "battery_voltage", "table": "input", "address": 6, "type": "uint16", "scale": 0.1, "unit": "V"},
    {"name": "temperature_c", "table": "input", "address": 7, "type": "int16", "scale": 0.1, "unit": "°C"},
    {"name": "on_battery", "table": "discrete", "address": 0, "type": "bool"},
    {"name": "on_bypass", "table": "discrete", "address": 1, "type": "bool"},
    {"name": "low_battery", "table": "discrete", "address": 2, "type": "bool"},
    {"name": "overload", "table": "discrete", "address": 3, "type": "bool"},
    {"name": "fault", "table": "discrete", "address": 4, "type": "bool"},
    {"name": "replace_battery", "table": "discrete", "address": 5, "type": "bool"},
]

MAPS = {"vertiv_unity_template": VERTIV_UNITY_TEMPLATE}
TABLES = {"holding", "input", "coil", "discrete"}
TYPES = {"uint16", "int16", "uint32", "int32", "float32", "bool", "bit"}


def points_for(device) -> list[dict]:
    if device.register_map == "custom":
        return list(device.custom_map or [])
    return list(MAPS.get(device.register_map, []))


def validate_points(points) -> list[str]:
    """Problems with a custom map, or [] when it is usable."""
    problems = []
    if not isinstance(points, list) or not points:
        return ["The map must be a non-empty list of points."]
    for i, p in enumerate(points):
        if not isinstance(p, dict) or not p.get("name"):
            problems.append(f"Point {i + 1}: a name is required.")
            continue
        if p.get("table") not in TABLES:
            problems.append(f"{p['name']}: table must be one of {sorted(TABLES)}.")
        if p.get("type") not in TYPES:
            problems.append(f"{p['name']}: type must be one of {sorted(TYPES)}.")
        if not isinstance(p.get("address"), int) or p["address"] < 0 or p["address"] > 65535:
            problems.append(f"{p['name']}: address must be 0-65535.")
        if p.get("type") == "bit" and not (isinstance(p.get("bit"), int) and 0 <= p["bit"] <= 15):
            problems.append(f"{p['name']}: bit must be 0-15.")
    return problems
