"""API views for 3D print STL analysis and materials."""

import io
import logging
import mimetypes
import threading
import zipfile

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import close_old_connections, transaction
from django.core.files.storage import default_storage
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from django.http import HttpResponse

from iic_booking.users.models.user_type import UserType

from .models import (
    Equipment,
    EquipmentProfileType,
    PrintAnalysis,
    PrintAnalysisBatch,
    PrintAnalysisBatchStatus,
    PrintAnalysisStatus,
    PrintMaterial,
)
from .fabrication import (
    QUANTITY_KEY,
    default_part_name,
    inject_print_parts,
    parse_quantity,
    prepare_new_job_quantity,
    stored_print_input_values,
    strip_fabrication_keys,
)
from .fabrication_material_support import NO_MATERIALS_MESSAGE, bookable_material_or_none, bookable_materials
from .print_3d_service import (
    analyze_stl_file,
    default_slicer_settings,
    recalculate_print_estimate,
)
from .print_size_limit import analyses_size_error, check_stl_files, print_size_limit_payload
from .serializers import (
    PrintAnalysisBatchSerializer,
    PrintAnalysisSerializer,
    PrintMaterialSerializer,
)

logger = logging.getLogger(__name__)

CHARGE_ESTIMATE_GUEST_EMAIL = "charge-estimate-guest@iic-booking.internal"


