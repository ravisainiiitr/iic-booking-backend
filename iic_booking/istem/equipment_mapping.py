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
from .models import IstemBookingLink, IstemDepartmentMapping, IstemEquipmentMapping

logger = logging.getLogger(__name__)

ISTEM_ID = re.compile(r"^[0-9]{1,12}$")
SEARCH_MIN_CHARS = 3
SEARCH_LIMIT = 50
# Links in these states are finished (or never started): the instrument's I-STEM ID may change under them.
CLOSED_STATES = frozenset({
    SyncState.NOT_ELIGIBLE, SyncState.EXECUTED, SyncState.REMOTE_CANCELLED, SyncState.REMOTE_REJECTED,
    SyncState.ABANDONED,
})


_DOWN = frozenset({"CONNECT_FAILED", "TLS", "AUTH_UNAVAILABLE", "READ_TIMEOUT", "CONNECTION_LOST", "SERVER_ERROR",
                   "RATE_LIMITED", "INVALID_RESPONSE"})


def istem_problem(exc: IstemError) -> str:
    """What to tell the admin when I-STEM did not answer: its server is down, or it refused the portal's sign-in."""
    category = str(getattr(exc, "category", "") or "")
    if category in ("AUTH", "FORBIDDEN"):
        return "I-STEM refused the portal's sign-in. The portal's I-STEM user name and password need checking."
    if category in _DOWN:
        return "I-STEM's server is not answering at the moment (it is run by IISc)."
    return "I-STEM did not give a usable answer."


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
    departments: dict = {}
    linked: dict = {}
    for department_id, istem_id in IstemDepartmentMapping.objects.values_list("department_id", "istem_department_id"):
        linked.setdefault(department_id, []).append(istem_id)
    for row in rows:
        d = row["department"] or {"id": None, "name": "No department"}
        s = departments.setdefault(d["id"], {"id": d["id"], "name": d["name"], "total": 0, "mapped": 0,
                                             "fbr_unmapped": 0, "istem_department_ids": sorted(linked.get(d["id"], []))})
        s["total"] += 1
        active = bool(row["mapping"] and row["mapping"]["is_active"])
        s["mapped"] += active
        s["fbr_unmapped"] += bool(row["requires_istem_fbr"] and not active)
    return {"istem_connected": conf.network_allowed(), "all_departments": managed_department_id(user) is None,
            "equipment": rows, "departments": sorted(departments.values(), key=lambda d: (d["name"] or "").lower())}


def search_istem_equipment(user, query: str, client_factory=None, source: dict | None = None) -> list[dict]:
    """Active I-STEM equipment of the portal's institute whose name contains ``query`` (read-only Equipment List).
    While I-STEM is not answering, the last list read from it is searched instead (``source`` says which)."""
    from .service import default_client

    managed_department_id(user)  # permission check
    query = " ".join(str(query or "").split())
    if len(query) < SEARCH_MIN_CHARS:
        raise MappingRefused(f"Type at least {SEARCH_MIN_CHARS} letters of the I-STEM equipment name.")
    if not conf.network_allowed():
        raise MappingRefused("I-STEM is not connected on this portal.", 503)
    client = (client_factory or default_client)()
    try:
        institute_id = _token_institute(client)
        page = client.list_equipment(institute_id=institute_id, equipment_name=query, limit=SEARCH_LIMIT)
        rows = page.rows
        if source is not None:
            source.update(_live_source(institute_id=institute_id))
    except IstemError as exc:
        snapshot = _snapshot()
        if snapshot is None:
            raise MappingRefused(f"{istem_problem(exc)} Try again later.", 502) from exc
        needle = query.lower()
        rows = [r for r in snapshot["rows"] if needle in (r.get("equipment_name") or "").lower()][:SEARCH_LIMIT]
        if source is not None:
            source.update(_saved_source(snapshot, exc))
    used = _mapped_to(r["equipment_id"] for r in rows)
    return [{**row, "mapped_to": used.get(row["equipment_id"], [])} for row in rows]


