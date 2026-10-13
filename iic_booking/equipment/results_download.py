"""Large result downloads without holding files in server or browser memory.

The browser downloads natively from a link instead of ``fetch()``-ing into a Blob: single files from a short-lived
S3 URL; the ZIP from the API with a signed ``t`` token (the browser cannot send the Authorization header). The ZIP
is streamed member by member in ``CHUNK_SIZE`` pieces through an async iterator, because Django buffers a
synchronous streaming iterator in full under ASGI.
"""

from __future__ import annotations

import logging
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Callable, Iterable, Iterator

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.core import signing
from django.utils import timezone
from django.utils.http import content_disposition_header

logger = logging.getLogger(__name__)

TOKEN_SALT = "booking-results-download"
TOKEN_MAX_AGE_SECONDS = 15 * 60
LINK_EXPIRES_SECONDS = 15 * 60
CHUNK_SIZE = 1024 * 1024

# Already compressed: deflating them again only costs CPU.
STORED_SUFFIXES = frozenset(
    {".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".zst", ".jpg", ".jpeg", ".png", ".gif", ".webp",
     ".mp4", ".mov", ".avi", ".mkv", ".mp3", ".pdf", ".docx", ".xlsx", ".pptx", ".h5", ".hdf5", ".npz"}
)


def make_download_token(user, booking_id: int) -> str:
    return signing.dumps({"u": user.pk, "b": int(booking_id)}, salt=TOKEN_SALT, compress=True)


def user_from_download_token(token: str, booking_id: int):
    try:
        data = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    if not isinstance(data, dict) or data.get("b") != int(booking_id):
        return None
    return get_user_model().objects.filter(pk=data.get("u"), is_active=True).first()


def download_user(request, booking_id: int):
    """Signed-in user, else the user named by a valid ``t`` token for this booking; None when neither."""
    user = getattr(request, "user", None)
    if getattr(user, "is_authenticated", False):
        return user
    token = (request.GET.get("t") or "").strip()
    return user_from_download_token(token, booking_id) if token else None


def tokenized_url(request, path: str, user, booking_id: int) -> str:
    url = f"{path}?t={make_download_token(user, booking_id)}"
    try:
        return request.build_absolute_uri(url)
    except Exception:  # noqa: BLE001
        return url


def storage_file_direct_url(field_file, filename: str) -> str | None:
    """Short-lived S3 URL that downloads ``field_file`` as ``filename``; None for non-S3 storage."""
    storage = field_file.storage
    if not hasattr(storage, "bucket_name"):
        return None
    try:
        return storage.url(
            field_file.name,
            parameters={"ResponseContentDisposition": content_disposition_header(True, filename)},
            expire=LINK_EXPIRES_SECONDS,
        )
    except Exception:
        logger.exception("Could not sign a download URL for %s", field_file.name)
        return None


def open_storage_file_stream(field_file):
    """Chunk-readable stream of a stored file: the S3 body directly (no temp copy), else ``open``."""
    storage = field_file.storage
    if hasattr(storage, "bucket_name") and hasattr(storage, "_normalize_name"):
        key = storage._normalize_name(field_file.name)
        return storage.connection.meta.client.get_object(Bucket=storage.bucket_name, Key=key)["Body"]
    return field_file.open("rb")


class _Sink:
    """Write-only, non-seekable target: ``zipfile`` then streams entries with data descriptors."""

    def __init__(self):
        self._parts: list[bytes] = []
        self._size = 0
        self._pos = 0

    def write(self, data) -> int:
        data = bytes(data)
        self._parts.append(data)
        self._size += len(data)
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def flush(self) -> None:
        pass

    @property
    def pending(self) -> int:
        return self._size

    def drain(self) -> bytes:
        out = b"".join(self._parts)
        self._parts.clear()
        self._size = 0
        return out


ZipMember = tuple[str, Callable[[], object]]