def get_charge_estimate_guest_user():
    """Shared system user for anonymous charge-estimate STL uploads."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.filter(email=CHARGE_ESTIMATE_GUEST_EMAIL).first()
    if user:
        return user
    return User.objects.create(
        email=CHARGE_ESTIMATE_GUEST_EMAIL,
        name="Charge Estimate Guest",
        user_type=UserType.STUDENT,
        is_active=True,
    )


def resolve_print_3d_user(request):
    if request.user and request.user.is_authenticated:
        return request.user
    return get_charge_estimate_guest_user()


def user_can_access_print_resource(request, owner_user_id: int) -> bool:
    if request.user and request.user.is_authenticated:
        if owner_user_id == request.user.id:
            return True
        ut = str(request.user.user_type or "").lower()
        if ut in (UserType.ADMIN, UserType.MANAGER, UserType.DEPT_ADMIN):
            return True
        return False
    return owner_user_id == get_charge_estimate_guest_user().id


def print_analysis_actor_owns(actor, owner_user_id: int) -> bool:
    """True if actor uploaded the analysis, or is staff booking on behalf of a user."""
    if actor is not None and getattr(actor, "is_authenticated", False):
        ut = str(getattr(actor, "user_type", "") or "").lower()
        if ut in (UserType.ADMIN, UserType.MANAGER, UserType.DEPT_ADMIN):
            return True
        return owner_user_id == actor.id
    return owner_user_id == get_charge_estimate_guest_user().id

MAX_STL_BYTES = getattr(settings, "PRINT_3D_MAX_STL_BYTES", 100 * 1024 * 1024)
MAX_ZIP_STL_FILES = getattr(settings, "PRINT_3D_MAX_ZIP_STL_FILES", 50)
FIXED_LAYER_HEIGHT_MM = 0.1
DEFAULT_DENSITY_PERCENT = 100.0
MIN_DENSITY_PERCENT = 20.0


def _parse_float_param(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_density_percent(value, default: float = DEFAULT_DENSITY_PERCENT) -> float:
    """Density (infill) is fixed between 20% and 100%."""
    density = _parse_float_param(value, default)
    return max(MIN_DENSITY_PERCENT, min(100.0, density))


def _slicer_settings_from_request(data) -> dict:
    layer_height = FIXED_LAYER_HEIGHT_MM
    density = _parse_density_percent(
        data.get("density_percent", data.get("infill_percent")),
        DEFAULT_DENSITY_PERCENT,
    )
    return default_slicer_settings(layer_height, density)


def _refresh_batch_status(batch: PrintAnalysisBatch) -> None:
    items = list(batch.items.all())
    if not items:
        batch.status = PrintAnalysisBatchStatus.FAILED
        batch.save(update_fields=["status", "updated_at"])
        return
    statuses = {item.status for item in items}
    if all(s == PrintAnalysisStatus.COMPLETED for s in statuses):
        batch.status = PrintAnalysisBatchStatus.COMPLETED
    elif any(s in (PrintAnalysisStatus.PENDING, PrintAnalysisStatus.PROCESSING) for s in statuses):
        batch.status = PrintAnalysisBatchStatus.PROCESSING
    elif all(s == PrintAnalysisStatus.FAILED for s in statuses):
        batch.status = PrintAnalysisBatchStatus.FAILED
    else:
        batch.status = PrintAnalysisBatchStatus.PARTIAL
    batch.save(update_fields=["status", "updated_at"])


def _extract_stl_entries_from_zip(upload) -> list[tuple[str, bytes]]:
    entries: list[tuple[str, bytes]] = []
    total_bytes = 0
    with zipfile.ZipFile(upload) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = (info.filename or "").replace("\\", "/")
            base = name.rsplit("/", 1)[-1]
            if not base or base.startswith("."):
                continue
            if "__MACOSX" in name or base.startswith("._"):
                continue
            if not base.lower().endswith(".stl"):
                continue
            if len(entries) >= MAX_ZIP_STL_FILES:
                raise ValueError(f"ZIP may contain at most {MAX_ZIP_STL_FILES} STL files.")
            data = zf.read(info)
            total_bytes += len(data)
            if total_bytes > MAX_STL_BYTES:
                raise ValueError(f"Combined STL size must be under {MAX_STL_BYTES // (1024 * 1024)} MB.")
            entries.append((base, data))
    if not entries:
        raise ValueError("ZIP contains no .stl files.")
    return entries


def _queue_print_analysis(analysis: PrintAnalysis):
    use_celery = getattr(settings, "PRINT_3D_USE_CELERY", True)
    if use_celery:
        try:
            from .tasks import run_print_analysis_task

            run_print_analysis_task.delay(str(analysis.id))
            return
        except Exception as exc:
            logger.warning("Celery unavailable for print analysis, running inline: %s", exc)

    from .tasks import run_print_analysis_impl

    async_inline = bool(getattr(settings, "PRINT_3D_ASYNC_INLINE", False))
    if async_inline:
        threading.Thread(target=run_print_analysis_impl, args=(str(analysis.id),), daemon=True).start()
        return

    run_print_analysis_impl(str(analysis.id))
    close_old_connections()


def _create_print_analysis(
    *,
    equipment,
    user,
    material,
    filename: str,
    file_bytes: bytes,
    slicer_settings: dict,
    batch=None,
    sequence: int = 0,
) -> PrintAnalysis:
    analysis = PrintAnalysis(
        equipment=equipment,
        user=user,
        material=material,
        batch=batch,
        sequence=sequence,
        original_filename=filename,
        part_name=default_part_name(filename),
        status=PrintAnalysisStatus.PENDING,
        slicer_settings=slicer_settings,
        material_code_snapshot=material.code if material else "",
        price_per_gram_snapshot=material.price_per_gram if material else None,
    )
    analysis.stl_file.save(filename, ContentFile(file_bytes), save=False)
    analysis.save()
    return analysis


def _print_size_rejection(equipment, files):
    """400 response listing every STL larger than the equipment's maximum print size, else None."""
    too_large, limit = check_stl_files(equipment, files)
    if not too_large:
        return None
    error = too_large[0]["message"]
    if len(too_large) > 1:
        error = f"{len(too_large)} STL files are larger than this printer's maximum print size. {error}"
    return Response(
        {
            "error": error,
            "code": "PRINT_SIZE_EXCEEDED",
            "too_large_files": too_large,
            "max_print_size": print_size_limit_payload(equipment),
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def equipment_print_materials(request, pk):
    """List the materials users can pick: supported by this 3D printer and enabled in the master list."""
    from .api_views import user_can_see_equipment, user_can_view_equipment_in_catalog
    try:
        equipment = Equipment.objects.get(pk=pk)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)

    if not user_can_see_equipment(request.user, equipment) and not user_can_view_equipment_in_catalog(
        request.user, equipment
    ):
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)

    user_type_param = (request.query_params.get("user_type") or "").strip().lower()
    if user_type_param:
        user_type = user_type_param
    elif request.user and request.user.is_authenticated:
        user_type = request.user.user_type or UserType.STUDENT
    else:
        user_type = UserType.STUDENT
    if equipment.profile_type != EquipmentProfileType.PRINT_3D:
        return Response({"materials": [], "no_materials_message": NO_MATERIALS_MESSAGE, "max_print_size": None})
    materials = bookable_materials(equipment)
    typed = materials.filter(user_type=user_type)
    if typed.exists():
        materials = typed
    else:
        materials = materials.filter(user_type__isnull=True) | materials.filter(user_type="")
    serializer = PrintMaterialSerializer(materials.distinct(), many=True)
    return Response(
        {
            "materials": serializer.data,
            "no_materials_message": NO_MATERIALS_MESSAGE,
            "max_print_size": print_size_limit_payload(equipment),
        }
    )


