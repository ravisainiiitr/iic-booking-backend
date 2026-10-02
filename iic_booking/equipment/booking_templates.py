"""Named booking templates: a user's saved booking inputs and booking options for one equipment."""

import json

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import BookingInputTemplate, DynamicInputField, DynamicInputFieldType, Equipment, EquipmentStatus
from .template_slot_preference import (
    AUTO_NEXT_MODES,
    IF_SLOT_TAKEN_ASK,
    MAX_PREFERRED_SLOT_COUNT,
    apply_preference_fields,
    clean_if_slot_taken,
    clean_preferred_slot,
    has_preferred_slot,
    resolve_preferred_slot,
    serialize_preference,
)

MAX_TEMPLATES_PER_EQUIPMENT = 25
MAX_NAME_LENGTH = 80
MAX_INPUT_VALUES_BYTES = 50_000
MAX_WORKSPACE_ID_LENGTH = 64
RESEARCH_WORKSPACE_KEY = "research_workspace"
SAMPLE_SETS_KEY = "_sample_sets"
SUMMARY_MAX_ITEMS = 4
SUMMARY_MAX_VALUE_LENGTH = 60

OPTION_KEYS = (
    "auto_slot_selection",
    "book_any_available_slots",
    "book_even_if_single_slot_available",
    "waitlist_on_failure",
    "auto_allocate_alternative",
    "sample_return_after_analysis",
    "atmosphere_sensitive_sample",
)


def normalise_slot_options(options, *, has_preferred):
    """Slots are chosen one way (yourself, auto-select or the preferred slot) and "a single slot is fine"
    only extends "book any free slots"; contradictory flags from older templates or clients are dropped."""
    options = dict(options) if isinstance(options, dict) else {}
    if has_preferred and options.get("auto_slot_selection") is True:
        options["auto_slot_selection"] = False
    if options.get("book_even_if_single_slot_available") is True and options.get("book_any_available_slots") is not True:
        options["book_even_if_single_slot_available"] = False
    return options


def _normalise_template(template):
    """Make the saved choices consistent before saving; return the extra fields that changed."""
    changed = []
    current = template.options if isinstance(template.options, dict) else {}
    options = normalise_slot_options(current, has_preferred=has_preferred_slot(template))
    if options != current:
        template.options = options
        changed.append("options")
    if options.get("book_any_available_slots") is True and template.if_slot_taken in AUTO_NEXT_MODES:
        template.if_slot_taken = IF_SLOT_TAKEN_ASK
        template.if_slot_taken_consented_at = None
        changed.extend(["if_slot_taken", "if_slot_taken_consented_at"])
    return changed


def template_booking_block(user, equipment):
    """Why ``user`` may not keep a template for ``equipment`` (same access rules as booking it), or None."""
    from iic_booking.users.legacy_ledger.booking_lock import department_equipment_booking_blocked

    from .api_views import user_can_see_equipment

    if not user_can_see_equipment(user, equipment):
        return "You are not authorized to book this equipment."
    blocked, message = department_equipment_booking_blocked(equipment, user)
    if blocked:
        return message
    return None


def _field_labels(user, equipment_ids):
    """{equipment_id: {field_key: (label, field_type)}}, preferring fields scoped to the user's type."""
    if not equipment_ids:
        return {}
    user_type = str(getattr(user, "user_type", "") or "")
    rows = DynamicInputField.objects.filter(
        equipment_id__in=list(equipment_ids), user_type__in=[user_type, ""]
    ).values_list("equipment_id", "user_type", "field_key", "field_label", "field_type")
    labels = {}
    for equipment_id, field_user_type, key, label, field_type in rows:
        per_equipment = labels.setdefault(equipment_id, {})
        if key in per_equipment and not field_user_type:
            continue
        per_equipment[key] = (label or key, field_type)
    return labels


