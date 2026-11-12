"""Creating an instrument on I-STEM (Equipment Create, 8 Nov 2026), for portal instruments I-STEM does not list yet.

The Main Admin opens an unmapped instrument, completes the details I-STEM needs (pre-filled from the instrument record,
choices from I-STEM's own lists), and sends Equipment Create once. I-STEM's id for the new instrument is stored on the
details record and saved as the instrument's (verified) I-STEM mapping, so its bookings sync from then on.

Guards: Main Admin only; never for an instrument that already has a mapping or was already created; refused when
I-STEM already lists an instrument of the institute with the same serial number (map to it instead); a create whose
answer carries no id is looked up in I-STEM's list by serial number before anything else is tried.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.db import transaction
from django.utils import timezone

from . import contract
from .choices import ErrorCategory
from .equipment_mapping import (
    MappingRefused,
    _default_sample_key,
    _forget_listings,
    _is_main_admin,
    _token_institute,
    istem_problem,
)
from .errors import IstemError
from .models import IstemDepartmentMapping, IstemEquipmentDetails, IstemEquipmentMapping, IstemReferenceValue

logger = logging.getLogger(__name__)

REFERENCE_LISTS = (
    (IstemReferenceValue.KIND_FUNDING_AGENCY, contract.FUNDING_AGENCIES_PATH, "funding_agencies"),
    (IstemReferenceValue.KIND_FUNDING_AGENCY_TYPE, contract.FUNDING_AGENCY_TYPES_PATH, "funding_agency_types"),
    (IstemReferenceValue.KIND_EQUIPMENT_CATEGORY, contract.EQUIPMENT_CATEGORIES_PATH, "equipment_categories"),
    (IstemReferenceValue.KIND_FACILITY, contract.FACILITIES_PATH, "facilities"),
)

# Details field -> (I-STEM form field, label shown to the admin, required).
FIELDS = (
    ("equipment_name", "equipment_name", "Equipment name", True),
    ("equipment_make", "equipment_make", "Make (manufacturer)", True),
    ("equipment_srno", "equipment_srno", "Serial number", True),
    ("istem_department_id", "equipment_department_id", "I-STEM department", True),
    ("funding_agency_id", "equipment_agency_id", "Funding agency", True),
    ("funding_agency_type", "funding_agency_type", "Funding agency type", True),
    # I-STEM refuses Equipment Create without these three (not in its Swagger; its answer names them).
    ("alert_mailid", "alert_mailid", "Alert email (I-STEM sends this instrument's alerts here)", True),
    ("pre_experiment_support", "pre_experiment_support", "Pre-experiment support", True),
    ("post_experiment_support", "post_experiment_support", "Post-experiment support", True),
    ("equipment_model", "equipment_model", "Model", False),
    ("equipment_abbr", "equipment_abbr", "Short name", False),
    ("equipment_location", "equipment_location", "Location", False),
    ("equipment_website", "equipment_website", "Website", False),
    ("equipment_description", "equipment_description", "Description", False),
    ("equipment_project_id", "equipment_project_id", "Project ID", False),
    ("equipment_rate", "equipment_rate", "Rate", False),
    ("equipment_fov_rate", "equipment_fov_rate", "FOV rate", False),
    ("equipment_cif_rate", "equipment_cif_rate", "CIF rate", False),
    ("equipment_other_rate", "equipment_other_rate", "Other rate", False),
    ("category", "category", "Category", False),
    ("facility_id", "equipment_facility_id", "Facility", False),
    ("supplier_name", "supplier_name", "Supplier name", False),
    ("supplier_email_id", "supplier_email_id", "Supplier email", False),
    ("service_provider_name", "service_provider_name", "Service provider name", False),
    ("service_provider_email_id", "service_provider_email_id", "Service provider email", False),
    ("last_amc_done", "last_amc_done", "Last AMC done (YYYY-MM-DD)", False),
    ("pooling_status", "pooling_status", "Pooling status", False),
)
EDITABLE = tuple(f[0] for f in FIELDS)
RATE_FIELDS = ("equipment_rate", "equipment_fov_rate", "equipment_cif_rate", "equipment_other_rate")
EMAIL_FIELDS = ("supplier_email_id", "service_provider_email_id", "alert_mailid")
YES_NO_FIELDS = ("pre_experiment_support", "post_experiment_support")
YES_NO = ("Yes", "No")
NUMERIC_ID = re.compile(r"^[0-9]{1,12}$")


def _require_main_admin(user) -> None:
    if not _is_main_admin(user):
        raise MappingRefused("Only the Main Administrator creates instruments on I-STEM.", 403)


def _client(client_factory=None):
    from .service import default_client

    return (client_factory or default_client)()


# --- I-STEM reference lists ------------------------------------------------------------------------------------------

def sync_reference_lists(user, *, client_factory=None) -> dict:
    """Read I-STEM's funding agencies, funding agency types, equipment categories and facilities and store them.
    A list I-STEM cannot give keeps its last stored values (reported in ``failed``)."""
    _require_main_admin(user)
    try:
        client = _client(client_factory)
    except IstemError as exc:
        raise MappingRefused(istem_problem(exc), 503) from None
    counts, failed = {}, {}
    now = timezone.now()
    for kind, path, op in REFERENCE_LISTS:
        try:
            rows = client.list_reference(path, op)
        except IstemError as exc:
            failed[kind] = istem_problem(exc)
            continue
        with transaction.atomic():
            seen = []
            for row in rows:
                obj, _ = IstemReferenceValue.objects.update_or_create(
                    kind=kind, istem_id=row["id"], name=row["name"], defaults={"is_current": True, "synced_at": now})
                seen.append(obj.pk)
            IstemReferenceValue.objects.filter(kind=kind).exclude(pk__in=seen).update(is_current=False)
        counts[kind] = len(rows)
    logger.info("istem reference lists synced by user=%s counts=%s failed=%s", user.pk, counts, sorted(failed))
    return {"counts": counts, "failed": failed, "synced_at": now}


def _choices(kind: str) -> list[dict]:
    return [{"id": v.istem_id, "name": v.name}
            for v in IstemReferenceValue.objects.filter(kind=kind, is_current=True).order_by("name")]


# --- Details ---------------------------------------------------------------------------------------------------------

def _equipment(equipment_id: int):
    from iic_booking.equipment.models import Equipment

    equipment = Equipment.objects.select_related("internal_department").filter(pk=equipment_id).first()
    if equipment is None:
        raise MappingRefused("Equipment not found.", 404)
    return equipment


def _prefill(equipment) -> IstemEquipmentDetails:
    """A new (unsaved) details record filled from what the portal already holds about the instrument."""
    department_ids = list(IstemDepartmentMapping.objects.filter(department_id=equipment.internal_department_id)
                          .values_list("istem_department_id", flat=True)) if equipment.internal_department_id else []
    return IstemEquipmentDetails(
        equipment=equipment,
        equipment_name=(equipment.name or "").strip()[:255],
        equipment_make=(equipment.make or "").strip()[:255],
        equipment_srno=(equipment.asset_serial_number or "").strip()[:120],
        istem_department_id=department_ids[0] if len(department_ids) == 1 else "",
        equipment_model=(equipment.model_information or "").strip()[:255],
        equipment_abbr=(equipment.code or "").strip()[:64],
        equipment_location=" ".join((equipment.location or "").split())[:255],
        equipment_description=(equipment.description or "").strip(),
        supplier_name=(equipment.supplier_name or "").strip()[:255],
        alert_mailid=_oic_email(equipment),
    )


def _oic_email(equipment) -> str:
    """The instrument's first Officer In-charge email, as the default alert address."""
    try:
        from iic_booking.equipment.reports import get_oic_emails_for_equipment

        emails = [e for e in get_oic_emails_for_equipment(equipment.pk) if e]
    except Exception:  # a default only: never block the form
        logger.warning("istem create: OIC email lookup failed for equipment=%s", equipment.pk, exc_info=True)
        return ""
    return emails[0][:255] if emails else ""


