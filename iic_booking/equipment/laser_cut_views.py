"""API views for 2D laser cutting: sheet materials and DXF part analysis."""

import logging
import zipfile
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

from .fabrication import default_part_name, parse_quantity, resolve_own_material
from .fabrication_material_support import NO_MATERIALS_MESSAGE, bookable_material_or_none, bookable_materials
from .laser_cut_service import (
    MAX_OWN_SHEET_MM,
    UNIT_TO_MM,
    DxfParseError,
    analyze_dxf_bytes,
    bbox_to_mm,
    format_mm,
    part_fits_sheet,
    sheet_fit_error,
)
from .models import (
    Equipment,
    EquipmentProfileType,
    LaserCutAnalysis,
    LaserCutBatch,
    LaserSheetMaterial,
    PrintAnalysisBatchStatus,
    PrintAnalysisStatus,
)
from .print_3d_views import (
    presign_design_file,
    resolve_print_3d_user,
    stream_design_file,
    user_can_access_print_resource,
    user_may_download_design_file,
)
from .serializers import LaserCutAnalysisSerializer, LaserCutBatchSerializer, LaserSheetMaterialSerializer

logger = logging.getLogger(__name__)

# DXFs are parsed synchronously in the request, so the cap is lower than the STL limit.
MAX_DXF_BYTES = getattr(settings, "LASER_CUT_MAX_DXF_BYTES", 25 * 1024 * 1024)
MAX_ZIP_DXF_FILES = getattr(settings, "LASER_CUT_MAX_ZIP_DXF_FILES", 50)


def _mb(n: int) -> int:
    return n // (1024 * 1024)


def visible_laser_materials(equipment, user_type):
    """Sheets users can pick: supported by this laser cutter and enabled in the master list."""
    if equipment.profile_type != EquipmentProfileType.LASER_CUT_2D:
        return LaserSheetMaterial.objects.none()
    materials = bookable_materials(equipment)
    typed = materials.filter(user_type=user_type)
    if user_type and typed.exists():
        return typed
    return (materials.filter(user_type__isnull=True) | materials.filter(user_type="")).distinct()


def _request_user_type(request):
    user_type_param = (request.query_params.get("user_type") or "").strip().lower()
    if user_type_param:
        return user_type_param
    if request.user and request.user.is_authenticated:
        return request.user.user_type or UserType.STUDENT
    return UserType.STUDENT


@api_view(["GET"])
@permission_classes([AllowAny])
def equipment_laser_sheet_materials(request, pk):
    """List active sheet materials for a 2D laser cutter."""
    from .api_views import user_can_see_equipment, user_can_view_equipment_in_catalog

    try:
        equipment = Equipment.objects.get(pk=pk)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if not user_can_see_equipment(request.user, equipment) and not user_can_view_equipment_in_catalog(
        request.user, equipment
    ):
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)

    materials = visible_laser_materials(equipment, _request_user_type(request))
    return Response(
        {
            "materials": LaserSheetMaterialSerializer(materials, many=True).data,
            "no_materials_message": NO_MATERIALS_MESSAGE,
            "own_material_fixed_charge": (
                str(equipment.own_material_fixed_charge)
                if equipment.own_material_fixed_charge is not None
                else None
            ),
        }
    )


def _extract_dxf_entries_from_zip(upload) -> list[tuple[str, bytes]]:
    entries: list[tuple[str, bytes]] = []
    total_bytes = 0
    with zipfile.ZipFile(upload) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = (info.filename or "").replace("\\", "/")
            base = name.rsplit("/", 1)[-1]
            if not base or base.startswith(".") or "__MACOSX" in name:
                continue
            if not base.lower().endswith(".dxf"):
                continue
            if len(entries) >= MAX_ZIP_DXF_FILES:
                raise ValueError(f"A ZIP may contain at most {MAX_ZIP_DXF_FILES} DXF files.")
            total_bytes += int(info.file_size or 0)
            if total_bytes > MAX_DXF_BYTES:
                raise ValueError(f"Combined DXF size must be under {_mb(MAX_DXF_BYTES)} MB.")
            entries.append((base, zf.read(info)))
    if not entries:
        raise ValueError("The ZIP contains no .dxf files.")
    return entries