def _summary_value(value, field_type, elements):
    if elements:
        return elements
    if field_type == DynamicInputFieldType.TABLE and isinstance(value, list):
        rows = sum(1 for row in value if isinstance(row, list) and any(str(c).strip() for c in row))
        return f"{rows} row{'' if rows == 1 else 's'}" if rows else None
    if isinstance(value, bool):
        return "Yes" if value else None
    if isinstance(value, list):
        text = ", ".join(str(v) for v in value if not isinstance(v, (list, dict)) and str(v).strip())
    elif isinstance(value, dict):
        return None
    else:
        text = str(value if value is not None else "").strip()
    if not text:
        return None
    if len(text) > SUMMARY_MAX_VALUE_LENGTH:
        text = text[: SUMMARY_MAX_VALUE_LENGTH - 1].rstrip() + "…"
    return text


def input_summary(input_values, labels):
    """Up to SUMMARY_MAX_ITEMS filled inputs as [{key, label, value}], in field-key order."""
    values = input_values if isinstance(input_values, dict) else {}
    items = []
    for key in sorted(labels):
        if key not in values:
            continue
        label, field_type = labels[key]
        raw_elements = values.get(f"{key}_elements")
        elements = raw_elements.strip() if isinstance(raw_elements, str) else ""
        text = _summary_value(values[key], field_type, elements.replace(",", ", ") if elements else "")
        if text is None:
            continue
        items.append({"key": key, "label": label, "value": text})
        if len(items) >= SUMMARY_MAX_ITEMS:
            break
    return items


def _sample_set_count(input_values):
    extra = (input_values or {}).get(SAMPLE_SETS_KEY) if isinstance(input_values, dict) else None
    return 1 + (sum(1 for s in extra if isinstance(s, dict)) if isinstance(extra, list) else 0)


def _serialize(template, *, labels=None, booking_block=False, health=None):
    """``booking_block`` is the template_booking_block() result, or False when it was not computed;
    ``health`` is the template_health.check_template() result when asked for."""
    equipment = template.equipment
    department = getattr(equipment, "internal_department", None) if equipment is not None else None
    input_values = template.input_values or {}
    data = {
        "id": template.pk,
        "equipment": template.equipment_id,
        "equipment_code": getattr(equipment, "code", None),
        "equipment_name": getattr(equipment, "name", None),
        "equipment_status": getattr(equipment, "status", None),
        "equipment_parent": getattr(equipment, "parent_equipment_id", None),
        "department_id": getattr(department, "id", None),
        "department_name": getattr(department, "name", None),
        "department_code": getattr(department, "code", None),
        "name": template.name,
        "input_values": input_values,
        "options": normalise_slot_options(template.options, has_preferred=has_preferred_slot(template)),
        "sample_set_count": _sample_set_count(input_values),
        **serialize_preference(template),
        "created_at": template.created_at.isoformat() if template.created_at else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
    }
    if labels is not None:
        data["input_summary"] = input_summary(input_values, labels.get(template.equipment_id, {}))
    if booking_block is not False:
        operational = (getattr(equipment, "status", "") or "").strip() == EquipmentStatus.ACTIVE
        data["bookable"] = booking_block is None and operational
        data["booking_block_reason"] = booking_block or (
            None if operational else "This equipment is not operational right now."
        )
    if health is not None:
        data["health"] = health
    return data


def _clean_name(raw):
    name = " ".join(str(raw or "").split())
    if not name:
        return None, "Give the template a name."
    if len(name) > MAX_NAME_LENGTH:
        return None, f"Template name must be at most {MAX_NAME_LENGTH} characters."
    return name, None


def _numeric_minimum_error(equipment, values, user, baseline=None):
    """First numeric input below its minimum (at least 1), or above a formula maximum worked out from its
    own sample set (e.g. A <= B*4), in sample set 1 or an extra set, else None.

    Fixed maximums are checked when the booking is made. Unchanged values of the stored template
    (``baseline``) saved before the minimum of 1 existed are kept, as is a set left exactly as stored.
    """
    from .api_views import _sample_set_groups_limit_error

    return _sample_set_groups_limit_error(
        equipment, values, booking_user=user, baseline=baseline if isinstance(baseline, dict) else {},
        check_max=False, check_formula_max=True,
    )


