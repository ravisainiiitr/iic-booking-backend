"""Zip downloads of a whole project or one folder, streamed straight from the research bucket.

Nothing is staged on disk or in memory beyond one read chunk: entries are written with data
descriptors (the output is not seekable) and ZIP64 is switched on per entry from the known size.
"""

from __future__ import annotations

import logging
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from . import storage
from .models import FileStatus, ResearchFile, ResearchFolder, ResearchWorkspace

logger = logging.getLogger(__name__)

TOKEN_SALT = "my_research.zip_download"
TOKEN_MAX_AGE_SECONDS = 300
READ_CHUNK = 1024 * 1024
FLUSH_AT = 1024 * 1024
_UNSAFE_NAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


class ArchiveTooLarge(Exception):
    pass


@dataclass(frozen=True)
class ArchiveEntry:
    arcname: str
    storage_key: str | None
    size: int
    modified: datetime | None


@dataclass(frozen=True)
class ArchivePlan:
    filename: str
    entries: list[ArchiveEntry]

    @property
    def file_count(self) -> int:
        return sum(1 for e in self.entries if e.storage_key)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self.entries if e.storage_key)


def max_files() -> int:
    return int(getattr(settings, "MY_RESEARCH_ZIP_MAX_FILES", 20000))


def max_bytes() -> int:
    return int(getattr(settings, "MY_RESEARCH_ZIP_MAX_BYTES", 100 * 1024**3))


def safe_component(name: str, fallback: str) -> str:
    cleaned = _UNSAFE_NAME.sub("_", (name or "").strip()).strip(" .")
    if cleaned in {"", ".", ".."}:
        return fallback
    return cleaned[:200]


def _unique(name: str, taken: set[str]) -> str:
    if name.lower() not in taken:
        taken.add(name.lower())
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    n = 2
    while True:
        candidate = f"{stem} ({n}).{ext}" if ext else f"{stem} ({n})"
        if candidate.lower() not in taken:
            taken.add(candidate.lower())
            return candidate
        n += 1


def build_plan(workspace: ResearchWorkspace, folder: ResearchFolder | None = None) -> ArchivePlan:
    """Folder tree under ``folder`` (or the whole project) as zip paths rooted at its name."""
    folders = list(
        ResearchFolder.objects.filter(workspace=workspace, deleted_at__isnull=True).values("pk", "parent_id", "name")
    )
    children: dict = {}
    for row in folders:
        children.setdefault(row["parent_id"], []).append(row)

    root_name = safe_component(folder.name if folder else workspace.name, "Project")
    paths: dict = {}
    taken_dirs: dict[str, set[str]] = {root_name: set()}
    queue = [(folder.pk if folder else None, root_name)]
    if folder is not None:
        paths[folder.pk] = root_name
    while queue:
        parent_id, parent_path = queue.pop()
        for row in sorted(children.get(parent_id, []), key=lambda r: r["name"].lower()):
            name = _unique(safe_component(row["name"], "Folder"), taken_dirs.setdefault(parent_path, set()))
            path = f"{parent_path}/{name}"
            paths[row["pk"]] = path
            taken_dirs[path] = set()
            queue.append((row["pk"], path))

    files = ResearchFile.objects.filter(
        workspace=workspace, status=FileStatus.AVAILABLE, deleted_at__isnull=True
    ).only("pk", "folder_id", "display_name", "storage_key", "size_bytes", "uploaded_at", "updated_at")
    if folder is not None:
        files = files.filter(folder_id__in=list(paths))
    else:
        files = files.filter(Q(folder_id__in=list(paths)) | Q(folder__isnull=True))

    entries: list[ArchiveEntry] = [
        ArchiveEntry(f"{p}/", None, 0, None) for p in sorted(paths.values()) if p != root_name
    ]
    count = 0
    total = 0
    for research_file in files.order_by("display_name").iterator():
        dir_path = paths.get(research_file.folder_id, root_name) if research_file.folder_id else root_name
        name = _unique(safe_component(research_file.display_name, "file"), taken_dirs.setdefault(dir_path, set()))
        size = int(research_file.size_bytes or 0)
        count += 1
        total += size
        if count > max_files() or total > max_bytes():
            raise ArchiveTooLarge
        entries.append(
            ArchiveEntry(f"{dir_path}/{name}", research_file.storage_key, size, research_file.uploaded_at or research_file.updated_at)
        )
    return ArchivePlan(filename=f"{root_name}.zip", entries=entries)


def issue_token(*, user_id: int, workspace_id, folder_id) -> str:
    return signing.dumps(
        {"u": user_id, "w": str(workspace_id), "f": str(folder_id) if folder_id else None, "j": uuid.uuid4().hex},
        salt=TOKEN_SALT,
    )


def redeem_token(token: str) -> dict | None:
    """Payload for a fresh, unused token; each link starts one download."""
    try:
        payload = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    if not isinstance(payload, dict) or not payload.get("j"):
        return None
    if not cache.add(f"my_research:zip:{payload['j']}", 1, TOKEN_MAX_AGE_SECONDS * 2):
        return None
    return payload


class _Sink:
    """Write-only target for ZipFile; no tell()/seek(), so entries use data descriptors."""

    def __init__(self):
        self._parts: list[bytes] = []
        self.size = 0

    def write(self, data) -> int:
        chunk = bytes(data)
        self._parts.append(chunk)
        self.size += len(chunk)
        return len(chunk)

    def flush(self) -> None:
        return None

    def drain(self) -> bytes:
        out = b"".join(self._parts)
        self._parts.clear()
        self.size = 0
        return out


def _zip_info(entry: ArchiveEntry) -> zipfile.ZipInfo:
    when = timezone.localtime(entry.modified) if entry.modified else timezone.localtime()
    info = zipfile.ZipInfo(entry.arcname, date_time=(max(when.year, 1980), when.month, when.day, when.hour, when.minute, when.second))
    if entry.storage_key is None:
        info.external_attr = (0o40755 << 16) | 0x10
        return info
    info.external_attr = 0o644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    info.compress_level = 1
    info.file_size = entry.size
    return info


def iter_zip(entries: list[ArchiveEntry]):
    sink = _Sink()
    with zipfile.ZipFile(sink, mode="w", allowZip64=True) as zf:
        for entry in entries:
            info = _zip_info(entry)
            if entry.storage_key is None:
                zf.writestr(info, b"")
                continue
            body = storage.open_stream(entry.storage_key)
            try:
                with zf.open(info, mode="w", force_zip64=entry.size >= zipfile.ZIP64_LIMIT) as dest:
                    while True:
                        chunk = body.read(READ_CHUNK)
                        if not chunk:
                            break
                        dest.write(chunk)
                        if sink.size >= FLUSH_AT:
                            yield sink.drain()
            finally:
                body.close()
            if sink.size:
                yield sink.drain()
    tail = sink.drain()
    if tail:
        yield tail


async def aiter_zip(entries: list[ArchiveEntry]):
    """ASGI must get an async iterator, otherwise Django buffers the whole sync stream in memory."""
    gen = iter_zip(entries)
    step = sync_to_async(lambda: next(gen, None), thread_sensitive=False)
    try:
        while True:
            chunk = await step()
            if chunk is None:
                return
            yield chunk
    finally:
        await sync_to_async(gen.close, thread_sensitive=False)()
