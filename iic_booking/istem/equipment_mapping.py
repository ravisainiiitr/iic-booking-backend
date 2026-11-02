"""I-STEM equipment mapping page (2 Nov 2026): which portal instrument is which I-STEM ``equipment_id``.

Main Admin manages every instrument; a department administrator (or department staff with ``equipment.manage``)
manages the instruments of their own department, so each department joining the portal maps its own equipment.
I-STEM is only read here (Equipment List); saving a mapping never sends anything to I-STEM's booking APIs.
"""

from __future__ import annotations

import logging
import re

from django.db import transaction
from django.utils import timezone

from . import conf
from .choices import SyncState
from .errors import IstemError
from .models import IstemBookingLink, IstemEquipmentMapping

logger = logging.getLogger(__name__)

ISTEM_ID = re.compile(r"^[0-9]{1,12}$")
SEARCH_MIN_CHARS = 3
SEARCH_LIMIT = 50
# Links in these states are finished (or never started): the instrument's I-STEM ID may change under them.
CLOSED_STATES = frozenset({
    SyncState.NOT_ELIGIBLE, SyncState.EXECUTED, SyncState.REMOTE_CANCELLED, SyncState.REMOTE_REJECTED,
    SyncState.ABANDONED,
})


class MappingRefused(Exception):
    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


def _is_main_admin(user) -> bool:
    from iic_booking.users.models import UserType

    return bool(user and user.is_authenticated and getattr(user, "user_type", None) == UserType.ADMIN)


def managed_department_id(user) -> int | None:
    """The department whose instruments ``user`` may map (None for Main Admin, who maps every instrument)."""
    from iic_booking.users.rbac import get_user_department_scope_id, user_has_permission

    if _is_main_admin(user):
        return None
    department_id = get_user_department_scope_id(user)
    if department_id is None or not user_has_permission(user, "equipment.manage", department_id):
        raise MappingRefused("You do not manage equipment in a department.", 403)
    return department_id


def managed_equipment(user):
    from iic_booking.equipment.models import Equipment

    department_id = managed_department_id(user)
    qs = Equipment.objects.select_related("internal_department")
    return qs if department_id is None else qs.filter(internal_department_id=department_id)


def _equipment_for(user, equipment_id: int):
    equipment = managed_equipment(user).filter(pk=equipment_id).first()
    if equipment is None:
        raise MappingRefused("Equipment not found among the instruments you manage.", 404)
    return equipment


def _open_links(equipment_id: int):
    return IstemBookingLink.objects.filter(booking__equipment_id=equipment_id).exclude(sync_state__in=CLOSED_STATES)


def _mapping_row(mapping: IstemEquipmentMapping | None) -> dict | None:
    if mapping is None:
        return None
    return {
        "istem_equipment_id": mapping.istem_equipment_id,
        "sample_count_input_key": mapping.sample_count_input_key,
        "is_active": mapping.is_active,
        "notes": mapping.notes,
        "set_by": (mapping.verified_by.get_full_name() or mapping.verified_by.email) if mapping.verified_by else "",
        "set_at": mapping.verified_at,
        "remote_found": mapping.remote_found,
        "remote_equipment_name": mapping.remote_equipment_name,
        "remote_checked_at": mapping.remote_checked_at,
    }


def list_rows(user) -> dict:
    """Every instrument the user manages, with its I-STEM mapping and whether its bookings need an I-STEM FBR."""
    from iic_booking.equipment.models import ChargeProfile, DynamicInputField

    equipment = list(managed_equipment(user).order_by("name", "pk"))
    ids = [e.pk for e in equipment]
    mappings = {m.equipment_id: m for m in IstemEquipmentMapping.objects.filter(equipment_id__in=ids).select_related("verified_by")}
    needs_fbr = set(ChargeProfile.objects.filter(equipment_id__in=ids, is_active=True, require_istem_fbr=True)
                    .values_list("equipment_id", flat=True))
    fields: dict[int, dict[str, str]] = {}
    for equipment_id, key, label in (DynamicInputField.objects.filter(equipment_id__in=ids)
                                     .order_by("field_key").values_list("equipment_id", "field_key", "field_label")):
        fields.setdefault(equipment_id, {}).setdefault(key, label or key)
    open_counts: dict[int, int] = {}
    for equipment_id in (IstemBookingLink.objects.filter(booking__equipment_id__in=ids)
                         .exclude(sync_state__in=CLOSED_STATES).values_list("booking__equipment_id", flat=True)):
        open_counts[equipment_id] = open_counts.get(equipment_id, 0) + 1
    rows = []
    for e in equipment:
        department = e.internal_department
        rows.append({
            "equipment_id": e.pk,
            "code": e.code,
            "name": e.name,
            "department": {"id": department.pk, "name": department.name} if department else None,
            "requires_istem_fbr": e.pk in needs_fbr,
            "input_fields": [{"key": k, "label": v} for k, v in sorted(fields.get(e.pk, {}).items())],
            "open_istem_bookings": open_counts.get(e.pk, 0),
            "mapping": _mapping_row(mappings.get(e.pk)),
        })
    return {"istem_connected": conf.network_allowed(), "all_departments": managed_department_id(user) is None,
            "equipment": rows}


