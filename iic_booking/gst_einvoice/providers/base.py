"""The interface every IRP adapter implements. Business logic (``service``) only talks to this."""

from __future__ import annotations

from dataclasses import dataclass, field

GENERATE, GET_BY_DOC, GET_IRN, GSTIN = "generate", "get_by_doc", "get_irn", "gstin"
ALL_OPERATIONS = (GSTIN, GENERATE, GET_BY_DOC, GET_IRN)  # no cancellation (SRIC decision)


@dataclass
class ProviderResult:
    ok: bool
    data: dict = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    uncertain: bool = False  # no answer: the request may or may not have been processed
    unsupported: bool = False
    http_status: int | None = None
    request: dict = field(default_factory=dict)  # redacted
    response: dict = field(default_factory=dict)  # redacted
    duration_ms: int | None = None


class IrpProvider:
    name = "base"
    simulated = False
    label = ""

    def capabilities(self) -> set[str]:
        return set()

    def unsupported(self, operation: str) -> ProviderResult:
        return ProviderResult(False, unsupported=True, error_message=f"{operation} is not available in {self.label}.")

    def gstin_details(self, gstin: str) -> ProviderResult:
        return self.unsupported("Get GSTIN details")

    def generate_irn(self, payload: dict, *, notes: str = "") -> ProviderResult:
        return self.unsupported("Generate IRN")

    def get_by_document(self, doc_type: str, doc_number: str, doc_date: str) -> ProviderResult:
        return self.unsupported("Get IRN by document")

    def get_irn(self, irn: str) -> ProviderResult:
        return self.unsupported("Get e-invoice by IRN")