def _details(equipment) -> tuple[IstemEquipmentDetails, bool]:
    existing = IstemEquipmentDetails.objects.filter(equipment=equipment).first()
    return (existing, True) if existing else (_prefill(equipment), False)


def problems(details: IstemEquipmentDetails) -> dict[str, str]:
    """{field: what is wrong}. Empty = Equipment Create can be sent."""
    out: dict[str, str] = {}
    for name, _, label, required in FIELDS:
        if required and not str(getattr(details, name) or "").strip():
            out[name] = f"{label} is required by I-STEM."
    if details.istem_department_id and not NUMERIC_ID.match(details.istem_department_id):
        out["istem_department_id"] = "Choose the I-STEM department from the list."
    agencies = {v["id"] for v in _choices(IstemReferenceValue.KIND_FUNDING_AGENCY)}
    if details.funding_agency_id and (not NUMERIC_ID.match(details.funding_agency_id)
                                      or (agencies and details.funding_agency_id not in agencies)):
        out["funding_agency_id"] = "Choose the funding agency from I-STEM's list (Sync from I-STEM first)."
    types = {v["name"] for v in _choices(IstemReferenceValue.KIND_FUNDING_AGENCY_TYPE)}
    if details.funding_agency_type and types and details.funding_agency_type not in types:
        out["funding_agency_type"] = "Choose the funding agency type from I-STEM's list."
    for name, kind in (("category", IstemReferenceValue.KIND_EQUIPMENT_CATEGORY), ("facility_id", IstemReferenceValue.KIND_FACILITY)):
        value = getattr(details, name)
        known = _choices(kind)
        # I-STEM's own example sends the category by name ("Fabrication"); its id is accepted too.
        if value and known and value not in {v["id"] for v in known if v["id"]} | {v["name"] for v in known}:
            out[name] = "Choose a value from I-STEM's list."
    for name in YES_NO_FIELDS:
        value = getattr(details, name)
        if value and value not in YES_NO:
            out[name] = "Choose Yes or No."
    for name in RATE_FIELDS:
        value = getattr(details, name)
        if value and not re.fullmatch(r"[0-9]{1,9}(\.[0-9]{1,2})?", value):
            out[name] = "A rate is a number of rupees (up to 2 decimals)."
    for name in EMAIL_FIELDS:
        value = getattr(details, name)
        if value:
            try:
                validate_email(value)
            except ValidationError:
                out[name] = "Not a valid email address."
    if details.equipment_website:
        website = details.equipment_website
        try:
            # A bare domain ("test.com", as in I-STEM's example) is accepted as well as a full address.
            URLValidator(schemes=["http", "https"])(website if "://" in website else f"https://{website}")
        except ValidationError:
            out["equipment_website"] = "Not a valid web address."
    if details.last_amc_done:
        try:
            date.fromisoformat(details.last_amc_done)
        except ValueError:
            out["last_amc_done"] = "Use the date format YYYY-MM-DD."
    return out