def save_mapping(user, equipment_id: int, data: dict, *, client_factory=None, check_remote: bool = True) -> dict:
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
    check = None
    if check_remote and conf.network_allowed():
        try:
            check = verify(user, equipment_ids=[equipment.pk], client_factory=client_factory)
        except MappingRefused:
            warnings.append("Saved. I-STEM is not answering, so the ID will be checked on I-STEM later.")
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
        raise MappingRefused(f"{istem_problem(exc)} Try again later.", 502) from exc


# --- Department-wise matching against I-STEM's own listing ------------------------------------------------------------
# I-STEM lists the institute's equipment by ID and name only (no department), so a department's unmapped instruments
# are matched by name against the whole listing; the Main Admin (or the department's administrator) confirms each one.

LISTING_CACHE_KEY = "istem:equipment_listing"
LISTING_CACHE_SECONDS = 600
SUGGESTIONS_PER_INSTRUMENT = 3
SUGGESTION_MIN_SCORE = 0.45
BULK_LIMIT = 200
_STOPWORDS = frozenset({"iic", "the", "of", "and", "for", "with", "a", "an", "system", "instrument", "equipment", "facility"})
_PAREN = re.compile(r"\(([^)]{2,20})\)")
_CODE_PREFIX = re.compile(r"[A-Za-z]{2,}")


LISTING_SNAPSHOT_KEY = "istem:equipment_listing:last"


def _snapshot() -> dict | None:
    from django.core.cache import cache

    snapshot = cache.get(LISTING_SNAPSHOT_KEY)
    return snapshot if isinstance(snapshot, dict) and isinstance(snapshot.get("rows"), list) else None


def _live_source(read_at: str | None = None, institute_id: str | None = None) -> dict:
    return {"live": True, "read_at": read_at or timezone.now().isoformat(), "problem": "", "institute_id": institute_id}


def _saved_source(snapshot: dict, exc: IstemError) -> dict:
    return {
        "live": False,
        "read_at": snapshot.get("read_at"),
        "problem": istem_problem(exc),
        "institute_id": snapshot.get("institute_id"),
    }


def _token_institute(client) -> str:
    """The institute named in I-STEM's token response: the mapping covers that institute's equipment only."""
    institute_id = client.session_info().institute_id
    if not institute_id:
        raise MappingRefused(
            "I-STEM signed the portal in but named no institute (institute_id), so its equipment list cannot be "
            "limited to this institute. Ask I-STEM to include institute_id in the token response.",
            502,
        )
    return str(institute_id)


def listing_with_source(*, refresh: bool = False, client_factory=None) -> tuple[list[dict], dict]:
    """Every active I-STEM equipment of the institute, and where it came from. Read from I-STEM at most every ten
    minutes; the last good read is kept, so while I-STEM is not answering the page works from it (``live`` False,
    ``read_at`` when it was read, ``problem`` why)."""
    from django.core.cache import cache

    from .service import default_client

    if not conf.network_allowed():
        raise MappingRefused("I-STEM is not connected on this portal.", 503)
    fresh = None if refresh else cache.get(LISTING_CACHE_KEY)
    if isinstance(fresh, dict) and isinstance(fresh.get("rows"), list) and fresh.get("institute_id"):
        return fresh["rows"], _live_source(fresh.get("read_at"), fresh.get("institute_id"))
    client = (client_factory or default_client)()
    try:
        institute_id = _token_institute(client)
        rows = client.list_all_equipment(institute_id=institute_id)
    except IstemError as exc:
        snapshot = _snapshot()
        if snapshot is None:
            raise MappingRefused(f"{istem_problem(exc)} Its equipment list has not been read before, so try again "
                                 "later.", 502) from exc
        logger.warning("istem equipment list unavailable (%s); using the list read at %s", exc.category,
                       snapshot.get("read_at"))
        return snapshot["rows"], _saved_source(snapshot, exc)
    value = {"rows": rows, "read_at": timezone.now().isoformat(), "institute_id": institute_id}
    cache.set(LISTING_CACHE_KEY, value, LISTING_CACHE_SECONDS)
    cache.set(LISTING_SNAPSHOT_KEY, value, None)
    return rows, _live_source(value["read_at"], institute_id)


