"""Semver helpers for installer / portal compatibility (Phase R.2.6)."""

from __future__ import annotations

import re
from typing import Optional


_SPLIT = re.compile(r"[.\-+_]+")


def parse_version(value: str) -> tuple[int, ...]:
    """Parse a loose version string into comparable ints (non-numeric parts ignored)."""
    text = (value or "").strip().lstrip("vV")
    if not text:
        return (0,)
    parts: list[int] = []
    for token in _SPLIT.split(text):
        if not token:
            continue
        digits = re.match(r"^(\d+)", token)
        if digits:
            parts.append(int(digits.group(1)))
        elif parts:
            # Trailing label (rc, r2) — stop numeric core.
            break
    return tuple(parts) if parts else (0,)


def version_gte(actual: str, minimum: str) -> bool:
    return parse_version(actual) >= parse_version(minimum)


def version_lte(actual: str, maximum: str) -> bool:
    return parse_version(actual) <= parse_version(maximum)


def compare_installer(
    product: str,
    installer_version: str,
    supported: dict,
) -> dict:
    """
    Return compatibility status for an installer product key.
    status: compatible | upgrade_recommended | unsupported | unknown_product
    """
    key = (product or "").strip().lower()
    row = supported.get(key) if isinstance(supported, dict) else None
    if not isinstance(row, dict):
        return {
            "product": key,
            "installer_version": installer_version,
            "status": "unknown_product",
            "compatible": False,
            "message": f"Unknown installer product '{key}'.",
        }

    minimum = str(row.get("minimum") or "0")
    latest = str(row.get("latest") or minimum)
    actual = (installer_version or "").strip()

    if not actual:
        return {
            "product": key,
            "installer_version": actual,
            "minimum": minimum,
            "latest": latest,
            "status": "unsupported",
            "compatible": False,
            "message": "Installer version is required.",
        }

    if not version_gte(actual, minimum):
        return {
            "product": key,
            "installer_version": actual,
            "minimum": minimum,
            "latest": latest,
            "status": "unsupported",
            "compatible": False,
            "message": (
                f"This installer ({actual}) is too old for this portal. "
                f"Minimum supported: {minimum}. Download the latest installer ({latest})."
            ),
            "download_hint": latest,
        }

    if parse_version(actual) < parse_version(latest):
        return {
            "product": key,
            "installer_version": actual,
            "minimum": minimum,
            "latest": latest,
            "status": "upgrade_recommended",
            "compatible": True,
            "message": (
                f"Installer {actual} works, but {latest} is recommended. "
                "Upgrade when convenient."
            ),
            "download_hint": latest,
        }

    return {
        "product": key,
        "installer_version": actual,
        "minimum": minimum,
        "latest": latest,
        "status": "compatible",
        "compatible": True,
        "message": "Installer is compatible with this portal.",
    }


def traffic_light(status: str) -> str:
    return {
        "compatible": "green",
        "upgrade_recommended": "yellow",
        "unsupported": "red",
        "unknown_product": "red",
    }.get(status, "red")
