"""Large result downloads without holding files in server or browser memory.

The browser downloads natively from a link instead of ``fetch()``-ing into a Blob: single files from a short-lived
S3 URL; the ZIP from the API with a signed ``t`` token (the browser cannot send the Authorization header). The ZIP
is streamed member by member in ``CHUNK_SIZE`` pieces through an async iterator, because Django buffers a
synchronous streaming iterator in full under ASGI.

Slow or unreliable client networks drop long downloads, and a streamed ZIP cannot be resumed. So "download all"
for one stored file is that file's S3 URL, and a large ZIP of stored files is built once into S3
(``ZIP_CACHE_PREFIX``) and downloaded from there; browsers resume S3 downloads after a dropped connection.
"""

from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

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

ZIP_CACHE_PREFIX = "results-zip-cache"
ZIP_CACHE_MIN_BYTES = 50 * 1024 * 1024
ZIP_CACHE_MAX_AGE = timedelta(days=30)
ZIP_BUILD_STATE_SECONDS = 30 * 60
ZIP_UPLOAD_PART_BYTES = 16 * 1024 * 1024


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


def iter_zip_stream(members: Iterable[ZipMember], *, strict: bool = False) -> Iterator[bytes]:
    """ZIP bytes for ``(arcname, opener)`` members; ``opener()`` returns a readable stream (closed afterwards).

    A member that cannot be opened is skipped (``strict``: raises instead); a read failure mid-member ends the
    stream (the archive would be corrupt otherwise).
    """
    sink = _Sink()
    stamp = timezone.localtime().timetuple()[:6]
    with zipfile.ZipFile(sink, "w", allowZip64=True) as zf:
        for arcname, opener in members:
            try:
                source = opener()
            except Exception:
                if strict:
                    raise
                logger.warning("Results ZIP: could not open %s", arcname, exc_info=True)
                continue
            if source is None:
                if strict:
                    raise FileNotFoundError(arcname)
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


@dataclass
class ResultSource:
    """One result file of a booking: where it goes in the ZIP and how to read it or link to it."""

    arcname: str
    size: int
    ident: str
    open: Callable[[], object]
    direct_url: Optional[Callable[[str], Optional[str]]] = None
    in_object_storage: bool = False

    @property
    def filename(self) -> str:
        return self.arcname.rsplit("/", 1)[-1]


def booking_result_sources(booking, s3_client=None, s3_bucket=None, s3_objects=()) -> list[ResultSource]:
    """Result files of ``booking``; all DB work happens here, openers and links only do I/O.

    ``s3_objects`` are ``(key, relative_name, size)`` under Results/{virtual_booking_id}/.
    """
    from iic_booking.equipment.booking_results_service import (
        booking_result_attachments_qs,
        booking_result_files_qs,
        resolve_dsa_attachment_path,
    )
    from iic_booking.sync.services.results_s3 import open_results_s3_stream, presign_results_s3_get

    def results_link(key):
        return lambda filename: presign_results_s3_get(key, expires_in=LINK_EXPIRES_SECONDS, filename=filename)

    virtual = (booking.virtual_booking_id or f"booking-{booking.pk}").strip()
    sources: list[ResultSource] = []

    for key, relative_name, size in s3_objects:
        sources.append(ResultSource(
            arcname=f"{virtual}/{relative_name}",
            size=int(size or 0),
            ident=f"s3:{key}",
            open=lambda key=key: s3_client.get_object(Bucket=s3_bucket, Key=key)["Body"],
            direct_url=results_link(key),
            in_object_storage=True,
        ))

    for att in booking_result_attachments_qs(booking).select_related("upload_session").order_by("created_at"):
        s3_key = (att.s3_key or "").strip()

        def open_attachment(att=att, s3_key=s3_key):
            body = open_results_s3_stream(s3_key) if s3_key else None
            if body is not None:
                return body
            path = resolve_dsa_attachment_path(att)
            return path.open("rb") if path is not None else None

        sources.append(ResultSource(
            arcname=f"{virtual}/dsa/{att.file_name or 'result.bin'}",
            size=int(att.size_bytes or 0),
            ident=f"s3:{s3_key}" if s3_key else f"dsa:{att.pk}",
            open=open_attachment,
            direct_url=results_link(s3_key) if s3_key else None,
            in_object_storage=bool(s3_key),
        ))

    for brf in booking_result_files_qs(booking).order_by("created_at"):
        if not brf.file:
            continue
        name = (brf.original_name or Path(brf.file.name).name).strip() or Path(brf.file.name).name
        try:
            size = int(brf.file.size or 0)
        except Exception:  # noqa: BLE001
            size = 0
        in_bucket = hasattr(brf.file.storage, "bucket_name")
        sources.append(ResultSource(
            arcname=f"{virtual}/uploaded/{name}",
            size=size,
            ident=f"file:{brf.file.name}",
            open=lambda field=brf.file: open_storage_file_stream(field),
            direct_url=(lambda filename, field=brf.file: storage_file_direct_url(field, filename)) if in_bucket else None,
            in_object_storage=in_bucket,
        ))

    return sources


def booking_zip_members(booking, s3_client=None, s3_bucket=None, s3_objects=()) -> tuple[list[ZipMember], int]:
    """``(members, total source bytes)`` for the results ZIP."""
    sources = booking_result_sources(booking, s3_client, s3_bucket, s3_objects)
    return [(s.arcname, s.open) for s in sources], sum(s.size for s in sources)


# ---------------------------------------------------------------------------
# Large ZIPs built once into object storage
# ---------------------------------------------------------------------------