def _clean_input_values(raw, equipment=None, user=None, baseline=None):
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, "input_values must be an object."
    if len(json.dumps(raw)) > MAX_INPUT_VALUES_BYTES:
        return None, "The template's inputs are too large to save."
    if equipment is not None:
        from .sample_set_limits import combined_max_error, sample_sets_disabled_error

        error = (
            sample_sets_disabled_error(equipment, raw, baseline)
            or _numeric_minimum_error(equipment, raw, user, baseline)
            or combined_max_error(equipment, raw, booking_user=user)
        )
        if error:
            return None, error
    return raw, None


def _clean_options(raw):
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, "options must be an object."
    options = {key: raw[key] for key in OPTION_KEYS if isinstance(raw.get(key), bool)}
    # Linking to the workspace is checked when a booking is made; a template only remembers the choice.
    if RESEARCH_WORKSPACE_KEY in raw:
        workspace = raw[RESEARCH_WORKSPACE_KEY]
        if workspace is None or workspace == "":
            options[RESEARCH_WORKSPACE_KEY] = None
        elif isinstance(workspace, str) and len(workspace.strip()) <= MAX_WORKSPACE_ID_LENGTH:
            options[RESEARCH_WORKSPACE_KEY] = workspace.strip()
        else:
            return None, "research_workspace must be a workspace id."
    return options, None


def _apply_preference(template, data, *, partial):
    """Apply preferred_slot / if_slot_taken from the payload; return an error string or None."""
    if partial and "preferred_slot" not in data and "if_slot_taken" not in data:
        return None
    if "preferred_slot" in data or not partial:
        preferred, error = clean_preferred_slot(data.get("preferred_slot"), template.equipment)
        if error:
            return error
    elif template.preferred_weekday is not None and template.preferred_start_time is not None:
        preferred = {
            "weekday": template.preferred_weekday,
            "start_time": template.preferred_start_time,
            "slot_count": template.preferred_slot_count or 1,
            "slot_master_id": template.preferred_slot_master_id,
        }
    else:
        preferred = None
    if "if_slot_taken" in data or not partial:
        if_slot_taken, error = clean_if_slot_taken(data.get("if_slot_taken"))
        if error:
            return error
    else:
        if_slot_taken = template.if_slot_taken
    if preferred is None:
        # Without a preferred slot there is nothing to fall back from.
        if_slot_taken = "ask"
    return apply_preference_fields(template, preferred, if_slot_taken, data.get("auto_book_consent"))


_PREFERENCE_FIELDS = [
    "preferred_weekday",
    "preferred_start_time",
    "preferred_slot_count",
    "preferred_slot_master",
    "if_slot_taken",
    "if_slot_taken_consented_at",
]