def _create_laser_analysis(*, equipment, user, batch, sequence, filename, data, material) -> LaserCutAnalysis:
    analysis = LaserCutAnalysis(
        equipment=equipment,
        user=user,
        batch=batch,
        sequence=sequence,
        original_filename=filename,
        part_name=default_part_name(filename),
        quantity=1,
        material=material,
        material_code_snapshot=material.code if material else "",
        sheet_rate_snapshot=material.sheet_rate if material else None,
        status=PrintAnalysisStatus.PROCESSING,
    )
    try:
        result = analyze_dxf_bytes(data)
    except DxfParseError as exc:
        analysis.status = PrintAnalysisStatus.FAILED
        analysis.error_message = str(exc)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected DXF parse failure for %s", filename)
        analysis.status = PrintAnalysisStatus.FAILED
        analysis.error_message = "This DXF could not be read. Please re-export it (DXF R12 or later) and try again."
    else:
        analysis.status = PrintAnalysisStatus.COMPLETED
        analysis.detected_units = result.detected_units
        analysis.units = result.units
        analysis.units_assumed = result.units_assumed
        analysis.bbox_drawing_units = result.bbox
        analysis.width_mm = result.width_mm
        analysis.height_mm = result.height_mm
        analysis.area_mm2 = result.area_mm2
        analysis.entity_count = result.entity_count
        analysis.cut_features = result.cut_features
        analysis.warnings = result.warnings
    analysis.dxf_file.save(filename, ContentFile(data), save=False)
    analysis.save()
    return analysis


def refresh_laser_batch_status(batch: LaserCutBatch) -> None:
    statuses = set(
        batch.items.filter(cancelled_at__isnull=True, superseded_at__isnull=True).values_list("status", flat=True)
    )
    if not statuses:
        new_status = PrintAnalysisBatchStatus.FAILED
    elif statuses == {PrintAnalysisStatus.COMPLETED}:
        new_status = PrintAnalysisBatchStatus.COMPLETED
    elif statuses == {PrintAnalysisStatus.FAILED}:
        new_status = PrintAnalysisBatchStatus.FAILED
    else:
        new_status = PrintAnalysisBatchStatus.PARTIAL
    if batch.status != new_status:
        batch.status = new_status
        batch.save(update_fields=["status", "updated_at"])


def add_dxf_upload_to_batch(*, equipment, user, upload, batch=None, material=None) -> LaserCutBatch:
    """Parse an uploaded .dxf/.zip into parts of ``batch`` (created when None). Raises ValueError for bad input."""
    filename_lower = (upload.name or "").lower()
    if filename_lower.endswith(".zip"):
        try:
            entries = _extract_dxf_entries_from_zip(upload)
        except zipfile.BadZipFile as exc:
            raise ValueError("Invalid ZIP file.") from exc
    elif filename_lower.endswith(".dxf"):
        entries = [(upload.name or "part.dxf", upload.read())]
    else:
        raise ValueError("Only .dxf or .zip files are supported.")

    with transaction.atomic():
        if batch is None:
            batch = LaserCutBatch.objects.create(
                equipment=equipment,
                user=user,
                original_filename=upload.name or "",
                status=PrintAnalysisBatchStatus.PROCESSING,
            )
        start = (
            batch.items.order_by("-sequence").values_list("sequence", flat=True).first()
        )
        next_seq = 0 if start is None else start + 1
        for offset, (name, data) in enumerate(entries):
            _create_laser_analysis(
                equipment=equipment,
                user=user,
                batch=batch,
                sequence=next_seq + offset,
                filename=name,
                data=data,
                material=material,
            )
        refresh_laser_batch_status(batch)
    return batch