def _istem_listing(*, refresh: bool = False, client_factory=None) -> list[dict]:
    return listing_with_source(refresh=refresh, client_factory=client_factory)[0]


def _mapped_to(istem_ids) -> dict[str, list[dict]]:
    used: dict[str, list[dict]] = {}
    for equipment_id, istem_id, name in (IstemEquipmentMapping.objects.filter(istem_equipment_id__in=list(istem_ids),
                                                                                 is_active=True)
                                         .values_list("equipment_id", "istem_equipment_id", "equipment__name")):
        used.setdefault(istem_id, []).append({"equipment_id": equipment_id, "name": name})
    return used


def istem_listing(user, *, refresh: bool = False, client_factory=None) -> dict:
    """I-STEM's equipment listing for the institute, each row with the portal instruments mapped to it."""
    managed_department_id(user)  # permission check
    rows, source = listing_with_source(refresh=refresh, client_factory=client_factory)
    used = _mapped_to(r["equipment_id"] for r in rows)
    out = sorted(({**r, "mapped_to": used.get(r["equipment_id"], [])} for r in rows),
                 key=lambda r: (r["equipment_name"] or "").lower())
    return {"equipment": out, "total": len(out), "mapped": sum(1 for r in out if r["mapped_to"]), "istem_source": source}


def _words(name: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split() if w not in _STOPWORDS}


def _acronyms(name: str, code: str = "") -> set[str]:
    """"X-ray Fluorescence (XRF) -IIC" -> {"XRF"}; "FESEM-01" (code) -> {"FESEM"}; upper-case words of two letters up."""
    out = {re.sub(r"[^A-Z0-9]", "", p.upper()) for p in _PAREN.findall(name or "")}
    out |= {w for w in re.findall(r"\b[A-Z][A-Z0-9-]{1,}\b", name or "") if w.upper() != "IIC"}
    prefix = _CODE_PREFIX.match(code or "")
    if prefix:
        out.add(prefix.group(0).upper())
    return {a.replace("-", "") for a in out if len(a.replace("-", "")) >= 2}


def match_score(portal_name: str, portal_code: str, istem_name: str) -> float:
    """0..1: how likely the I-STEM name is the same instrument (shared words, spelling, a shared acronym)."""
    from difflib import SequenceMatcher

    a, b = _words(portal_name), _words(istem_name)
    overlap = len(a & b) / len(a | b) if a and b else 0.0
    spelling = SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio() * 0.9
    score = max(overlap, spelling)
    if _acronyms(portal_name, portal_code) & _acronyms(istem_name):
        score = max(score, 0.85)
    return round(score, 2)


def _default_sample_key(keys: list[str]) -> str:
    """The sample-count field to propose: "A" (the portal's usual "number of samples") or the only field."""
    if "A" in keys:
        return "A"
    return keys[0] if len(keys) == 1 else ""


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def score_row(equipment, row: dict) -> tuple[float, list[str]]:
    """How likely the I-STEM entry is this instrument, and why: same serial number (certain), same model, the
    I-STEM short name in the instrument's name or code, then the names themselves."""
    reasons: list[str] = []
    score = match_score(equipment.name, equipment.code, row.get("equipment_name", ""))
    if score >= SUGGESTION_MIN_SCORE:
        reasons.append("name")
    serial = _compact(getattr(equipment, "asset_serial_number", ""))
    if len(serial) >= 4 and serial == _compact(row.get("serial", "")):
        score = 1.0
        reasons.insert(0, "serial number")
    model = _compact(getattr(equipment, "model_information", ""))
    if len(model) >= 3 and model == _compact(row.get("model", "")):
        score = max(score, 0.95)
        reasons.insert(0, "model")
    abbr = re.sub(r"[^A-Z0-9]", "", (row.get("abbr") or "").upper())
    if len(abbr) >= 2 and abbr in _acronyms(equipment.name, equipment.code):
        score = max(score, 0.9)
        reasons.append("short name")
    make = _words(getattr(equipment, "make", ""))
    if make and make & _words(row.get("make", "")) and score >= SUGGESTION_MIN_SCORE:
        score = min(1.0, score + 0.05)
        reasons.append("make")
    return round(score, 2), reasons


