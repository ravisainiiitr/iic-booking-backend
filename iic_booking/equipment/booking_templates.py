"""Named booking templates: a user's saved booking inputs and booking options for one equipment."""

import json

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import BookingInputTemplate, Equipment

MAX_TEMPLATES_PER_EQUIPMENT = 25
MAX_NAME_LENGTH = 80
MAX_INPUT_VALUES_BYTES = 50_000

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
    return {key: raw[key] for key in OPTION_KEYS if isinstance(raw.get(key), bool)}, None


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
    try:
        with transaction.atomic():
            template = BookingInputTemplate.objects.create(
                user=user,
                equipment=equipment,
                name=name,
                input_values=input_values,
                options=options,
            )
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
    if update_fields:
        try:
            with transaction.atomic():
                template.save(update_fields=[*update_fields, "updated_at"])
        except IntegrityError:
            return _name_taken_response(template.name)
    return Response(_serialize(template))
