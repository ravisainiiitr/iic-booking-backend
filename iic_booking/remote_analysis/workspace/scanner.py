"""Virus scanning abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from iic_booking.remote_analysis.constants import VirusStatus
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.remote_analysis.workspace_models import VirusScanResult, WorkspaceFile


@dataclass
class ScanOutcome:
    status: str
    detail: str = ""
    scanner: str = "noop"


class IFileScanner(ABC):
    @abstractmethod
    def scan(self, path: str, *, file: WorkspaceFile | None = None) -> ScanOutcome:
        raise NotImplementedError


class NoOpScanner(IFileScanner):
    """Marks files CLEAN without invoking an AV engine (Milestone 5 default)."""

    def scan(self, path: str, *, file: WorkspaceFile | None = None) -> ScanOutcome:
        return ScanOutcome(status=VirusStatus.CLEAN, detail="NoOpScanner — scan skipped", scanner="noop")


def get_scanner(settings_obj: RemoteAnalysisSettings | None = None) -> IFileScanner:
    settings_obj = settings_obj or RemoteAnalysisSettings.get_solo()
    name = (settings_obj.virus_scanner or "noop").strip().lower()
    # Future: Windows Defender / ClamAV / enterprise adapters
    if name in {"noop", "", "none"}:
        return NoOpScanner()
    return NoOpScanner()


def scan_and_record(file: WorkspaceFile, path: str, *, settings_obj: RemoteAnalysisSettings | None = None) -> VirusScanResult:
    scanner = get_scanner(settings_obj)
    outcome = scanner.scan(path, file=file)
    file.virus_status = outcome.status
    file.save(update_fields=["virus_status", "modified_at"])
    return VirusScanResult.objects.create(
        file=file,
        scanner=outcome.scanner,
        status=outcome.status,
        detail=outcome.detail[:512],
    )