CONFIDENT_SCORE = 0.9
CONFIDENT_GAP = 0.1


def _istem_departments_of(department_ids) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for department_id, istem_id in (IstemDepartmentMapping.objects.filter(department_id__in=list(department_ids))
                                    .values_list("department_id", "istem_department_id")):
        out.setdefault(department_id, set()).add(istem_id)
    return out


def suggestions(user, *, department_id=None, equipment_id=None, refresh: bool = False, client_factory=None) -> dict:
    """A department's instruments without an active mapping (or one instrument, mapped or not), each with up to three
    I-STEM equipment. Searched automatically in the I-STEM departments mapped to the instrument's department (the whole
    institute list when none is mapped). ``confident``: the top match is certain enough to save without picking."""
    from iic_booking.equipment.models import ChargeProfile, DynamicInputField

    scope = managed_department_id(user)
    if scope is not None:
        department_id = scope
    listing, source = listing_with_source(refresh=refresh, client_factory=client_factory)
    used = _mapped_to(r["equipment_id"] for r in listing)
    qs = managed_equipment(user)
    if equipment_id:
        equipment = list(qs.filter(pk=int(equipment_id)))
        if not equipment:
            raise MappingRefused("Equipment not found among the instruments you manage.", 404)
    else:
        if department_id:
            qs = qs.filter(internal_department_id=int(department_id))
        mapped = set(IstemEquipmentMapping.objects.filter(is_active=True).values_list("equipment_id", flat=True))
        equipment = [e for e in qs.order_by("name", "pk") if e.pk not in mapped]
    istem_departments = _istem_departments_of({e.internal_department_id for e in equipment if e.internal_department_id})
    ids = [e.pk for e in equipment]
    needs_fbr = set(ChargeProfile.objects.filter(equipment_id__in=ids, is_active=True, require_istem_fbr=True)
                    .values_list("equipment_id", flat=True))
    fields: dict[int, dict[str, str]] = {}
    for equipment_id, key, label in (DynamicInputField.objects.filter(equipment_id__in=ids)
                                     .order_by("field_key").values_list("equipment_id", "field_key", "field_label")):
        fields.setdefault(equipment_id, {}).setdefault(key, label or key)
    rows = []
    for e in equipment:
        narrowed_to = sorted(istem_departments.get(e.internal_department_id, set()))
        pool = [r for r in listing if r.get("department_id") in narrowed_to] if narrowed_to else listing
        scored = sorted(((*score_row(e, r), r) for r in pool), key=lambda s: (-s[0], s[2]["equipment_name"]))
        candidates = [{**r, "score": s, "reasons": why,
                       "mapped_to": [m for m in used.get(r["equipment_id"], []) if m["equipment_id"] != e.pk]}
                      for s, why, r in scored[:SUGGESTIONS_PER_INSTRUMENT] if s >= SUGGESTION_MIN_SCORE]
        top = candidates[0] if candidates else None
        confident = bool(top and top["score"] >= CONFIDENT_SCORE and not top["mapped_to"]
                         and (len(candidates) == 1 or candidates[1]["score"] <= top["score"] - CONFIDENT_GAP))
        department = e.internal_department
        keys = sorted(fields.get(e.pk, {}))
        rows.append({
            "equipment_id": e.pk,
            "code": e.code,
            "name": e.name,
            "department": {"id": department.pk, "name": department.name} if department else None,
            "requires_istem_fbr": e.pk in needs_fbr,
            "input_fields": [{"key": k, "label": fields[e.pk][k]} for k in keys],
            "default_sample_key": _default_sample_key(keys),
            "istem_departments": narrowed_to,
            "candidates": candidates,
            "confident": confident,
        })
    return {"department_id": int(department_id) if department_id else None, "istem_total": len(listing),
            "equipment": rows, "istem_source": source}


# --- Portal departments <-> I-STEM departments (Main Admin) -----------------------------------------------------------

SAMPLE_NAMES = 5


def _require_main_admin(user) -> None:
    if not _is_main_admin(user):
        raise MappingRefused("Only the Main Administrator maps departments to I-STEM.", 403)