def zip_fingerprint(sources: list[ResultSource]) -> str:
    """Changes whenever a file is added, removed, renamed or replaced."""
    digest = hashlib.sha256()
    for s in sources:
        digest.update(f"{s.arcname}\0{s.size}\0{s.ident}\n".encode())
    return digest.hexdigest()[:24]


def zip_cache_key(booking_id: int, fingerprint: str) -> str:
    return f"{ZIP_CACHE_PREFIX}/{int(booking_id)}/{fingerprint}.zip"


def zip_cacheable(sources: list[ResultSource]) -> bool:
    """Large enough to matter, and every file readable from object storage by any worker."""
    return (
        len(sources) > 1
        and sum(s.size for s in sources) >= ZIP_CACHE_MIN_BYTES
        and all(s.in_object_storage for s in sources)
    )


def _build_state_key(booking_id: int, fingerprint: str) -> str:
    return f"results-zip-build:{int(booking_id)}:{fingerprint}"


def cached_zip_url(booking_id: int, fingerprint: str, filename: str) -> Optional[str]:
    """Short-lived URL of the built ZIP, or None when it has not been built."""
    from iic_booking.sync.services.results_s3 import _s3_client, presign_results_s3_get

    client, bucket = _s3_client()
    if client is None:
        return None
    key = zip_cache_key(booking_id, fingerprint)
    try:
        client.head_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001
        return None
    return presign_results_s3_get(key, expires_in=LINK_EXPIRES_SECONDS, filename=filename)


def zip_build_state(booking_id: int, fingerprint: str) -> Optional[dict]:
    from django.core.cache import cache

    return cache.get(_build_state_key(booking_id, fingerprint))


def request_zip_build(booking_id: int, fingerprint: str, total_bytes: int) -> dict:
    """Queue the build once; returns its current state (``queued`` / ``building`` / ``failed``)."""
    from django.core.cache import cache

    key = _build_state_key(booking_id, fingerprint)
    state = {"state": "queued", "done": 0, "total": int(total_bytes)}
    if (cache.get(key) or {}).get("state") == "ready":
        cache.delete(key)
    if cache.add(key, state, ZIP_BUILD_STATE_SECONDS):
        from iic_booking.equipment.tasks import build_results_zip

        try:
            build_results_zip.delay(int(booking_id), fingerprint)
        except Exception:
            logger.exception("Results ZIP build could not be queued for booking %s", booking_id)
            state = {"state": "failed", "done": 0, "total": int(total_bytes)}
            cache.set(key, state, ZIP_BUILD_STATE_SECONDS)
        return state
    return cache.get(key) or state


class _ChunkReader(io.RawIOBase):
    """Read-only, non-seekable file over a chunk iterator (for multipart upload)."""

    def __init__(self, chunks: Iterator[bytes]):
        self._chunks = chunks
        self._buffer = bytearray()

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._buffer) < size:
            try:
                self._buffer += next(self._chunks)
            except StopIteration:
                break
        if size < 0:
            size = len(self._buffer)
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out


def build_zip_into_storage(booking, sources: list[ResultSource], fingerprint: str) -> str:
    """Write the ZIP of ``sources`` to object storage (multipart, streamed) and return its key.

    Older ZIPs of the same booking and any cached ZIP past ``ZIP_CACHE_MAX_AGE`` are removed afterwards.
    """
    from boto3.s3.transfer import TransferConfig
    from django.core.cache import cache

    from iic_booking.sync.services.results_s3 import _s3_client

    client, bucket = _s3_client()
    if client is None:
        raise RuntimeError("Object storage is not configured")
    key = zip_cache_key(booking.pk, fingerprint)
    state_key = _build_state_key(booking.pk, fingerprint)
    total = sum(s.size for s in sources)
    progress = {"done": 0, "reported": 0}

    def on_bytes(n):
        progress["done"] += n
        if progress["done"] - progress["reported"] >= ZIP_UPLOAD_PART_BYTES:
            progress["reported"] = progress["done"]
            cache.set(state_key, {"state": "building", "done": progress["done"], "total": total}, ZIP_BUILD_STATE_SECONDS)

    cache.set(state_key, {"state": "building", "done": 0, "total": total}, ZIP_BUILD_STATE_SECONDS)
    client.upload_fileobj(
        _ChunkReader(iter_zip_stream([(s.arcname, s.open) for s in sources], strict=True)),
        bucket,
        key,
        ExtraArgs={"ContentType": "application/zip"},
        Config=TransferConfig(
            multipart_threshold=ZIP_UPLOAD_PART_BYTES, multipart_chunksize=ZIP_UPLOAD_PART_BYTES, max_concurrency=4
        ),
        Callback=on_bytes,
    )
    cache.set(state_key, {"state": "ready", "done": total, "total": total}, ZIP_BUILD_STATE_SECONDS)
    _prune_zip_cache(client, bucket, booking.pk, keep=key)
    return key


def _prune_zip_cache(client, bucket, booking_id: int, *, keep: str) -> None:
    cutoff = timezone.now() - ZIP_CACHE_MAX_AGE
    own_prefix = f"{ZIP_CACHE_PREFIX}/{int(booking_id)}/"
    try:
        stale = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{ZIP_CACHE_PREFIX}/"):
            for obj in page.get("Contents") or []:
                name = obj.get("Key") or ""
                if name == keep:
                    continue
                if name.startswith(own_prefix) or (obj.get("LastModified") and obj["LastModified"] < cutoff):
                    stale.append({"Key": name})
        for start in range(0, len(stale), 1000):
            client.delete_objects(Bucket=bucket, Delete={"Objects": stale[start:start + 1000], "Quiet": True})
    except Exception:
        logger.warning("Results ZIP cache cleanup failed", exc_info=True)


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
