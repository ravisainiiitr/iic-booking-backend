"""Persist Channel-I identity facts without rewriting portal roles or historical records."""

from __future__ import annotations

from django.utils import timezone

from iic_booking.users.identity.dates import add_calendar_years
from iic_booking.users.identity.extract import extract_channel_i_academic_facts, facts_as_history_values, normalize_label
from iic_booking.users.models.channel_i_identity import (
    ChannelIDepartmentMapping,
    ChannelIIdentityHistory,
    ChannelIIdentityProfile,
    StudentValiditySource,
)


TRACKED_FIELDS = (
    "channel_i_user_id",
    "channel_i_username",
    "student_degree_name",
    "student_department_name",
    "student_start_date",
    "student_end_date",
    "faculty_department_name",
    "faculty_designation",
)


def _str(value) -> str:
    if value is None:
        return ""
    return str(value)


def sync_channel_i_identity(user, user_info: dict) -> ChannelIIdentityProfile:
    facts = extract_channel_i_academic_facts(user_info)
    profile, _created = ChannelIIdentityProfile.objects.get_or_create(user=user)
    changed = False
    now = timezone.now()
    for field in TRACKED_FIELDS:
        old = getattr(profile, field)
        new = facts.get(field)
        if _str(old) != _str(new):
            ChannelIIdentityHistory.objects.create(
                profile=profile,
                field_name=field,
                previous_value=_str(old),
                new_value=_str(new),
            )
            setattr(profile, field, new)
            changed = True
    profile.has_student_payload = bool(facts.get("has_student_payload"))
    profile.has_faculty_payload = bool(facts.get("has_faculty_payload"))
    profile.raw_student_keys = facts.get("raw_student_keys") or []
    profile.last_channel_i_sync = now
    if changed:
        profile.profile_last_changed_at = now

    # Validity: Channel-I end_date is authoritative. Local derived/extension never overrides it.
    if profile.student_end_date:
        profile.validity_source = StudentValiditySource.CHANNEL_I_END_DATE
        # Keep derived_end_date as historical local calculation; do not copy Channel-I into it.
    elif profile.student_start_date:
        five = add_calendar_years(profile.student_start_date, 5)
        if profile.derived_end_date and profile.derived_end_date > five:
            profile.validity_source = StudentValiditySource.ADMIN_EXTENSION
        else:
            if not profile.derived_end_date:
                profile.derived_end_date = five
            profile.validity_source = StudentValiditySource.START_DATE_PLUS_5_YEARS
    else:
        if profile.has_student_payload:
            profile.validity_source = StudentValiditySource.UNRESOLVED

    profile.save()
    _ensure_unmapped_department_row(profile.student_department_name)
    return profile


def _ensure_unmapped_department_row(channel_i_name: str) -> None:
    """Record unseen Channel-I department names as UNMAPPED. Does not create internal departments."""
    key = normalize_label(channel_i_name)
    if not key:
        return
    ChannelIDepartmentMapping.objects.get_or_create(
        channel_i_department_name_normalized=key,
        defaults={
            "channel_i_department_name": channel_i_name.strip(),
            "internal_department": None,
            "active": True,
        },
    )