DEPARTMENT_NAMES_CACHE_KEY = "istem:department_names"
DEPARTMENT_NAMES_SNAPSHOT_KEY = "istem:department_names:last"
DEPARTMENT_NAMES_CACHE_SECONDS = 6 * 3600
DEPARTMENT_NAME_MIN_SCORE = 0.75
_DEPARTMENT_FILLER = {"department", "dept", "of", "the", "and", "centre", "center", "for", "in"}


def istem_department_names(*, refresh: bool = False, client_factory=None) -> dict[str, str]:
    """{I-STEM department id: name} from I-STEM's Departments list for the token's institute. Cached for six hours;
    the last good read is used while I-STEM is not answering, and {} when it has never been read."""
    from django.core.cache import cache

    from .service import default_client

    fresh = None if refresh else cache.get(DEPARTMENT_NAMES_CACHE_KEY)
    if isinstance(fresh, dict):
        return fresh
    try:
        client = (client_factory or default_client)()
        names = {d["id"]: d["name"] for d in client.list_departments(institute_id=_token_institute(client))}
    except (IstemError, MappingRefused) as exc:
        logger.warning("istem department names unavailable (%s)", getattr(exc, "category", exc))
        saved = cache.get(DEPARTMENT_NAMES_SNAPSHOT_KEY)
        return saved if isinstance(saved, dict) else {}
    cache.set(DEPARTMENT_NAMES_CACHE_KEY, names, DEPARTMENT_NAMES_CACHE_SECONDS)
    cache.set(DEPARTMENT_NAMES_SNAPSHOT_KEY, names, None)
    return names


def _department_words(name: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split() if w not in _DEPARTMENT_FILLER}


def department_name_score(istem_name: str, portal_name: str) -> float:
    """0..1: "Department of Chemistry" vs "Chemistry Department", "Institute Instrumentation Centre " vs "IIC"."""
    from difflib import SequenceMatcher

    a, b = _department_words(istem_name), _department_words(portal_name)
    if not a or not b:
        return 0.0
    overlap = len(a & b) / len(a | b)
    spelling = SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()
    return round(max(overlap, spelling), 2)


def _suggest_by_name(istem_name: str, portal: dict[int, str], codes: dict[int, str]) -> dict | None:
    best = None
    for pk, name in portal.items():
        score = department_name_score(istem_name, name)
        code = (codes.get(pk) or "").strip().lower()
        if code and code in {w.lower() for w in re.findall(r"\b[A-Z]{2,}\b", istem_name or "")}:
            score = max(score, 0.95)  # "Institute Computer Centre ICC" -> department code ICC
        if best is None or score > best[1]:
            best = (pk, score)
    if best is None or best[1] < DEPARTMENT_NAME_MIN_SCORE:
        return None
    return {"id": best[0], "name": portal[best[0]], "instruments": 0, "by": "name", "score": best[1]}


