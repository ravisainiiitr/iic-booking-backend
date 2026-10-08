"""Turn an export ``Document`` into a download response, enforcing row caps."""

from __future__ import annotations

from django.http import HttpResponse

from . import spec
from .render_csv import render_csv
from .render_pdf import render_pdf
from .render_xlsx import render_xlsx
from .values import now_ist

EXPORT_ROW_LIMIT = 10_000
PDF_ROW_LIMIT = 2_000
FORMATS = ("xlsx", "csv", "pdf")
CONTENT_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
    "pdf": "application/pdf",
}


class ExportError(Exception):
    """A user-facing reason the export could not be produced (HTTP 400 unless ``status`` says otherwise)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class ExportTooLarge(ExportError):
    pass


def parse_format(request) -> str:
    fmt = (request.query_params.get("export_format") or "xlsx").strip().lower()
    if fmt not in FORMATS:
        raise ExportError("export_format must be xlsx, csv or pdf.")
    return fmt


def check_row_limit(count: int, *, noun: str = "rows") -> None:
    if count > EXPORT_ROW_LIMIT:
        raise ExportTooLarge(
            f"More than {EXPORT_ROW_LIMIT:,} {noun} match these filters; exports are limited to "
            f"{EXPORT_ROW_LIMIT:,}. Narrow the filters (for example a date range) and try again.",
        )


def check_pdf_limit(count: int, *, noun: str = "rows") -> None:
    if count > PDF_ROW_LIMIT:
        raise ExportTooLarge(
            f"{count:,} {noun} match these filters; PDF exports are limited to {PDF_ROW_LIMIT:,} rows. "
            "Download Excel or CSV instead (up to 10,000 rows), or narrow the filters.",
        )


def export_filename(slug: str, ext: str, now=None) -> str:
    local = now or now_ist()
    return f"{slug}_{local:%Y-%m-%d_%H%M}.{ext}"


def role_label(user) -> str:
    getter = getattr(user, "get_user_type_display", None)
    try:
        label = getter() if callable(getter) else ""
    except Exception:  # noqa: BLE001 - unknown legacy code
        label = ""
    return str(label or getattr(user, "user_type", "") or "").strip()


def render(document: spec.Document, fmt: str) -> tuple[bytes, str]:
    now = now_ist()
    generated_at = now.strftime("%d %b %Y, %H:%M")
    if fmt == "csv":
        content = render_csv(document)
    elif fmt == "xlsx":
        content = render_xlsx(document, generated_at=generated_at)
    else:
        content = render_pdf(document, generated_at=generated_at)
    return content, export_filename(document.slug, fmt, now)


def export_response(document: spec.Document, fmt: str) -> HttpResponse:
    rows = document.row_count
    check_row_limit(rows)
    if fmt == "pdf":
        check_pdf_limit(rows)
    content, filename = render(document, fmt)
    response = HttpResponse(content, content_type=CONTENT_TYPES[fmt])
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Export-Row-Count"] = str(rows)
    response["Cache-Control"] = "no-store"
    return response
