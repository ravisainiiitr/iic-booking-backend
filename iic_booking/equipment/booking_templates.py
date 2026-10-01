"""Named booking templates: a user's saved booking inputs and booking options for one equipment."""

import json

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import BookingInputTemplate, Equipment
from .template_slot_preference import (
    MAX_PREFERRED_SLOT_COUNT,
    apply_preference_fields,
    clean_if_slot_taken,
    clean_preferred_slot,
    resolve_preferred_slot,
    serialize_preference,
)

MAX_TEMPLATES_PER_EQUIPMENT = 25
MAX_NAME_LENGTH = 80
MAX_INPUT_VALUES_BYTES = 50_000
MAX_WORKSPACE_ID_LENGTH = 64
RESEARCH_WORKSPACE_KEY = "research_workspace"

OPTION_KEYS = (
    "auto_slot_selection",
    "book_any_available_slots",
    "book_even_if_single_slot_available",
    "waitlist_on_failure",
    "auto_allocate_alternative",
    "sample_return_after_analysis",
    "atmosphere_sensitive_sample",
)


def _serialize(template):
    equipment = template.equipment
    return {
        "id": template.pk,
        "equipment": template.equipment_id,
        "equipment_code": getattr(equipment, "code", None),
        "equipment_name": getattr(equipment, "name", None),
        "name": template.name,
        "input_values": template.input_values or {},
        "options": template.options or {},
        **serialize_preference(template),
        "created_at": template.created_at.isoformat() if template.created_at else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
    }


def _clean_name(raw):
    name = " ".join(str(raw or "").split())
    if not name:
        return None, "Give the template a name."
    if len(name) > MAX_NAME_LENGTH:
        return None, f"Template name must be at most {MAX_NAME_LENGTH} characters."
    return name, None


def _clean_input_values(raw):
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, "input_values must be an object."
    if len(json.dumps(raw)) > MAX_INPUT_VALUES_BYTES:
        return None, "The template's inputs are too large to save."
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


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def booking_templates(request):
    user = request.user
    if request.method == "GET":
        qs = BookingInputTemplate.objects.filter(user=user).select_related("equipment")
        equipment_param = request.query_params.get("equipment")
        if equipment_param:
            if not str(equipment_param).isdigit():
                return Response({"error": "equipment must be an equipment id."}, status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(equipment_id=int(equipment_param))
        return Response({"templates": [_serialize(t) for t in qs]})

    data = request.data if isinstance(request.data, dict) else {}
    equipment_id = data.get("equipment")
    if not str(equipment_id or "").isdigit():
        return Response({"error": "Choose the equipment for this template."}, status=status.HTTP_400_BAD_REQUEST)
    equipment = Equipment.objects.filter(pk=int(equipment_id)).first()
    if equipment is None:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    name, error = _clean_name(data.get("name"))
    if error:
        return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
    input_values, error = _clean_input_values(data.get("input_values"))
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
    try:
        with transaction.atomic():
            template.save()
    except IntegrityError:
        return _name_taken_response(name)
    return Response(_serialize(template), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "PUT", "DELETE"])
@permission_classes([IsAuthenticated])
def booking_template_detail(request, template_id):
    template = (
        BookingInputTemplate.objects.select_related("equipment").filter(pk=template_id, user=request.user).first()
    )
    if template is None:
        return Response({"error": "Template not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response(_serialize(template))

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
        input_values, error = _clean_input_values(data.get("input_values"))
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
        try:
            with transaction.atomic():
                template.save(update_fields=[*update_fields, "updated_at"])
        except IntegrityError:
            return _name_taken_response(template.name)
    return Response(_serialize(template))


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