@transaction.non_atomic_requests
@api_view(["POST"])
@permission_classes([AllowAny])
def equipment_analyze_dxf(request, pk):
    """
    Upload a DXF or a ZIP of DXFs. Form fields: file (required), batch_id (optional: add to an
    existing upload that is not booked yet), material_id (optional default sheet for new parts).
    """
    from .api_views import user_can_see_equipment

    try:
        equipment = Equipment.objects.get(pk=pk)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if not user_can_see_equipment(request.user, equipment):
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if equipment.profile_type != EquipmentProfileType.LASER_CUT_2D:
        return Response(
            {"error": "This equipment is not configured for 2D laser cutting."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    upload = request.FILES.get("file")
    if not upload:
        return Response({"error": "A DXF or ZIP file is required."}, status=status.HTTP_400_BAD_REQUEST)
    if upload.size > MAX_DXF_BYTES:
        return Response(
            {"error": f"File must be under {_mb(MAX_DXF_BYTES)} MB."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    laser_user = resolve_print_3d_user(request)
    batch = None
    batch_id = request.data.get("batch_id")
    if batch_id:
        batch = LaserCutBatch.objects.filter(pk=batch_id, equipment=equipment).first()
        if not batch or not user_can_access_print_resource(request, batch.user_id):
            return Response({"error": "Upload not found."}, status=status.HTTP_404_NOT_FOUND)
        if batch.booking_id:
            return Response(
                {"error": "These files are already booked. Use 'Replace files' on the booking instead."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    material = None
    material_id = request.data.get("material_id")
    if material_id:
        material = bookable_material_or_none(equipment, material_id)
        if not material:
            return Response({"error": "Invalid material_id."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        batch = add_dxf_upload_to_batch(
            equipment=equipment, user=laser_user, upload=upload, batch=batch, material=material
        )
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(LaserCutBatchSerializer(batch, context={"request": request}).data, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([AllowAny])
def laser_cut_batch_detail(request, batch_id):
    try:
        batch = LaserCutBatch.objects.get(pk=batch_id)
    except (LaserCutBatch.DoesNotExist, ValueError):
        return Response({"error": "Upload not found."}, status=status.HTTP_404_NOT_FOUND)
    if not user_can_access_print_resource(request, batch.user_id):
        return Response({"error": "Upload not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(LaserCutBatchSerializer(batch, context={"request": request}).data)


def _blank(value) -> bool:
    return value is None or str(value).strip() == ""


def clean_own_sheet_size(analysis: LaserCutAnalysis, raw_width, raw_height):
    """((width, height), None) for the user's own sheet, ((None, None), None) to go back to the size from the
    drawing, or ((None, None), error message)."""
    if _blank(raw_width) and _blank(raw_height):
        return (None, None), None
    try:
        width = Decimal(str(raw_width).strip())
        height = Decimal(str(raw_height).strip())
    except (InvalidOperation, TypeError, ValueError):
        return (None, None), "Enter the sheet width and height in mm."
    if not (width.is_finite() and height.is_finite()) or width <= 0 or height <= 0:
        return (None, None), "Enter the sheet width and height in mm."
    if width > MAX_OWN_SHEET_MM or height > MAX_OWN_SHEET_MM:
        return (None, None), f"The sheet can be at most {format_mm(MAX_OWN_SHEET_MM)} mm on each side."
    width = width.quantize(Decimal("0.1"), rounding=ROUND_CEILING)
    height = height.quantize(Decimal("0.1"), rounding=ROUND_CEILING)
    if analysis.width_mm is not None and analysis.height_mm is not None and not part_fits_sheet(
        analysis.width_mm, analysis.height_mm, width, height
    ):
        return (None, None), (
            f"The part is {format_mm(analysis.width_mm)} × {format_mm(analysis.height_mm)} mm, so your sheet "
            "must be at least that large."
        )
    return (width, height), None


def apply_laser_part_changes(analysis: LaserCutAnalysis, data, *, equipment, own_material=False) -> str | None:
    """Apply part_name / quantity / material_id / units / own sheet size from ``data``. Returns an error message or None.

    With ``own_material`` (already resolved against the equipment) the part is not checked against the sheet size."""
    update_fields = ["updated_at"]
    if "part_name" in data:
        analysis.part_name = str(data.get("part_name") or "").strip()[:255]
        update_fields.append("part_name")
    if "quantity" in data:
        qty = parse_quantity(data.get("quantity"))
        if qty is None:
            return "Number of parts must be a whole number of at least 1."
        analysis.quantity = qty
        update_fields.append("quantity")
    if "units" in data:
        units = str(data.get("units") or "").strip().lower()
        if units not in UNIT_TO_MM:
            return "Choose a unit: mm, cm, m, in or ft."
        if analysis.units != units:
            if analysis.detected_units and analysis.detected_units != "unitless":
                return "The drawing already defines its units; they cannot be changed."
            if not analysis.bbox_drawing_units:
                return "This part has no measured outline."
            analysis.units = units
            analysis.width_mm, analysis.height_mm, analysis.area_mm2 = bbox_to_mm(analysis.bbox_drawing_units, units)
            # The part changed size, so the own-sheet size goes back to the one worked out from the drawing.
            analysis.own_sheet_width_mm = analysis.own_sheet_height_mm = None
            update_fields += ["units", "width_mm", "height_mm", "area_mm2", "own_sheet_width_mm", "own_sheet_height_mm"]
    if "own_sheet_width_mm" in data or "own_sheet_height_mm" in data:
        sheet, err = clean_own_sheet_size(analysis, data.get("own_sheet_width_mm"), data.get("own_sheet_height_mm"))
        if err:
            return err
        analysis.own_sheet_width_mm, analysis.own_sheet_height_mm = sheet
        update_fields += ["own_sheet_width_mm", "own_sheet_height_mm"]
    if "material_id" in data:
        material_id = data.get("material_id")
        if material_id in (None, ""):
            analysis.material = None
            analysis.material_code_snapshot = ""
            analysis.sheet_rate_snapshot = None
        elif str(material_id) != str(analysis.material_id):
            # Re-sending a part's current sheet is allowed even if it was disabled or unsupported since.
            material = bookable_material_or_none(equipment, material_id)
            if not material:
                return "Choose one of the available sheet materials."
            analysis.material = material
            analysis.material_code_snapshot = material.code
            analysis.sheet_rate_snapshot = material.sheet_rate
        update_fields += ["material", "material_code_snapshot", "sheet_rate_snapshot"]
    # Booking re-checks the fit, so a name/quantity edit on a part whose sheet is too small is still allowed.
    fit_inputs_changed = "material_id" in data or "units" in data
    if (
        fit_inputs_changed
        and not own_material
        and analysis.material_id
        and analysis.status == PrintAnalysisStatus.COMPLETED
    ):
        err = sheet_fit_error(analysis.width_mm, analysis.height_mm, analysis.material)
        if err:
            return err
    analysis.save(update_fields=list(dict.fromkeys(update_fields)))
    return None


@api_view(["PATCH", "DELETE"])
@permission_classes([AllowAny])
def laser_cut_analysis_detail(request, analysis_id):
    """Edit (part name, quantity, sheet material, units for unitless drawings, own sheet size) or remove an
    unbooked part. ``own_sheet_width_mm`` / ``own_sheet_height_mm`` both blank go back to the size from the drawing."""
    try:
        analysis = LaserCutAnalysis.objects.select_related("equipment", "material", "batch").get(pk=analysis_id)
    except (LaserCutAnalysis.DoesNotExist, ValueError):
        return Response({"error": "Part not found."}, status=status.HTTP_404_NOT_FOUND)
    if not user_can_access_print_resource(request, analysis.user_id):
        return Response({"error": "Part not found."}, status=status.HTTP_404_NOT_FOUND)
    if analysis.booking_id or (analysis.batch and analysis.batch.booking_id):
        return Response(
            {"error": "This part is already booked. Use 'Replace files' on the booking instead."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if request.method == "DELETE":
        batch = analysis.batch
        analysis.dxf_file.delete(save=False)
        analysis.delete()
        if batch:
            refresh_laser_batch_status(batch)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # The booking form sends own_material while "I will bring my own sheet material" is ticked; booking re-checks.
    own_material = resolve_own_material(analysis.equipment, request.data.get("own_material"))
    err = apply_laser_part_changes(analysis, request.data, equipment=analysis.equipment, own_material=own_material)
    if err:
        return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
    analysis.refresh_from_db()
    return Response(LaserCutAnalysisSerializer(analysis, context={"request": request}).data)


def _get_downloadable_analysis(request, analysis_id):
    from .api_views import check_operator_permission

    try:
        analysis = LaserCutAnalysis.objects.select_related("booking").get(pk=analysis_id)
    except (LaserCutAnalysis.DoesNotExist, ValueError):
        return None
    if not user_may_download_design_file(request.user, analysis, check_operator_permission):
        return None
    return analysis


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def download_laser_cut_dxf(request, analysis_id):
    analysis = _get_downloadable_analysis(request, analysis_id)
    if analysis is None:
        return Response({"error": "Part not found."}, status=status.HTTP_404_NOT_FOUND)
    return stream_design_file(analysis.dxf_file, analysis.original_filename, "part.dxf")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def presign_laser_cut_dxf(request, analysis_id):
    analysis = _get_downloadable_analysis(request, analysis_id)
    if analysis is None:
        return Response({"error": "Part not found."}, status=status.HTTP_404_NOT_FOUND)
    return presign_design_file(
        analysis.dxf_file,
        analysis.original_filename,
        "part.dxf",
        fallback_url=f"/api/laser-cut-analyses/{analysis.id}/dxf/",
    )