def build_payload(details: IstemEquipmentDetails, institute_id: str) -> dict:
    """The Equipment Create form. Free-text supplier / service provider names go as "Other" plus suppl / ser_prov,
    as I-STEM documents for a value outside its own list."""
    # Every documented field is sent, empty ones as "" ([USER 2026-11-08] Swagger "Send empty value"): I-STEM answered
    # "Failed to insert equipment" to a form that left the empty optional fields out.
    payload: dict[str, str] = {}
    for name, istem_field, _, _ in FIELDS:
        payload[istem_field] = str(getattr(details, name) or "").strip()
    if payload.get("supplier_name"):
        payload["suppl"] = payload["supplier_name"]
        payload["supplier_name"] = "Other"
    if payload.get("service_provider_name"):
        payload["ser_prov"] = payload["service_provider_name"]
        payload["service_provider_name"] = "Other"
    payload["institute_id"] = str(institute_id)
    return payload


def _cached_department_names() -> dict[str, str]:
    """I-STEM department names as last read by the department mapping page (no I-STEM call on page load)."""
    from django.core.cache import cache

    from .equipment_mapping import DEPARTMENT_NAMES_CACHE_KEY, DEPARTMENT_NAMES_SNAPSHOT_KEY

    for key in (DEPARTMENT_NAMES_CACHE_KEY, DEPARTMENT_NAMES_SNAPSHOT_KEY):
        value = cache.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _department_choices(equipment) -> list[dict]:
    names = _cached_department_names()
    mapped = set(IstemDepartmentMapping.objects.filter(department_id=equipment.internal_department_id)
                 .values_list("istem_department_id", flat=True)) if equipment.internal_department_id else set()
    ids = set(names) | mapped
    rows = [{"id": i, "name": names.get(i, f"I-STEM department {i}"), "mapped_to_instrument_department": i in mapped}
            for i in ids]
    return sorted(rows, key=lambda r: (not r["mapped_to_instrument_department"], r["name"].lower()))