@transaction.non_atomic_requests
@api_view(["POST"])
@permission_classes([AllowAny])
def equipment_analyze_stl(request, pk):
    """
    Upload STL or ZIP (multiple STLs) for analysis.
    Form fields: file (required), material_id, density_percent (20–100, default 100).
    Layer height is fixed at 0.1 mm.
    """
    from .api_views import user_can_see_equipment
    try:
        equipment = Equipment.objects.get(pk=pk)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)

    if not user_can_see_equipment(request.user, equipment):
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)

    if equipment.profile_type != EquipmentProfileType.PRINT_3D:
        return Response(
            {"error": "This equipment is not configured for 3D printing."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    upload = request.FILES.get("file")
    if not upload:
        return Response({"error": "STL or ZIP file is required."}, status=status.HTTP_400_BAD_REQUEST)
    if upload.size > MAX_STL_BYTES:
        return Response(
            {"error": f"File must be under {MAX_STL_BYTES // (1024 * 1024)} MB."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    filename_lower = (upload.name or "").lower()
    is_zip = filename_lower.endswith(".zip")
    is_stl = filename_lower.endswith(".stl")
    if not is_zip and not is_stl:
        return Response(
            {"error": "Only .stl or .zip files are supported."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    material_id = request.data.get("material_id")
    material = None
    if material_id:
        material = bookable_material_or_none(equipment, material_id)
        if not material:
            return Response({"error": "Invalid material_id."}, status=status.HTTP_400_BAD_REQUEST)

    slicer_settings = _slicer_settings_from_request(request.data)
    print_user = resolve_print_3d_user(request)

    if is_stl:
        file_bytes = upload.read()
        too_large = _print_size_rejection(equipment, [(upload.name or "model.stl", file_bytes)])
        if too_large:
            return too_large
        analysis = _create_print_analysis(
            equipment=equipment,
            user=print_user,
            material=material,
            filename=upload.name or "model.stl",
            file_bytes=file_bytes,
            slicer_settings=slicer_settings,
        )
        _queue_print_analysis(analysis)
        async_inline = bool(getattr(settings, "PRINT_3D_ASYNC_INLINE", False))
        use_celery = getattr(settings, "PRINT_3D_USE_CELERY", True)
        if async_inline or use_celery:
            return Response(
                PrintAnalysisSerializer(analysis, context={"request": request}).data,
                status=status.HTTP_202_ACCEPTED,
            )
        analysis = PrintAnalysis.objects.select_related("equipment", "material").get(pk=analysis.pk)
        code = (
            status.HTTP_200_OK
            if analysis.status == PrintAnalysisStatus.COMPLETED
            else status.HTTP_500_INTERNAL_SERVER_ERROR
        )
        return Response(
            PrintAnalysisSerializer(analysis, context={"request": request}).data,
            status=code,
        )

    try:
        stl_entries = _extract_stl_entries_from_zip(upload)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except zipfile.BadZipFile:
        return Response({"error": "Invalid ZIP file."}, status=status.HTTP_400_BAD_REQUEST)
    too_large = _print_size_rejection(equipment, stl_entries)
    if too_large:
        return too_large

    batch = PrintAnalysisBatch.objects.create(
        equipment=equipment,
        user=print_user,
        material=material,
        original_filename=upload.name or "models.zip",
        status=PrintAnalysisBatchStatus.PENDING,
        slicer_settings=slicer_settings,
    )
    analyses = []
    for seq, (stl_name, stl_bytes) in enumerate(stl_entries):
        analyses.append(
            _create_print_analysis(
                equipment=equipment,
                user=print_user,
                material=material,
                filename=stl_name,
                file_bytes=stl_bytes,
                slicer_settings=slicer_settings,
                batch=batch,
                sequence=seq,
            )
        )
    batch.status = PrintAnalysisBatchStatus.PROCESSING
    batch.save(update_fields=["status", "updated_at"])
    for analysis in analyses:
        _queue_print_analysis(analysis)

    batch = PrintAnalysisBatch.objects.prefetch_related("items").get(pk=batch.pk)
    return Response(
        PrintAnalysisBatchSerializer(batch, context={"request": request}).data,
        status=status.HTTP_202_ACCEPTED,
    )


@api_view(["PATCH"])
@permission_classes([AllowAny])
def recalculate_print_analysis(request, analysis_id):
    """
    Fast re-quote when only material, layer height, or infill changes.
    Reuses stored mesh volume/surface metrics — does not re-upload or re-parse the STL.
    """
    try:
        analysis = PrintAnalysis.objects.select_related("equipment", "material").get(pk=analysis_id)
    except PrintAnalysis.DoesNotExist:
        return Response({"error": "Analysis not found."}, status=status.HTTP_404_NOT_FOUND)

    if not user_can_access_print_resource(request, analysis.user_id):
        return Response({"error": "Analysis not found."}, status=status.HTTP_404_NOT_FOUND)

    if analysis.status != PrintAnalysisStatus.COMPLETED:
        return Response(
            {"error": "Initial STL analysis must complete before recalculating settings."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if analysis.booking_id:
        return Response(
            {"error": "This analysis is already linked to a booking and cannot be changed."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if analysis.volume_cm3 is None:
        return Response(
            {"error": "Analysis has no mesh data to recalculate from."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    equipment = analysis.equipment
    if not equipment or equipment.profile_type != EquipmentProfileType.PRINT_3D:
        return Response(
            {"error": "This equipment is not configured for 3D printing."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    material_id = request.data.get("material_id")
    material = analysis.material
    if material_id:
        material = bookable_material_or_none(equipment, material_id)
        if not material:
            return Response({"error": "Invalid material_id."}, status=status.HTTP_400_BAD_REQUEST)

    layer_height = FIXED_LAYER_HEIGHT_MM
    infill = _parse_density_percent(
        request.data.get("density_percent", request.data.get("infill_percent")),
        DEFAULT_DENSITY_PERCENT,
    )
    slicer_settings = default_slicer_settings(layer_height, infill)

    try:
        estimate = recalculate_print_estimate(
            analysis,
            material=material,
            layer_height_mm=layer_height,
            infill_percent=infill,
        )
    except Exception as exc:
        logger.exception("Print settings recalculation failed for %s", analysis_id)
        return Response(
            {"error": f"Recalculation failed: {exc}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    analysis.material = material
    analysis.slicer_settings = slicer_settings
    analysis.material_code_snapshot = material.code if material else ""
    analysis.price_per_gram_snapshot = material.price_per_gram if material else None
    analysis.weight_grams = estimate.weight_grams
    analysis.volume_cm3 = estimate.volume_cm3
    analysis.estimated_time_minutes = estimate.estimated_time_minutes
    analysis.bounding_box = estimate.bounding_box
    analysis.analysis_method = estimate.analysis_method
    analysis.save(
        update_fields=[
            "material",
            "slicer_settings",
            "material_code_snapshot",
            "price_per_gram_snapshot",
            "weight_grams",
            "volume_cm3",
            "estimated_time_minutes",
            "bounding_box",
            "analysis_method",
            "updated_at",
        ]
    )

    return Response(PrintAnalysisSerializer(analysis).data)


@api_view(["GET"])
@permission_classes([AllowAny])
def print_analysis_detail(request, analysis_id):
    """Poll analysis status/result."""
    try:
        analysis = PrintAnalysis.objects.select_related("equipment", "material").get(pk=analysis_id)
    except PrintAnalysis.DoesNotExist:
        return Response({"error": "Analysis not found."}, status=status.HTTP_404_NOT_FOUND)

    if not user_can_access_print_resource(request, analysis.user_id):
        return Response({"error": "Analysis not found."}, status=status.HTTP_404_NOT_FOUND)

    return Response(PrintAnalysisSerializer(analysis, context={"request": request}).data)


@api_view(["PATCH"])
@permission_classes([AllowAny])
def update_print_analysis_part(request, analysis_id):
    """Set part name and/or number of copies for an uploaded STL before it is booked."""
    try:
        analysis = PrintAnalysis.objects.select_related("equipment", "material").get(pk=analysis_id)
    except PrintAnalysis.DoesNotExist:
        return Response({"error": "Analysis not found."}, status=status.HTTP_404_NOT_FOUND)
    if not user_can_access_print_resource(request, analysis.user_id):
        return Response({"error": "Analysis not found."}, status=status.HTTP_404_NOT_FOUND)
    if analysis.booking_id:
        return Response(
            {"error": "This file is already linked to a booking. Use 'Replace files' on the booking instead."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    update_fields = ["updated_at"]
    if "part_name" in request.data:
        analysis.part_name = str(request.data.get("part_name") or "").strip()[:255]
        update_fields.append("part_name")
    if "quantity" in request.data:
        qty = parse_quantity(request.data.get("quantity"))
        if qty is None:
            return Response(
                {"error": "Number of parts must be a whole number of at least 1."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        analysis.quantity = qty
        update_fields.append("quantity")
    analysis.save(update_fields=update_fields)
    return Response(PrintAnalysisSerializer(analysis, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([AllowAny])
def print_analysis_batch_detail(request, batch_id):
    """Poll batch status/results for ZIP uploads."""
    try:
        batch = PrintAnalysisBatch.objects.prefetch_related("items").get(pk=batch_id)
    except PrintAnalysisBatch.DoesNotExist:
        return Response({"error": "Batch not found."}, status=status.HTTP_404_NOT_FOUND)

    if not user_can_access_print_resource(request, batch.user_id):
        return Response({"error": "Batch not found."}, status=status.HTTP_404_NOT_FOUND)

    _refresh_batch_status(batch)
    batch = PrintAnalysisBatch.objects.prefetch_related("items").get(pk=batch_id)
    return Response(PrintAnalysisBatchSerializer(batch, context={"request": request}).data)


@api_view(["PATCH"])
@permission_classes([AllowAny])
def recalculate_print_analysis_batch(request, batch_id):
    """Fast re-quote for all items in a ZIP batch when material or density changes."""
    try:
        batch = PrintAnalysisBatch.objects.prefetch_related("items").get(pk=batch_id)
    except PrintAnalysisBatch.DoesNotExist:
        return Response({"error": "Batch not found."}, status=status.HTTP_404_NOT_FOUND)

    if not user_can_access_print_resource(request, batch.user_id):
        return Response({"error": "Batch not found."}, status=status.HTTP_404_NOT_FOUND)
    if batch.booking_id:
        return Response(
            {"error": "This batch is already linked to a booking and cannot be changed."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    equipment = batch.equipment
    if not equipment or equipment.profile_type != EquipmentProfileType.PRINT_3D:
        return Response(
            {"error": "This equipment is not configured for 3D printing."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    material_id = request.data.get("material_id")
    material = batch.material
    if material_id:
        material = bookable_material_or_none(equipment, material_id)
        if not material:
            return Response({"error": "Invalid material_id."}, status=status.HTTP_400_BAD_REQUEST)

    layer_height = FIXED_LAYER_HEIGHT_MM
    infill = _parse_density_percent(
        request.data.get("density_percent", request.data.get("infill_percent")),
        DEFAULT_DENSITY_PERCENT,
    )
    slicer_settings = default_slicer_settings(layer_height, infill)

    items = list(batch.items.filter(cancelled_at__isnull=True).order_by("sequence", "created_at"))
    if not items:
        return Response({"error": "Batch has no active STL files."}, status=status.HTTP_400_BAD_REQUEST)

    for analysis in items:
        if analysis.status != PrintAnalysisStatus.COMPLETED:
            return Response(
                {"error": f"Analysis for {analysis.original_filename} is not complete yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if analysis.volume_cm3 is None:
            return Response(
                {"error": f"Analysis for {analysis.original_filename} has no mesh data."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            estimate = recalculate_print_estimate(
                analysis,
                material=material,
                layer_height_mm=layer_height,
                infill_percent=infill,
            )
        except Exception as exc:
            logger.exception("Batch recalculation failed for %s", analysis.id)
            return Response(
                {"error": f"Recalculation failed for {analysis.original_filename}: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        analysis.material = material
        analysis.slicer_settings = slicer_settings
        analysis.material_code_snapshot = material.code if material else ""
        analysis.price_per_gram_snapshot = material.price_per_gram if material else None
        analysis.weight_grams = estimate.weight_grams
        analysis.volume_cm3 = estimate.volume_cm3
        analysis.estimated_time_minutes = estimate.estimated_time_minutes
        analysis.bounding_box = estimate.bounding_box
        analysis.analysis_method = estimate.analysis_method
        analysis.save(
            update_fields=[
                "material",
                "slicer_settings",
                "material_code_snapshot",
                "price_per_gram_snapshot",
                "weight_grams",
                "volume_cm3",
                "estimated_time_minutes",
                "bounding_box",
                "analysis_method",
                "updated_at",
            ]
        )

    batch.material = material
    batch.slicer_settings = slicer_settings
    batch.save(update_fields=["material", "slicer_settings", "updated_at"])
    batch = PrintAnalysisBatch.objects.prefetch_related("items").get(pk=batch_id)
    return Response(PrintAnalysisBatchSerializer(batch, context={"request": request}).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def download_print_analysis_stl(request, analysis_id):
    """
    Download STL file for a print analysis.
    Allowed: analysis creator OR operator/manager/admin (lab staff).
    Streams via Django so it works with S3 (signed URLs) and avoids CORS.
    """
    from .api_views import check_operator_permission

    try:
        analysis = PrintAnalysis.objects.select_related("equipment", "material", "user", "booking").get(
            pk=analysis_id
        )
    except PrintAnalysis.DoesNotExist:
        return Response({"error": "Analysis not found."}, status=status.HTTP_404_NOT_FOUND)

    if not user_may_download_design_file(request.user, analysis, check_operator_permission):
        return Response({"error": "Analysis not found."}, status=status.HTTP_404_NOT_FOUND)

    return stream_design_file(analysis.stl_file, analysis.original_filename, "model.stl")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def presign_print_analysis_stl(request, analysis_id):
    """
    Return a presigned URL for the STL file so browser can download immediately.
    Allowed: analysis creator OR operator/manager/admin (lab staff).
    """
    from .api_views import check_operator_permission

    try:
        analysis = PrintAnalysis.objects.select_related("equipment", "material", "user", "booking").get(
            pk=analysis_id
        )
    except PrintAnalysis.DoesNotExist:
        return Response({"error": "Analysis not found."}, status=status.HTTP_404_NOT_FOUND)

    if not user_may_download_design_file(request.user, analysis, check_operator_permission):
        return Response({"error": "Analysis not found."}, status=status.HTTP_404_NOT_FOUND)

    return presign_design_file(
        analysis.stl_file,
        analysis.original_filename,
        "model.stl",
        fallback_url=f"/api/print-analyses/{analysis.id}/stl/",
    )


def user_may_download_design_file(user, analysis, check_operator_permission) -> bool:
    if check_operator_permission(user):
        return True
    if analysis.user_id == user.id:
        return True
    booking = getattr(analysis, "booking", None)
    return bool(booking and booking.user_id == user.id)


def _resolve_storage_name(file_field):
    if not file_field or not file_field.name:
        return None, None
    storage = getattr(file_field, "storage", None) or default_storage
    file_name = file_field.name

    def _exists(name: str) -> bool:
        try:
            return bool(name) and storage.exists(name)
        except Exception:
            return False

    # Handle historical key differences (sometimes stored with/without leading "media/").
    candidate_names = [file_name]
    if file_name.startswith("media/"):
        candidate_names.append(file_name[len("media/"):])
    else:
        candidate_names.append(f"media/{file_name}")
    resolved = next((n for n in candidate_names if _exists(n)), None)
    if not resolved:
        logger.warning("Design file not found in storage. Tried: %s", candidate_names)
    return storage, resolved


def _safe_download_name(original_filename, default_name):
    return (original_filename or default_name).replace('"', "").replace("\r", "").replace("\n", "")


def stream_design_file(file_field, original_filename, default_name):
    """Stream an uploaded STL/DXF through Django (works with S3 and avoids CORS)."""
    if not file_field or not file_field.name:
        return Response({"error": "No file available."}, status=status.HTTP_404_NOT_FOUND)
    storage, resolved_name = _resolve_storage_name(file_field)
    if not resolved_name:
        return Response({"error": "File not found in storage."}, status=status.HTTP_404_NOT_FOUND)
    try:
        with storage.open(resolved_name, "rb") as fh:
            content = fh.read()
    except Exception as e:
        logger.exception("Error opening design file %s: %s", resolved_name, e)
        return Response({"error": "File not available."}, status=status.HTTP_404_NOT_FOUND)

    content_type, _ = mimetypes.guess_type(original_filename or resolved_name)
    if not content_type:
        content_type = "application/octet-stream"
    response = HttpResponse(content, content_type=content_type)
    safe_name = _safe_download_name(original_filename, default_name)
    response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def presigned_design_file_url(file_field, original_filename, default_name, *, expires_in=None):
    """Presigned S3 GET URL, or None when storage is not S3 or the object is missing."""
    from iic_booking.common_download import _boto3_presign, _resolve_s3_object_key

    storage_backend = (
        getattr(settings, "STORAGES", {}).get("default", {}).get("BACKEND", "")
        if hasattr(settings, "STORAGES")
        else ""
    )
    bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
    if "s3" not in (storage_backend or "").lower() or not bucket:
        return None
    storage, name = _resolve_storage_name(file_field)
    if not name:
        return None
    # The storage name is relative to the storage location (e.g. "media/"); the bucket key is not.
    url = _boto3_presign(
        bucket=getattr(storage, "bucket_name", None) or bucket,
        key=_resolve_s3_object_key(storage, name),
        download_name=_safe_download_name(original_filename, default_name),
        expires_in=int(expires_in or getattr(settings, "AWS_S3_QUERYSTRING_EXPIRE", 3600)),
        use_accelerate=False,
    )
    if not url:
        logger.error("Failed to generate presigned url for %s", file_field.name)
    return url


def presign_design_file(file_field, original_filename, default_name, *, fallback_url):
    if not file_field or not file_field.name:
        return Response({"error": "No file available."}, status=status.HTTP_404_NOT_FOUND)
    storage_backend = (
        getattr(settings, "STORAGES", {}).get("default", {}).get("BACKEND", "")
        if hasattr(settings, "STORAGES")
        else ""
    )
    if "s3" not in (storage_backend or "").lower() or not getattr(settings, "AWS_STORAGE_BUCKET_NAME", None):
        return Response({"url": fallback_url}, status=status.HTTP_200_OK)
    _storage, key = _resolve_storage_name(file_field)
    if not key:
        return Response({"error": "File not found in storage."}, status=status.HTTP_404_NOT_FOUND)
    url = presigned_design_file_url(file_field, original_filename, default_name)
    if not url:
        return Response({"error": "Failed to generate download URL."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return Response({"url": url}, status=status.HTTP_200_OK)


def _aggregate_print_analyses(analyses):
    total_weight = 0
    total_time = 0
    material_code = ""
    for analysis in analyses:
        weight, time_min = get_effective_print_weight_and_time_from_analysis(analysis)
        total_weight += int(weight or 0)
        total_time += int(time_min or 0)
        if not material_code:
            material_code = analysis.material_code_snapshot or (
                analysis.material.code if analysis.material else ""
            )
    return total_weight, total_time, material_code


def print_material_error(equipment, analyses) -> str | None:
    """New bookings and replaced files may only use materials the equipment supports and that are enabled."""
    bookable = {m.pk: m.code for m in bookable_materials(equipment)}
    codes = {(c or "").lower() for c in bookable.values()}
    for a in analyses:
        if a.material_id is None:
            if a.material_code_snapshot and a.material_code_snapshot.lower() in codes:
                continue
            return f"{a.display_part_name}: choose a material."
        if a.material_id not in bookable:
            return (
                f"{a.display_part_name}: the selected material is no longer available on this equipment. "
                "Choose another one."
            )
    return None


def merge_print_booking_into_input_values(
    equipment,
    input_values,
    user,
    *,
    print_analysis_id=None,
    print_analysis_batch_id=None,
):
    """Validate print analysis/batch and inject aggregated A/B/C for PRINT_3D bookings."""
    if equipment.profile_type != EquipmentProfileType.PRINT_3D:
        return input_values, None

    if print_analysis_batch_id:
        try:
            batch = PrintAnalysisBatch.objects.prefetch_related("items__material").get(
                pk=print_analysis_batch_id
            )
        except PrintAnalysisBatch.DoesNotExist:
            return input_values, "Invalid print_analysis_batch_id."

        if batch.equipment_id != equipment.equipment_id:
            return input_values, "Batch does not belong to this equipment."
        if not print_analysis_actor_owns(user, batch.user_id):
            return input_values, "Batch was created by another user."
        if batch.booking_id:
            return input_values, "This batch is already linked to a booking."

        items = list(
            batch.items.filter(cancelled_at__isnull=True).order_by("sequence", "created_at")
        )
        if not items:
            return input_values, "Batch has no active STL files."
        for item in items:
            if item.status != PrintAnalysisStatus.COMPLETED:
                return input_values, f"STL analysis for {item.original_filename} is not complete yet."
            if item.booking_id:
                return input_values, f"{item.original_filename} is already linked to a booking."

        total_weight, total_time, material_code = _aggregate_print_analyses(items)
        if not material_code:
            return input_values, "Batch has no material selected."
        if total_weight <= 0 or total_time <= 0:
            return input_values, "Batch has invalid weight or time estimates."
        err = print_material_error(equipment, items) or analyses_size_error(equipment, items)
        if err:
            return input_values, err

        return _inject_new_print_job(input_values, items)

    if print_analysis_id:
        return merge_print_analysis_into_input_values(
            equipment, print_analysis_id, input_values, user
        )

    return input_values, "print_analysis_id or print_analysis_batch_id is required for 3D print bookings."


def merge_print_analysis_into_input_values(equipment, analysis_id, input_values, user):
    """Validate single analysis and inject A/B/C for PRINT_3D bookings."""
    if equipment.profile_type != EquipmentProfileType.PRINT_3D:
        return input_values, None

    if not analysis_id:
        return input_values, "print_analysis_id is required for 3D print bookings."

    try:
        analysis = PrintAnalysis.objects.select_related("material").get(pk=analysis_id)
    except PrintAnalysis.DoesNotExist:
        return input_values, "Invalid print_analysis_id."

    if analysis.equipment_id != equipment.equipment_id:
        return input_values, "Analysis does not belong to this equipment."
    if not print_analysis_actor_owns(user, analysis.user_id):
        return input_values, "Analysis was created by another user."
    if analysis.status != PrintAnalysisStatus.COMPLETED:
        return input_values, "STL analysis is not complete yet."
    if analysis.booking_id:
        return input_values, "This STL analysis is already linked to a booking."
    if analysis.cancelled_at:
        return input_values, "This STL analysis is no longer active."

    material_code = analysis.material_code_snapshot or (analysis.material.code if analysis.material else "")
    if not material_code:
        return input_values, "Analysis has no material selected."
    err = print_material_error(equipment, [analysis]) or analyses_size_error(equipment, [analysis])
    if err:
        return input_values, err

    return _inject_new_print_job(input_values, [analysis])


def _inject_new_print_job(input_values, analyses):
    merged, err = prepare_new_job_quantity(EquipmentProfileType.PRINT_3D, strip_fabrication_keys(input_values))
    if err:
        return input_values, err
    return inject_print_parts(merged, analyses, merged[QUANTITY_KEY]), None


def link_print_analyses_to_booking(
    booking,
    *,
    print_analysis_obj=None,
    print_analysis_batch_obj=None,
):
    """Attach STL analysis records to a newly created booking."""
    if print_analysis_batch_obj:
        booking.print_analysis_batch = print_analysis_batch_obj
        booking.save(update_fields=["print_analysis_batch", "updated_at"])
        print_analysis_batch_obj.booking = booking
        print_analysis_batch_obj.save(update_fields=["booking", "updated_at"])
        items = list(
            PrintAnalysis.objects.filter(batch=print_analysis_batch_obj, cancelled_at__isnull=True)
            .order_by("sequence", "created_at")
        )
        PrintAnalysis.objects.filter(batch=print_analysis_batch_obj).update(booking=booking)
        if not print_analysis_obj and items:
            print_analysis_obj = items[0]
    if print_analysis_obj and not print_analysis_obj.booking_id:
        booking.print_analysis = print_analysis_obj
        booking.save(update_fields=["print_analysis", "updated_at"])
        print_analysis_obj.booking = booking
        print_analysis_obj.save(update_fields=["booking", "updated_at"])


def get_effective_print_weight_and_time_from_analysis(analysis):
    """Prefer actual post-print values when set; otherwise use STL estimate."""
    from .print_3d_service import ceil_weight_grams

    if not analysis:
        return None, None
    weight = (
        analysis.actual_weight_grams
        if analysis.actual_weight_grams is not None
        else analysis.weight_grams
    )
    if weight is not None:
        weight = ceil_weight_grams(weight)
    time_min = (
        analysis.actual_time_minutes
        if analysis.actual_time_minutes is not None
        else analysis.estimated_time_minutes
    )
    return weight, time_min


def apply_print_analysis_to_input_values(booking, input_values):
    """Inject per-part fabrication values (A/B/C and reserved keys) from the booking's linked files.

    Covers 3D printing and 2D laser cutting; other profiles are returned unchanged.
    """
    from .fabrication import apply_fabrication_to_input_values

    return apply_fabrication_to_input_values(booking, input_values)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def update_booking_print_actuals(request, booking_id):
    """
    Admin/OIC/operator: set post-print actual weight (g) and time (min) of one STL file of a 3D print booking
    (``analysis_id``; defaults to the booking's main file).

    The charge is always re-priced from the actuals with the same engine and GST rule as the estimate, through
    the post-booking charge recalculation: a higher charge becomes an extra amount collected with Pay Now /
    Deduct Money, a lower one a refund the Officer In Charge confirms. Allowed while the booking is Booked, and
    after completion for the equipment's Officer In Charge or an administrator.
    """
    from .api_views import (
        _calculate_input_values_charge,
        _is_admin_user,
        _oic_booking_scope_denied,
        _recalculate_booking_charge_and_adjust_wallet,
        _user_can_act_as_oic_for_equipment,
        check_operator_permission,
    )
    from .fabrication import active_print_analyses_for_booking
    from .models import Booking, BookingStatus, EquipmentProfileType
    from .serializers import BookingSerializer

    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only Admin or Officer In Charge can update print actuals."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        booking = Booking.objects.select_related(
            "equipment", "charge_profile", "print_analysis", "print_analysis__material"
        ).get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    denied = _oic_booking_scope_denied(request.user, booking)
    if denied is not None:
        return denied

    if booking.source_booking_id is not None:
        return Response(
            {"error": "Actuals cannot be modified for repeat sample bookings."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    equipment = booking.equipment
    if not equipment or equipment.profile_type != EquipmentProfileType.PRINT_3D:
        return Response(
            {"error": "This booking is not a 3D print booking."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    analyses = active_print_analyses_for_booking(booking)
    analysis_id = str(request.data.get("analysis_id") or "").strip()
    if analysis_id:
        analysis = next((a for a in analyses if str(a.id) == analysis_id), None)
        if analysis is None:
            return Response(
                {"error": "This STL file is not part of the booking."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        main = booking.print_analysis
        analysis = next((a for a in analyses if main and a.id == main.id), None) or (analyses[0] if analyses else None)
    if not analysis:
        return Response(
            {"error": "No STL analysis linked to this booking."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    is_charge_manager = _user_can_act_as_oic_for_equipment(request.user, equipment) or _is_admin_user(request.user)
    if booking.status == BookingStatus.COMPLETED and not is_charge_manager:
        return Response(
            {
                "error": "After the booking is completed, only the Officer In Charge can set the actual weight and "
                "time, because it changes the charge."
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    if booking.status not in (BookingStatus.BOOKED, BookingStatus.COMPLETED):
        return Response(
            {"error": "Actual weight and time can only be set on Booked or Completed bookings."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    weight_raw = request.data.get("actual_weight_grams")
    time_raw = request.data.get("actual_time_minutes")
    if weight_raw is None and time_raw is None:
        return Response(
            {"error": "Provide actual_weight_grams and/or actual_time_minutes."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    weight_val = None
    if weight_raw is not None:
        try:
            weight_val = float(weight_raw)
        except (TypeError, ValueError):
            return Response(
                {"error": "actual_weight_grams must be a number."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if weight_val <= 0:
            return Response(
                {"error": "actual_weight_grams must be greater than 0."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    time_val = None
    if time_raw is not None:
        try:
            time_val = int(time_raw)
        except (TypeError, ValueError):
            return Response(
                {"error": "actual_time_minutes must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if time_val <= 0:
            return Response(
                {"error": "actual_time_minutes must be greater than 0."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    had_actuals = analysis.actual_weight_grams is not None or analysis.actual_time_minutes is not None
    try:
        charge_before = _calculate_input_values_charge(booking, booking.input_values) if booking.charge_profile_id else None
    except Exception:
        logger.exception("Could not price booking %s before print actuals update", booking.pk)
        charge_before = None

    summary = None
    try:
        with transaction.atomic():
            update_fields = ["updated_at"]
            if weight_val is not None:
                from .print_3d_service import ceil_weight_grams

                analysis.actual_weight_grams = ceil_weight_grams(weight_val)
                update_fields.append("actual_weight_grams")
            if time_val is not None:
                analysis.actual_time_minutes = time_val
                update_fields.append("actual_time_minutes")
            analysis.save(update_fields=update_fields)

            booking.input_values = stored_print_input_values(booking, active_print_analyses_for_booking(booking))
            booking.save(update_fields=["input_values", "updated_at"])

            # Actuals are a pricing input: re-price whenever they change the charge. Re-saving actuals that are
            # already set also re-prices, so a charge that was never adjusted can still be corrected.
            needs_recalc = bool(booking.charge_profile_id) and (
                getattr(equipment, "enable_charge_recalculation", False)
                or had_actuals
                or charge_before is None
                or _calculate_input_values_charge(booking, booking.input_values) != charge_before
            )
            if needs_recalc:
                summary = _recalculate_booking_charge_and_adjust_wallet(request, booking)
    except Exception:
        logger.exception("Charge recalculation failed after print actuals update for booking %s", booking.pk)
        return Response(
            {"error": "The charge could not be recalculated, so the actual weight and time were not saved. Please try again."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    booking.refresh_from_db()
    analysis.refresh_from_db()
    message = "Actual weight and time saved."
    if summary:
        if summary.get("refund_amount"):
            message += (
                f" The charge went down from ₹{summary['previous_charge']} to ₹{summary['new_charge']}; the refund of "
                f"₹{summary['refund_amount']} is waiting for the Officer In Charge's confirmation."
            )
        elif summary.get("extra_amount"):
            message += (
                f" The charge went up from ₹{summary['previous_charge']} to ₹{summary['new_charge']}; the extra "
                f"₹{summary['extra_amount']} is to be paid."
            )
        else:
            message += " The charge is unchanged."
    return Response(
        {
            "message": message,
            "booking": BookingSerializer(booking, context={"request": request}).data,
            "print_analysis": PrintAnalysisSerializer(analysis, context={"request": request}).data,
            "charge_recalculation_summary": summary,
        },
        status=status.HTTP_200_OK,
    )
