from django.http import HttpResponse, HttpResponseNotFound
from django.views.decorators.http import require_GET

from .image_utils import get_equipment_image_storage_path, open_equipment_image_bytes
from .models import Equipment


@require_GET
def serve_equipment_image(request, pk):
    """
    Stream equipment image from storage for Django admin preview.
    Staff-only; uses the same session as the admin, so the image stays visible.
    """
    from django.contrib.admin.views.decorators import staff_member_required

    if not getattr(request.user, "is_authenticated", False) or not getattr(request.user, "is_staff", False):
        return HttpResponseNotFound("Not found.")

    try:
        obj = Equipment.objects.get(pk=pk)
    except (Equipment.DoesNotExist, ValueError):
        return HttpResponseNotFound("Equipment not found.")
    stored_path = get_equipment_image_storage_path(obj)
    if not stored_path:
        return HttpResponseNotFound("No image for this equipment.")
    content, resolved_path, content_type = open_equipment_image_bytes(obj)
    if not content or not resolved_path:
        return HttpResponseNotFound("Image not available.")

    response = HttpResponse(content, content_type=content_type or "image/jpeg")
    response["Cache-Control"] = "private, max-age=3600"
    response["Content-Length"] = len(content)
    return response