def _serialize(equipment, details: IstemEquipmentDetails, saved: bool) -> dict:
    mapping = IstemEquipmentMapping.objects.filter(equipment=equipment).first()
    return {
        "equipment": {"id": equipment.pk, "code": equipment.code, "name": equipment.name,
                      "department": equipment.internal_department.name if equipment.internal_department else ""},
        "details": {name: getattr(details, name) for name in EDITABLE},
        "fields": [{"name": n, "label": label, "required": req} for n, _, label, req in FIELDS],
        "saved": saved,
        "problems": problems(details),
        "created": {"istem_equipment_id": details.created_istem_equipment_id, "at": details.created_at_istem}
        if details.created_istem_equipment_id else None,
        "last_error": details.last_error,
        "mapping": {"istem_equipment_id": mapping.istem_equipment_id, "is_active": mapping.is_active} if mapping else None,
        "choices": {
            "istem_departments": _department_choices(equipment),
            "funding_agencies": _choices(IstemReferenceValue.KIND_FUNDING_AGENCY),
            "funding_agency_types": _choices(IstemReferenceValue.KIND_FUNDING_AGENCY_TYPE),
            "equipment_categories": _choices(IstemReferenceValue.KIND_EQUIPMENT_CATEGORY),
            "facilities": _choices(IstemReferenceValue.KIND_FACILITY),
            "yes_no": [{"id": v, "name": v} for v in YES_NO],
        },
    }


def get_details(user, equipment_id: int) -> dict:
    _require_main_admin(user)
    equipment = _equipment(equipment_id)
    details, saved = _details(equipment)
    return _serialize(equipment, details, saved)


def save_details(user, equipment_id: int, data: dict) -> dict:
    _require_main_admin(user)
    equipment = _equipment(equipment_id)
    with transaction.atomic():
        details, saved = _details(equipment)
        if saved:
            details = IstemEquipmentDetails.objects.select_for_update().get(pk=details.pk)
        if details.created_istem_equipment_id:
            raise MappingRefused("This instrument was already created on I-STEM; its details there are changed on I-STEM.", 409)
        for name in EDITABLE:
            if name in (data or {}):
                value = data.get(name)
                field = IstemEquipmentDetails._meta.get_field(name)
                text = " ".join(str(value or "").split()) if name != "equipment_description" else str(value or "").strip()
                setattr(details, name, text[: field.max_length] if field.max_length else text)
        details.updated_by = user
        details.save()
    return _serialize(equipment, details, True)


# --- Equipment Create ------------------------------------------------------------------------------------------------

def _listed_with_serial(client, institute_id: str, serial: str) -> dict | None:
    wanted = re.sub(r"\s+", "", serial or "").lower()
    if not wanted:
        return None
    for row in client.list_all_equipment(institute_id=institute_id):
        if re.sub(r"\s+", "", row.get("serial") or "").lower() == wanted:
            return row
    return None


def _default_key(equipment) -> str:
    return _default_sample_key(sorted(set(equipment.input_fields.values_list("field_key", flat=True))))