def istem_departments(user, *, refresh: bool = False, client_factory=None) -> dict:
    """Every I-STEM department of the institute, with its name (I-STEM's Departments list) and what its equipment
    list shows (count, a few names and locations), the portal department it is mapped to, and a suggestion: the
    portal departments of instruments already mapped to its equipment, else the portal department with the most
    similar name."""
    from collections import Counter

    from iic_booking.users.models import Department

    _require_main_admin(user)
    listing, source = listing_with_source(refresh=refresh, client_factory=client_factory)
    names = istem_department_names(refresh=refresh, client_factory=client_factory)
    groups: dict[str, list[dict]] = {}
    for r in listing:
        groups.setdefault(r.get("department_id") or "", []).append(r)
    for istem_department_id in names:
        groups.setdefault(istem_department_id, [])
    mapped = {m.istem_department_id: m for m in IstemDepartmentMapping.objects.select_related("department")}
    by_istem_equipment: dict[str, list[int]] = {}
    for istem_id, department_id in (IstemEquipmentMapping.objects.filter(is_active=True, equipment__internal_department__isnull=False)
                                    .values_list("istem_equipment_id", "equipment__internal_department_id")):
        by_istem_equipment.setdefault(istem_id, []).append(department_id)
    departments = {d.pk: d.name for d in Department.objects.all()}
    codes = dict(Department.objects.values_list("pk", "code"))
    rows = []
    for istem_department_id, items in sorted(groups.items(), key=lambda g: (g[0] == "", -len(g[1]), g[0])):
        votes = Counter(d for r in items for d in by_istem_equipment.get(r["equipment_id"], []))
        suggested = votes.most_common(1)[0] if votes else None
        current = mapped.get(istem_department_id)
        name = names.get(istem_department_id, "")
        by_name = None if suggested or not name else _suggest_by_name(name, departments, codes)
        rows.append({
            "istem_department_id": istem_department_id,
            "name": name,
            "equipment_count": len(items),
            "sample_equipment": sorted(r["equipment_name"] for r in items)[:SAMPLE_NAMES],
            "locations": [loc for loc, _ in Counter(r.get("location") for r in items if r.get("location")).most_common(3)],
            "department": {"id": current.department_id, "name": current.department.name} if current else None,
            "suggested": ({"id": suggested[0], "name": departments.get(suggested[0], ""), "instruments": suggested[1],
                           "by": "instruments"}
                          if suggested else by_name),
        })
    portal = sorted(({"id": pk, "name": name} for pk, name in departments.items()), key=lambda d: d["name"].lower())
    return {"istem_departments": rows, "portal_departments": portal, "istem_source": source}


def save_department_mappings(user, items) -> dict:
    """``items``: [{istem_department_id, department_id}] - department_id empty/null removes that I-STEM department's
    mapping. Several I-STEM departments may belong to one portal department."""
    from iic_booking.users.models import Department

    _require_main_admin(user)
    if not isinstance(items, list) or not items:
        raise MappingRefused("Nothing to save.")
    known = set(Department.objects.values_list("pk", flat=True))
    saved = removed = 0
    with transaction.atomic():
        for item in items:
            istem_id = str((item or {}).get("istem_department_id") or "").strip()
            if not ISTEM_ID.match(istem_id):
                raise MappingRefused("The I-STEM department ID is a number.")
            department_id = (item or {}).get("department_id")
            if department_id in (None, ""):
                removed += IstemDepartmentMapping.objects.filter(istem_department_id=istem_id).delete()[0]
                continue
            try:
                department_id = int(department_id)
            except (TypeError, ValueError):
                raise MappingRefused("Choose a portal department.") from None
            if department_id not in known:
                raise MappingRefused("That portal department does not exist.", 404)
            IstemDepartmentMapping.objects.update_or_create(
                istem_department_id=istem_id, defaults={"department_id": department_id, "set_by": user})
            saved += 1
    logger.info("istem department mappings: %s saved, %s removed by user=%s", saved, removed, user.pk)
    return {"saved": saved, "removed": removed}


def save_many(user, items, *, client_factory=None) -> dict:
    """Save several mappings at once (the department matching screen); each row succeeds or fails on its own, with
    the same rules as saving one. One check against I-STEM's listing afterwards."""
    if not isinstance(items, list) or not items:
        raise MappingRefused("Choose at least one instrument to map.")
    if len(items) > BULK_LIMIT:
        raise MappingRefused(f"Save at most {BULK_LIMIT} mappings at a time.")
    results, saved = [], []
    for item in items:
        try:
            equipment_id = int((item or {}).get("equipment_id"))
        except (TypeError, ValueError):
            results.append({"equipment_id": None, "ok": False, "error": "Missing instrument."})
            continue
        try:
            out = save_mapping(user, equipment_id, item, client_factory=client_factory, check_remote=False)
        except MappingRefused as exc:
            results.append({"equipment_id": equipment_id, "ok": False, "error": exc.message})
            continue
        saved.append(equipment_id)
        results.append({"equipment_id": equipment_id, "ok": True, "warnings": out["warnings"]})
    checked = False
    if saved and conf.network_allowed():
        try:
            verify(user, equipment_ids=saved, client_factory=client_factory)
            checked = True
        except MappingRefused:
            checked = False
    return {"results": results, "saved": len(saved), "failed": len(results) - len(saved), "checked": checked}