def _name_taken(user, equipment_id, name, exclude_pk=None):
    qs = BookingInputTemplate.objects.filter(user=user, equipment_id=equipment_id, name__iexact=name)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def _name_taken_response(name):
    return Response(
        {"error": f'You already have a template named "{name}" for this equipment.'},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _serialize_one(user, template):
    from .template_health import check_template, peak_light

    return _serialize(
        template,
        labels=_field_labels(user, [template.equipment_id]),
        booking_block=template_booking_block(user, template.equipment),
        health=check_template(template, user, light=peak_light()),
    )


def _wants_health(request) -> bool:
    return str(request.query_params.get("health") or "").strip().lower() in ("1", "true", "yes")


def _owned_templates(user):
    return BookingInputTemplate.objects.filter(user=user).select_related(
        "equipment__internal_department", "equipment__equipment_group"
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def booking_templates(request):
    """GET: all of the user's templates (``?equipment=<id>`` for one equipment, ``?department=<id>`` for one
    department; ``?health=1`` adds whether each would book cleanly now). POST: create a template from just an
    equipment id, a name and the booking inputs."""
    user = request.user
    if request.method == "GET":
        qs = _owned_templates(user)
        for param, lookup in (("equipment", "equipment_id"), ("department", "equipment__internal_department_id")):
            raw = request.query_params.get(param)
            if raw:
                if not str(raw).isdigit():
                    return Response({"error": f"{param} must be an id."}, status=status.HTTP_400_BAD_REQUEST)
                qs = qs.filter(**{lookup: int(raw)})
        templates = list(qs)
        equipment_by_id = {t.equipment_id: t.equipment for t in templates}
        labels = _field_labels(user, equipment_by_id.keys())
        blocks = {pk: template_booking_block(user, eq) for pk, eq in equipment_by_id.items()}
        if _wants_health(request):
            from .template_health import check_template, peak_light

            light = peak_light()
            health = {t.pk: check_template(t, user, light=light) for t in templates}
        else:
            health = {}
        return Response(
            {
                "templates": [
                    _serialize(t, labels=labels, booking_block=blocks[t.equipment_id], health=health.get(t.pk))
                    for t in templates
                ]
            }
        )

    data = request.data if isinstance(request.data, dict) else {}
    equipment_id = data.get("equipment")
    if not str(equipment_id or "").isdigit():
        return Response({"error": "Choose the equipment for this template."}, status=status.HTTP_400_BAD_REQUEST)
    equipment = Equipment.objects.select_related("internal_department").filter(pk=int(equipment_id)).first()
    if equipment is None:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    block = template_booking_block(user, equipment)
    if block:
        return Response({"error": block, "code": "equipment_not_bookable"}, status=status.HTTP_403_FORBIDDEN)
    name, error = _clean_name(data.get("name"))
    if error:
        return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
    input_values, error = _clean_input_values(data.get("input_values"), equipment, user)
    if error:
        return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
    options, error = _clean_options(data.get("options"))
    if error:
        return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
    if BookingInputTemplate.objects.filter(user=user, equipment=equipment).count() >= MAX_TEMPLATES_PER_EQUIPMENT:
        return Response(
            {"error": f"You can keep up to {MAX_TEMPLATES_PER_EQUIPMENT} templates per equipment. Delete one first."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if _name_taken(user, equipment.pk, name):
        return _name_taken_response(name)
    template = BookingInputTemplate(user=user, equipment=equipment, name=name, input_values=input_values, options=options)
    error = _apply_preference(template, data, partial=False)
    if error:
        return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
    _normalise_template(template)
    try:
        with transaction.atomic():
            template.save()
    except IntegrityError:
        return _name_taken_response(name)
    return Response(_serialize_one(user, template), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "PUT", "DELETE"])
@permission_classes([IsAuthenticated])
def booking_template_detail(request, template_id):
    template = _owned_templates(request.user).filter(pk=template_id).first()
    if template is None:
        return Response({"error": "Template not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(_serialize_one(request.user, template))

    if request.method == "DELETE":
        template.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    data = request.data if isinstance(request.data, dict) else {}
    update_fields = []
    if "name" in data or request.method == "PUT":
        name, error = _clean_name(data.get("name"))
        if error:
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
        if _name_taken(request.user, template.equipment_id, name, exclude_pk=template.pk):
            return _name_taken_response(name)
        template.name = name
        update_fields.append("name")
    if "input_values" in data or request.method == "PUT":
        input_values, error = _clean_input_values(
            data.get("input_values"), template.equipment, request.user, baseline=template.input_values
        )
        if error:
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
        template.input_values = input_values
        update_fields.append("input_values")
    if "options" in data or request.method == "PUT":
        options, error = _clean_options(data.get("options"))
        if error:
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
        template.options = options
        update_fields.append("options")
    if "preferred_slot" in data or "if_slot_taken" in data or request.method == "PUT":
        error = _apply_preference(template, data, partial=request.method != "PUT")
        if error:
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
        update_fields.extend(_PREFERENCE_FIELDS)
    if update_fields:
        update_fields.extend(f for f in _normalise_template(template) if f not in update_fields)
        try:
            with transaction.atomic():
                template.save(update_fields=[*update_fields, "updated_at"])
        except IntegrityError:
            return _name_taken_response(template.name)
    return Response(_serialize_one(request.user, template))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_template_check(request):
    """Advice for a template being created or edited: the checks of a saved template, run on the draft.

    Body: ``equipment``, ``input_values``, ``options``, ``preferred_slot`` ({weekday, start_time, slot_count}).
    Nothing is saved.
    """
    from .template_health import check_values, parse_preferred

    data = request.data if isinstance(request.data, dict) else {}
    equipment_id = data.get("equipment")
    if not str(equipment_id or "").isdigit():
        return Response({"error": "Choose the equipment for this template."}, status=status.HTTP_400_BAD_REQUEST)
    equipment = (
        Equipment.objects.select_related("internal_department", "equipment_group").filter(pk=int(equipment_id)).first()
    )
    if equipment is None:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    input_values = data.get("input_values") or {}
    if not isinstance(input_values, dict):
        return Response({"error": "input_values must be an object."}, status=status.HTTP_400_BAD_REQUEST)
    if len(json.dumps(input_values)) > MAX_INPUT_VALUES_BYTES:
        return Response({"error": "The template's inputs are too large to check."}, status=status.HTTP_400_BAD_REQUEST)
    options, error = _clean_options(data.get("options"))
    if error:
        return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
    preferred = parse_preferred(data.get("preferred_slot"))
    return Response(check_values(request.user, equipment, input_values, options, preferred))


ATTENTION_CACHE_SECONDS = 600
ATTENTION_LIST_LIMIT = 10


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_template_attention(request):
    """How many of the user's templates would fail at booking time for a reason they can fix (dashboard notice)."""
    from django.core.cache import cache

    from .template_health import ERROR, UNFIXABLE_CODES, check_template, peak_light

    user = request.user
    light = peak_light()
    templates = list(_owned_templates(user))
    if not templates:
        return Response({"total": 0, "needs_attention": 0, "templates": []})
    stamp = max((t.updated_at.timestamp() if t.updated_at else 0) for t in templates)
    key = f"tplattention:v1:{user.pk}:{len(templates)}:{stamp}"
    cached = cache.get(key)
    if cached is not None:
        return Response(cached)
    items = []
    for template in templates:
        health = check_template(template, user, light=light)
        if not health.get("fixable_error_count"):
            continue
        first = next(i for i in health["issues"] if i["severity"] == ERROR and i["code"] not in UNFIXABLE_CODES)
        items.append({
            "id": template.pk,
            "name": template.name,
            "equipment": template.equipment_id,
            "equipment_name": getattr(template.equipment, "name", None),
            "equipment_code": getattr(template.equipment, "code", None),
            "issue": first["message"],
            "field": first.get("field"),
            "error_count": health["fixable_error_count"],
        })
    result = {"total": len(templates), "needs_attention": len(items), "templates": items[:ATTENTION_LIST_LIMIT]}
    cache.set(key, result, 60 if light else ATTENTION_CACHE_SECONDS)
    return Response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_template_preferred_slot(request, template_id):
    """Resolve the template's preferred weekday/time to slots in the user's open booking window.

    Read-only: the booking page pre-selects the returned slots and the user still clicks Book.
    ``?slot_count=N`` overrides the saved number of slots (the current inputs may need more or fewer).
    """
    template = (
        BookingInputTemplate.objects.select_related("equipment").filter(pk=template_id, user=request.user).first()
    )
    if template is None:
        return Response({"error": "Template not found."}, status=status.HTTP_404_NOT_FOUND)
    slot_count = None
    raw = request.query_params.get("slot_count")
    if raw not in (None, ""):
        if not str(raw).isdigit() or not 1 <= int(raw) <= MAX_PREFERRED_SLOT_COUNT:
            return Response(
                {"error": f"slot_count must be between 1 and {MAX_PREFERRED_SLOT_COUNT}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        slot_count = int(raw)
    return Response(resolve_preferred_slot(template, request.user, slot_count=slot_count))