def _record_created(user, equipment, details: IstemEquipmentDetails, istem_id: str, how: str) -> None:
    now = timezone.now()
    with transaction.atomic():
        details.created_istem_equipment_id = istem_id
        details.created_at_istem = now
        details.created_by = user
        details.last_error = ""
        details.save()
        IstemEquipmentMapping.objects.create(
            equipment=equipment, istem_equipment_id=istem_id, sample_count_input_key=_default_key(equipment),
            is_active=True, verified_by=user, verified_at=now, remote_found=True, remote_checked_at=now,
            remote_equipment_name=details.equipment_name[:255],
            notes=f"Created on I-STEM from the portal ({how}) on {now:%Y-%m-%d}.")
    _forget_listings()


def create_on_istem(user, equipment_id: int, *, dry_run: bool = False, client_factory=None) -> dict:
    """Send Equipment Create for the instrument (``dry_run``: build and return the form without sending it)."""
    _require_main_admin(user)
    equipment = _equipment(equipment_id)
    details = IstemEquipmentDetails.objects.filter(equipment=equipment).first()
    if details is None:
        raise MappingRefused("Save the instrument's I-STEM details first.")
    if details.created_istem_equipment_id:
        raise MappingRefused(f"Already created on I-STEM as equipment {details.created_istem_equipment_id}.", 409)
    if IstemEquipmentMapping.objects.filter(equipment=equipment).exists():
        raise MappingRefused("This instrument is already mapped to I-STEM equipment; it needs no new I-STEM record.", 409)
    found = problems(details)
    if found:
        raise MappingRefused("Complete the I-STEM details first: " + " ".join(found.values()))
    try:
        client = _client(client_factory)
        institute_id = _token_institute(client)
        payload = build_payload(details, institute_id)
        if dry_run:
            return {"dry_run": True, "payload": payload}
        duplicate = _listed_with_serial(client, institute_id, details.equipment_srno)
        if duplicate:
            raise MappingRefused(
                f"I-STEM already lists \"{duplicate['equipment_name']}\" (equipment {duplicate['equipment_id']}) with "
                "serial number " + details.equipment_srno + ". Map the instrument to it instead of creating another.", 409)
        details.last_attempt_at = timezone.now()
        details.save(update_fields=["last_attempt_at", "updated_at"])
        try:
            istem_id = client.create_equipment(payload)
            how = "Equipment Create"
        except IstemError as exc:
            if exc.category != ErrorCategory.INVALID_RESPONSE:
                raise
            # Created, but the answer named no id: find it by serial number rather than risk a second create.
            listed = _listed_with_serial(client, institute_id, details.equipment_srno)
            if not listed:
                raise
            istem_id, how = listed["equipment_id"], "Equipment Create, id read from I-STEM's list"
    except IstemError as exc:
        # I-STEM's own refusal text tells the admin which field to correct.
        details.last_error = (str(exc) if exc.category == ErrorCategory.BUSINESS_ERROR else istem_problem(exc))[:2000]
        details.save(update_fields=["last_error", "updated_at"])
        logger.warning("istem equipment create failed equipment=%s category=%s", equipment.pk, exc.category)
        raise MappingRefused(details.last_error, 502 if exc.category == ErrorCategory.BUSINESS_ERROR else 503) from None
    _record_created(user, equipment, details, istem_id, how)
    logger.info("istem equipment created equipment=%s istem_equipment_id=%s by user=%s", equipment.pk, istem_id, user.pk)
    warnings = []
    mapping = IstemEquipmentMapping.objects.get(equipment=equipment)
    if not mapping.sample_count_input_key:
        warnings.append("Choose the sample-count field on the mapping page; I-STEM bookings wait until it is set.")
    warnings.append("Set the instrument's I-STEM codes (service type, unit, analysis type, user types) before bookings sync.")
    return {"created": True, "istem_equipment_id": istem_id, "warnings": warnings,
            **_serialize(equipment, details, True)}
