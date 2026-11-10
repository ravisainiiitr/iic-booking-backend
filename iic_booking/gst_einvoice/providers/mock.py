"""Simulated sandbox: answers like the NIC sandbox's documented operations, but nothing leaves the server and every
value is marked as simulated so it can never be mistaken for an IRP response:

* IRN: "SIMULATED-" + 40 hex characters (a real IRN is 64 hex characters with no prefix);
* acknowledgement number starts with "SIM";
* the "signed" invoice and QR are plain JSON carrying "SIMULATED": true and no signature.

Demonstration switches (in the invoice notes): "#simulate-timeout" (the first Generate IRN registers the invoice but
answers nothing, so the portal must look it up by document before trying again) and "#simulate-refusal" (refused with
an IRP-style error).
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from ..gstin import validate as validate_gstin
from .base import ALL_OPERATIONS, IrpProvider, ProviderResult

SIM_PREFIX = "SIMULATED-"


def _sim_irn(gstin: str, doc_type: str, doc_number: str, doc_date: str) -> str:
    digest = hashlib.sha256(f"{gstin}|{doc_type}|{doc_number}|{doc_date}|simulated".encode()).hexdigest()
    return SIM_PREFIX + digest[:40]


def _unsigned_token(claims: dict) -> str:
    """A JWT-shaped string with alg 'none' (deliberately unsigned) and SIMULATED in its claims."""
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none', 'typ': 'JWT', 'note': 'SIMULATED - not signed by the GST IRP'})}.{part({'SIMULATED': True, **claims})}."


class MockSandboxProvider(IrpProvider):
    name = "mock"
    simulated = True
    label = "the simulated sandbox"

    def capabilities(self) -> set[str]:
        return set(ALL_OPERATIONS)

    # --- GSTIN -------------------------------------------------------------------------------------------------------
    def gstin_details(self, gstin: str) -> ProviderResult:
        ok, why = validate_gstin(gstin)
        req = {"operation": "Get GSTIN details", "Gstin": gstin}
        if not ok:
            return ProviderResult(False, error_code="3028", error_message=f"GSTIN is invalid ({why})", request=req)
        data = {"Gstin": gstin, "Status": "ACT", "StateCode": gstin[:2], "SIMULATED": True,
                "Note": "Simulated check: only the format and check digit were verified."}
        return ProviderResult(True, data=data, request=req, response=data)

    # --- Generate IRN ------------------------------------------------------------------------------------------------
    def generate_irn(self, payload: dict, *, notes: str = "") -> ProviderResult:
        from ..models import SimulatedIrpRecord

        started = time.monotonic()
        doc = payload["DocDtls"]
        gstin = payload["SellerDtls"]["Gstin"]
        req = {"operation": "Generate IRN", "DocDtls": doc, "SellerGstin": gstin, "BuyerGstin": payload["BuyerDtls"]["Gstin"],
               "TotInvVal": payload["ValDtls"]["TotInvVal"]}
        if "#simulate-refusal" in notes:
            return ProviderResult(False, error_code="2172", error_message="Simulated refusal: the recipient GSTIN is not valid for this document (demonstration).",
                                  request=req, response={"Status": "0", "SIMULATED": True}, duration_ms=_ms(started))
        existing = SimulatedIrpRecord.objects.filter(supplier_gstin=gstin, doc_type=doc["Typ"], doc_number=doc["No"]).first()
        if existing is not None:
            info = {"AckNo": existing.ack_number, "AckDt": _fmt(existing.ack_date), "Irn": existing.irn}
            return ProviderResult(False, error_code="2150", error_message="Duplicate IRN", request=req,
                                  response={"Status": "0", "InfoDtls": [{"InfCd": "DUPIRN", "Desc": info}], "SIMULATED": True},
                                  data={"duplicate": info}, duration_ms=_ms(started))
        record = self._register(payload)
        if "#simulate-timeout" in notes:
            # Registered, but the answer never arrives (like a read timeout after the IRP processed it).
            return ProviderResult(False, uncertain=True, error_message="Simulated timeout: no answer within the time limit.",
                                  request=req, duration_ms=_ms(started))
        return ProviderResult(True, data=self._answer(record), request=req, response=self._answer(record, redact=True),
                              duration_ms=_ms(started))

    def _register(self, payload: dict):
        from ..models import SimulatedIrpRecord

        doc = payload["DocDtls"]
        gstin = payload["SellerDtls"]["Gstin"]
        now = timezone.now()
        irn = _sim_irn(gstin, doc["Typ"], doc["No"], doc["Dt"])
        ack = "SIM" + str(int(now.timestamp() * 1000))[-12:]
        qr_claims = {"SellerGstin": gstin, "BuyerGstin": payload["BuyerDtls"]["Gstin"], "DocNo": doc["No"],
                     "DocTyp": doc["Typ"], "DocDt": doc["Dt"], "TotInvVal": payload["ValDtls"]["TotInvVal"],
                     "ItemCnt": len(payload["ItemList"]), "MainHsnCode": payload["ItemList"][0]["HsnCd"], "Irn": irn,
                     "IrnDt": _fmt(now)}
        try:
            with transaction.atomic():
                return SimulatedIrpRecord.objects.create(
                    supplier_gstin=gstin, doc_type=doc["Typ"], doc_number=doc["No"], doc_date=doc["Dt"], irn=irn,
                    ack_number=ack, ack_date=now, signed_qr=_unsigned_token({"data": qr_claims}),
                    signed_invoice=_unsigned_token({"data": {**payload, "Irn": irn, "AckNo": ack}}))
        except IntegrityError:
            return SimulatedIrpRecord.objects.get(supplier_gstin=gstin, doc_type=doc["Typ"], doc_number=doc["No"])

    @staticmethod
    def _answer(record, *, redact: bool = False) -> dict:
        return {
            "AckNo": record.ack_number, "AckDt": _fmt(record.ack_date), "Irn": record.irn, "Status": record.status,
            "SignedInvoice": f"<simulated, {len(record.signed_invoice)} characters>" if redact else record.signed_invoice,
            "SignedQRCode": f"<simulated, {len(record.signed_qr)} characters>" if redact else record.signed_qr,
            "SIMULATED": True,
        }

    # --- lookups and cancellation ------------------------------------------------------------------------------------
    def get_by_document(self, doc_type: str, doc_number: str, doc_date: str) -> ProviderResult:
        from ..models import SimulatedIrpRecord

        req = {"operation": "Get IRN by document", "doctype": doc_type, "docnum": doc_number, "docdate": doc_date}
        record = SimulatedIrpRecord.objects.filter(doc_type=doc_type, doc_number=doc_number, doc_date=doc_date).first()
        if record is None:
            return ProviderResult(False, error_code="2154", error_message="IRN details are not found", request=req)
        return ProviderResult(True, data=self._answer(record), request=req, response=self._answer(record, redact=True))

    def get_irn(self, irn: str) -> ProviderResult:
        from ..models import SimulatedIrpRecord

        record = SimulatedIrpRecord.objects.filter(irn=irn).first()
        req = {"operation": "Get e-invoice by IRN", "Irn": irn}
        if record is None:
            return ProviderResult(False, error_code="2154", error_message="IRN details are not found", request=req)
        return ProviderResult(True, data=self._answer(record), request=req, response=self._answer(record, redact=True))

    def cancel_irn(self, irn: str, reason: str, remarks: str) -> ProviderResult:
        from ..models import SimulatedIrpRecord

        req = {"operation": "Cancel IRN", "Irn": irn, "CnlRsn": reason, "CnlRem": remarks}
        record = SimulatedIrpRecord.objects.filter(irn=irn).first()
        if record is None:
            return ProviderResult(False, error_code="2154", error_message="IRN details are not found", request=req)
        if record.status == "CNL":
            return ProviderResult(False, error_code="9999", error_message="Invoice is already cancelled", request=req)
        if timezone.now() - record.ack_date > timedelta(hours=24):
            return ProviderResult(False, error_code="2270", error_message="The allowed cancellation time limit is crossed, you cannot cancel the IRN",
                                  request=req)
        record.status, record.cancelled_at = "CNL", timezone.now()
        record.save(update_fields=["status", "cancelled_at"])
        data = {"Irn": irn, "CancelDate": _fmt(record.cancelled_at), "SIMULATED": True}
        return ProviderResult(True, data=data, request=req, response=data)


def _fmt(dt) -> str:
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M:%S")


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