def search_istem_equipment(user, query: str, client_factory=None) -> list[dict]:
    """Active I-STEM equipment of the portal's institute whose name contains ``query`` (read-only Equipment List)."""
    from .service import default_client

    managed_department_id(user)  # permission check
    query = " ".join(str(query or "").split())
    if len(query) < SEARCH_MIN_CHARS:
        raise MappingRefused(f"Type at least {SEARCH_MIN_CHARS} letters of the I-STEM equipment name.")
    if not conf.network_allowed():
        raise MappingRefused("I-STEM is not connected on this portal.", 503)
    client = (client_factory or default_client)()
    try:
        institute_id = client.session_info().institute_id
        page = client.list_equipment(institute_id=institute_id, equipment_name=query, limit=SEARCH_LIMIT)
    except IstemError as exc:
        raise MappingRefused(f"I-STEM did not answer the equipment search ({exc.category}). Try again shortly.", 502) from exc
    used = {}
    for equipment_id, istem_id, name in (IstemEquipmentMapping.objects.filter(
            istem_equipment_id__in=[r["equipment_id"] for r in page.rows], is_active=True)
            .values_list("equipment_id", "istem_equipment_id", "equipment__name")):
        used.setdefault(istem_id, []).append({"equipment_id": equipment_id, "name": name})
    return [{**row, "mapped_to": used.get(row["equipment_id"], [])} for row in page.rows]


def save_mapping(user, equipment_id: int, data: dict, *, client_factory=None) -> dict:
    """Create or change an instrument's I-STEM mapping, then check it against I-STEM's Equipment List when connected.
    The I-STEM ID cannot change, and the mapping cannot be switched off, while the instrument has bookings open on
    I-STEM (their Confirm Booking / FBR Execute must use the ID they were created with)."""
    equipment = _equipment_for(user, equipment_id)
    istem_id = str(data.get("istem_equipment_id") or "").strip()
    if not ISTEM_ID.match(istem_id):
        raise MappingRefused("The I-STEM equipment ID is a number (pick it from the I-STEM search).")
    key = str(data.get("sample_count_input_key") or "").strip().upper()
    allowed_keys = set(equipment.input_fields.values_list("field_key", flat=True))
    if key and key not in allowed_keys:
        raise MappingRefused(f"Input field {key} does not exist on {equipment.name}.")
    is_active = data.get("is_active", True) is not False
    notes = str(data.get("notes") or "").strip()[:2000]
    with transaction.atomic():
        mapping = IstemEquipmentMapping.objects.select_for_update().filter(equipment=equipment).first()
        changes_id = mapping is not None and mapping.istem_equipment_id != istem_id
        switches_off = mapping is not None and mapping.is_active and not is_active
        if (changes_id or switches_off) and _open_links(equipment.pk).exists():
            raise MappingRefused(
                f"{equipment.name} has bookings still open on I-STEM under ID {mapping.istem_equipment_id}; the ID can "
                "be changed (or the mapping switched off) once they are executed or cancelled.", 409)
        if mapping is None:
            mapping = IstemEquipmentMapping(equipment=equipment)
        previous = mapping.istem_equipment_id
        mapping.istem_equipment_id = istem_id
        mapping.sample_count_input_key = key
        mapping.is_active = is_active
        mapping.notes = notes
        mapping.verified_by = user
        mapping.verified_at = timezone.now()
        if changes_id or mapping.pk is None:
            mapping.remote_checked_at, mapping.remote_found, mapping.remote_equipment_name = None, None, ""
        mapping.save()
    logger.info("istem equipment mapping: equipment=%s %s -> %s active=%s by user=%s",
                equipment.pk, previous or "-", istem_id, is_active, user.pk)
    warnings = []
    shared = (IstemEquipmentMapping.objects.filter(istem_equipment_id=istem_id, is_active=True)
              .exclude(equipment=equipment).values_list("equipment__name", flat=True))
    if shared:
        warnings.append("The same I-STEM ID is also mapped to: " + ", ".join(sorted(shared)) + ".")
    if not key:
        warnings.append("No sample-count field chosen: I-STEM bookings for this instrument will wait until one is set.")
    check = verify(user, equipment_ids=[equipment.pk], client_factory=client_factory) if conf.network_allowed() else None
    mapping.refresh_from_db()
    if mapping.remote_found is False:
        warnings.append("I-STEM's equipment list for the institute does not show this ID as active.")
    return {"mapping": _mapping_row(mapping), "warnings": warnings, "checked": check is not None}


def verify(user, *, equipment_ids=None, client_factory=None) -> dict:
    """Check the mappings of the instruments the user manages against I-STEM's Equipment List."""
    from .service import default_client, verify_equipment_mappings

    managed = set(managed_equipment(user).values_list("pk", flat=True))
    ids = managed if equipment_ids is None else managed & {int(i) for i in equipment_ids}
    if not conf.network_allowed():
        raise MappingRefused("I-STEM is not connected on this portal.", 503)
    try:
        return verify_equipment_mappings(client_factory or default_client, equipment_ids=sorted(ids))
    except IstemError as exc:
        raise MappingRefused(f"I-STEM's equipment list could not be read ({exc.category}). Try again shortly.", 502) from exc
