"""Uploaded portraits are made web-friendly once, on upload: at most 640 px on the longer side, JPEG."""

from __future__ import annotations

from io import BytesIO

from django.core.files.base import ContentFile

MAX_SIDE = 640


def optimised_portrait(upload, *, max_side: int = MAX_SIDE) -> ContentFile:
    """Return a resized JPEG copy of an uploaded image (orientation from EXIF applied). Raises ValueError when the
    file is not an image."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        upload.seek(0)
        img = Image.open(upload)
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("The file is not a supported image.") from exc
    img.thumbnail((max_side, max_side))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True, progressive=True)
    name = (getattr(upload, "name", "") or "photo").rsplit("/", 1)[-1].rsplit(".", 1)[0][:80] or "photo"
    return ContentFile(out.getvalue(), name=f"{name}.jpg")