def iter_zip_stream(members: Iterable[ZipMember]) -> Iterator[bytes]:
    """ZIP bytes for ``(arcname, opener)`` members; ``opener()`` returns a readable stream (closed afterwards).

    A member that cannot be opened is skipped; a read failure mid-member ends the stream (the archive would be
    corrupt otherwise).
    """
    sink = _Sink()
    stamp = timezone.localtime().timetuple()[:6]
    with zipfile.ZipFile(sink, "w", allowZip64=True) as zf:
        for arcname, opener in members:
            try:
                source = opener()
            except Exception:
                logger.warning("Results ZIP: could not open %s", arcname, exc_info=True)
                continue
            if source is None:
                continue
            info = zipfile.ZipInfo(arcname, date_time=stamp)
            if Path(arcname).suffix.lower() in STORED_SUFFIXES:
                info.compress_type = zipfile.ZIP_STORED
            else:
                info.compress_type = zipfile.ZIP_DEFLATED
                info.compress_level = 1
            with closing(source), zf.open(info, "w", force_zip64=True) as target:
                while True:
                    chunk = source.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    target.write(chunk)
                    if sink.pending >= CHUNK_SIZE:
                        yield sink.drain()
            if sink.pending:
                yield sink.drain()
    if sink.pending:
        yield sink.drain()


def booking_zip_members(booking, s3_client=None, s3_bucket=None, s3_objects=()) -> tuple[list[ZipMember], int]:
    """``(members, total source bytes)`` for the results ZIP; all DB work happens here, openers only do I/O.

    ``s3_objects`` are ``(key, relative_name, size)`` under Results/{virtual_booking_id}/.
    """
    from iic_booking.equipment.booking_results_service import (
        booking_result_attachments_qs,
        booking_result_files_qs,
        resolve_dsa_attachment_path,
    )
    from iic_booking.sync.services.results_s3 import open_results_s3_stream

    virtual = (booking.virtual_booking_id or f"booking-{booking.pk}").strip()
    members: list[ZipMember] = []
    total = 0

    for key, relative_name, size in s3_objects:
        members.append(
            (f"{virtual}/{relative_name}", lambda key=key: s3_client.get_object(Bucket=s3_bucket, Key=key)["Body"])
        )
        total += int(size or 0)

    for att in booking_result_attachments_qs(booking).select_related("upload_session").order_by("created_at"):

        def open_attachment(att=att):
            s3_key = (att.s3_key or "").strip()
            body = open_results_s3_stream(s3_key) if s3_key else None
            if body is not None:
                return body
            path = resolve_dsa_attachment_path(att)
            return path.open("rb") if path is not None else None

        members.append((f"{virtual}/dsa/{att.file_name or 'result.bin'}", open_attachment))
        total += int(att.size_bytes or 0)

    for brf in booking_result_files_qs(booking).order_by("created_at"):
        if not brf.file:
            continue
        name = (brf.original_name or Path(brf.file.name).name).strip() or Path(brf.file.name).name
        members.append((f"{virtual}/uploaded/{name}", lambda field=brf.file: open_storage_file_stream(field)))
        try:
            total += int(brf.file.size or 0)
        except Exception:  # noqa: BLE001
            pass

    return members, total


def streaming_download(request, chunks: Iterator[bytes], *, filename: str, content_type: str, length: int | None = None):
    """Attachment response that hands ``chunks`` to the client as they are produced (no gzip, no proxy buffering)."""
    from django.core.handlers.asgi import ASGIRequest
    from django.http import StreamingHttpResponse

    django_request = getattr(request, "_request", request)
    content = aiter_chunks(chunks) if isinstance(django_request, ASGIRequest) else chunks
    response = StreamingHttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(True, filename)
    response["Cache-Control"] = "no-store"
    response["X-Accel-Buffering"] = "no"
    if length is not None:
        response["Content-Length"] = str(length)
    response.skip_gzip = True
    return response


def iter_file_chunks(source) -> Iterator[bytes]:
    with closing(source):
        while True:
            chunk = source.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


async def aiter_chunks(chunks: Iterator[bytes]):
    """Serve a blocking chunk iterator under ASGI one chunk at a time (off the event loop)."""
    done = object()
    try:
        while True:
            chunk = await sync_to_async(next, thread_sensitive=False)(chunks, done)
            if chunk is done:
                break
            if chunk:
                yield chunk
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            await sync_to_async(close, thread_sensitive=False)()
