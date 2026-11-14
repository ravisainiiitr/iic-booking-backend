"""Phase 4: S3 lifecycle rules for RAA large data (applied only by the apply-s3-lifecycle.yml ops workflow).

Rules (merged into the bucket's existing lifecycle configuration; other rules are kept unchanged):
- Intelligent-Tiering for raw data (``Results/``) and processed data (My Research prefix), from day 0;
- optional transition to an archive class after N days (left empty until the user decides);
- abort incomplete multipart uploads after 7 days (whole bucket);
- expire ``speedtest/`` objects after 1 day.

No rule deletes research data: the only Expiration is on ``speedtest/`` and :func:`check_rules` enforces that.
"""

from __future__ import annotations

import copy
from typing import Any

from django.conf import settings

RULE_PREFIX = "raa-large-data-"
SPEEDTEST_PREFIX = "speedtest/"
RAW_PREFIX = "Results/"
ABORT_DAYS = 7
ARCHIVE_CLASSES = {"GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"}


def processed_prefix() -> str:
    return (getattr(settings, "MY_RESEARCH_S3_PREFIX", "research") or "research").strip("/") + "/"


def buckets() -> list[str]:
    from iic_booking.my_research import storage as research_storage
    from iic_booking.remote_analysis.large_data import s3

    found = []
    for name in (s3.results_bucket(), research_storage.bucket_name()):
        if name and name not in found:
            found.append(name)
    return found


def desired_rules(*, archive_after_days: int | None = None, archive_class: str = "GLACIER_IR") -> list[dict[str, Any]]:
    if archive_after_days is not None and archive_after_days < 1:
        raise ValueError("archive_after_days must be at least 1")
    if archive_class not in ARCHIVE_CLASSES:
        raise ValueError(f"archive_class must be one of {sorted(ARCHIVE_CLASSES)}")

    def tiering(rule_id: str, prefix: str) -> dict[str, Any]:
        transitions = [{"Days": 0, "StorageClass": "INTELLIGENT_TIERING"}]
        if archive_after_days:
            transitions.append({"Days": int(archive_after_days), "StorageClass": archive_class})
        return {"ID": rule_id, "Status": "Enabled", "Filter": {"Prefix": prefix}, "Transitions": transitions}

    return [
        tiering(f"{RULE_PREFIX}raw-tiering", RAW_PREFIX),
        tiering(f"{RULE_PREFIX}processed-tiering", processed_prefix()),
        {
            "ID": f"{RULE_PREFIX}abort-incomplete-multipart",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": ABORT_DAYS},
        },
        {
            "ID": f"{RULE_PREFIX}speedtest-expire",
            "Status": "Enabled",
            "Filter": {"Prefix": SPEEDTEST_PREFIX},
            "Expiration": {"Days": 1},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
        },
    ]


def check_rules(rules: list[dict[str, Any]]) -> None:
    """Refuse any rule of ours that could delete data outside ``speedtest/``."""
    for rule in rules:
        if not str(rule.get("ID", "")).startswith(RULE_PREFIX):
            continue
        prefix = str((rule.get("Filter") or {}).get("Prefix", ""))
        deletes = "Expiration" in rule or "NoncurrentVersionExpiration" in rule
        if deletes and prefix != SPEEDTEST_PREFIX:
            raise ValueError(f"Rule {rule['ID']} would delete objects under {prefix!r}")
        if "speedtest" in rule["ID"] and prefix != SPEEDTEST_PREFIX:
            raise ValueError("The speed test rule must be limited to speedtest/")


def merge(existing: list[dict[str, Any]], desired: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Existing rules except ours (replaced), then ours."""
    kept = [copy.deepcopy(r) for r in existing if not str(r.get("ID", "")).startswith(RULE_PREFIX)]
    return kept + [copy.deepcopy(r) for r in desired]


def overlaps(existing: list[dict[str, Any]]) -> list[str]:
    """Notes about other rules that also act on our prefixes (shown, never changed)."""
    notes = []
    ours = (RAW_PREFIX, processed_prefix(), SPEEDTEST_PREFIX)
    for rule in existing:
        rid = str(rule.get("ID", ""))
        if rid.startswith(RULE_PREFIX):
            continue
        prefix = str((rule.get("Filter") or {}).get("Prefix", (rule.get("Prefix") or "")))
        if any(p.startswith(prefix) or prefix.startswith(p) for p in ours):
            kinds = [k for k in ("Expiration", "Transitions", "AbortIncompleteMultipartUpload") if k in rule]
            notes.append(f"existing rule {rid or '(no id)'} on {prefix!r}: {', '.join(kinds) or 'other'} (kept as is)")
    return notes


def read_existing(client, bucket: str) -> list[dict[str, Any]]:
    from botocore.exceptions import ClientError

    try:
        return client.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", []) or []
    except ClientError as exc:
        if (exc.response.get("Error") or {}).get("Code") == "NoSuchLifecycleConfiguration":
            return []
        raise
